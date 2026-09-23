"""Сборка приложения Telegram.

Этот файл после таска 01 не меняется: новый хендлер это новый файл в
bot/handlers с функцией register(app), реестр найдёт его сам.

Порядок важен. Внутри одной группы python-telegram-bot берёт первый
подходящий хендлер, поэтому:
  1) модули из bot/handlers - у них приоритет;
  2) базовые команды /start и /help - если модуль их не перехватил;
  3) обработчик артикула из legacy - запасной для любого текста.
"""

from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import InvalidToken
from telegram.ext import Application, CommandHandler, ContextTypes

from bot import texts
from bot.handlers import register_all
from core import audit, config, crypto, db, queue, scheduler

logger = logging.getLogger(__name__)

TOKEN_MISSING = (
    "Не задан TELEGRAM_BOT_TOKEN. Создайте файл .env на основе .env.example "
    "и положите туда токен бота, полученный у @BotFather в Telegram."
)

TOKEN_REJECTED = (
    "Телеграм не принял TELEGRAM_BOT_TOKEN: токен неверный, отозван или "
    "скопирован не целиком. Проверьте значение в .env или в настройках "
    "хостинга, при необходимости выпустите новый у @BotFather. "
    "Токен выглядит так: 123456789:ABCdef-ГдеТоДлиннаяСтрокаБукв."
)

# Сколько сообщений бот разбирает одновременно.
CONCURRENT_UPDATES = 32

# Как часто воркер заглядывает в пустую очередь.
WORKER_POLL_SEC = 5.0
# Сколько ждём, пока воркер доработает текущую задачу при выключении.
WORKER_STOP_TIMEOUT_SEC = 10.0


def make_notifier(app):
    """Чем очередь пишет клиенту: перевод внутреннего id в чат Telegram.

    Задача без клиента это служебная работа, и о ней должен узнать владелец,
    поэтому такое сообщение уходит в ADMIN_TELEGRAM_IDS. Ошибка отправки
    гасится: очередь не должна падать из-за молчащего Telegram.
    """

    async def notify(client_id: int | None, text: str) -> None:
        if client_id is None:
            chat_ids = list(config.admin_ids())
        else:
            row = db.admin_repo().client(client_id)
            chat_ids = [int(row["telegram_id"])] if row else []
            if not row:
                logger.warning("некому написать: клиента %s нет в базе", client_id)
        for chat_id in chat_ids:
            try:
                await app.bot.send_message(chat_id=chat_id, text=text)
            except Exception:  # noqa: BLE001 - молчание Telegram не наша авария
                logger.exception("не удалось отправить сообщение в чат %s", chat_id)

    return notify


async def start_background(app) -> None:
    """Поднимает фон: сначала подбираем брошенное, потом расписание, потом воркер."""
    queue.set_notifier(make_notifier(app))

    recovered = queue.recover()
    if recovered:
        audit.log("queue", None, f"После перезапуска вернул в очередь задач: {recovered}.")

    job_queue = getattr(app, "job_queue", None)
    if job_queue is None:
        app.bot_data["scheduled_jobs"] = []
        audit.log(
            "schedule",
            None,
            "JobQueue недоступен, расписание выключено. Бот работает, но сам "
            "ничего не пришлёт. Установите зависимость python-telegram-bot[job-queue].",
            level="warning",
        )
    else:
        names = scheduler.install(job_queue)
        app.bot_data["scheduled_jobs"] = names
        audit.log(
            "schedule",
            None,
            "Расписание: " + (", ".join(names) if names else "работ пока нет"),
        )

    stop = asyncio.Event()
    app.bot_data["queue_stop"] = stop
    app.bot_data["queue_worker"] = asyncio.create_task(
        queue.run_worker(poll_sec=WORKER_POLL_SEC, stop=stop)
    )
    logger.info("Воркер очереди запущен")


async def stop_background(app) -> None:
    """Останавливает воркер без обрыва текущей задачи."""
    stop = app.bot_data.pop("queue_stop", None)
    worker = app.bot_data.pop("queue_worker", None)
    if stop is not None:
        stop.set()
    if worker is None:
        return
    try:
        await asyncio.wait_for(worker, timeout=WORKER_STOP_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        logger.warning("воркер не успел остановиться, снимаю принудительно")
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
    except asyncio.CancelledError:
        pass

    # Общая HTTP-сессия к Wildberries живёт на весь процесс, закрыть её должен
    # тот, кто гасит фон. Импорт ленивый: выключение бота не обязано зависеть
    # от того, загружен ли модуль WB, и сломанное закрытие не мешает остальному.
    try:
        from core import wbapi

        await wbapi.close_session()
    except Exception:  # noqa: BLE001 - на выключении мы уже ничего не спасаем
        logger.exception("не удалось закрыть сессию к Wildberries")

    db.close_all()
    logger.info("Воркер очереди остановлен")


def startup() -> dict:
    """Готовит бота к работе: папка данных, миграции, честный отчёт о ключах.

    Ничего не роняет: без ключа шифрования и без списка админов бот работает,
    просто часть возможностей выключена, и об этом написано в журнале.
    """
    config.data_dir().mkdir(parents=True, exist_ok=True)
    version = db.migrate()

    admins = config.admin_ids()
    if not admins:
        audit.log(
            "system",
            None,
            "Переменная ADMIN_TELEGRAM_IDS пуста: админ-команд нет ни у кого. "
            "Укажите в ней свой Telegram ID, чтобы получить /stats и /tasks.",
            level="warning",
        )

    tokens_enabled = crypto.key_available()
    if not tokens_enabled:
        audit.log(
            "system",
            None,
            "Переменная ENCRYPTION_KEY не задана или испорчена: токены кабинетов "
            "принимать нельзя. " + crypto.KEY_HINT,
            level="warning",
        )

    audit.log(
        "system",
        None,
        f"Бот запущен. Схема базы {version}, админов {len(admins)}, "
        f"приём токенов {'включён' if tokens_enabled else 'выключен'}.",
    )
    return {
        "schema_version": version,
        "admins": len(admins),
        "tokens_enabled": tokens_enabled,
        "db_path": str(config.db_path()),
    }


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        texts.INTRO, parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        texts.HELP, parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


async def on_error(update: object, context) -> None:
    """Единственный обработчик ошибок: клиент получает ответ, владелец запись.

    Без него сбой в хендлере оставлял клиента вообще без ответа, а владельца
    без строчки в журнале: искать было нечего и негде.
    """
    error = getattr(context, "error", None)
    logger.exception("сбой в хендлере: %s", error, exc_info=error)

    client_id = None
    try:
        user = getattr(update, "effective_user", None)
        if user is not None:
            row = db.admin_repo().client_by_telegram(int(user.id))
            client_id = int(row["id"]) if row else None
    except Exception:  # noqa: BLE001 - разбор апдейта не должен добавить второй сбой
        logger.exception("не удалось определить клиента по апдейту")

    audit.log(
        "error",
        client_id,
        f"{type(error).__name__}: {error}" if error else "неизвестный сбой",
        level="error",
    )

    message = getattr(update, "effective_message", None)
    if message is None:
        return
    try:
        await message.reply_text(texts.SOMETHING_WENT_WRONG)
    except Exception:  # noqa: BLE001 - молчащий Telegram уже не наша авария
        logger.exception("не удалось ответить клиенту на сбой")


def register_base(app: Application) -> None:
    """Команды, которые есть всегда, даже когда ни один модуль не подключён."""
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))


def build_app(token: str) -> Application:
    """Собирает приложение со всеми хендлерами в правильном порядке.

    Фон (очередь и расписание) поднимается в post_init и гасится в
    post_shutdown: так он живёт ровно столько, сколько живёт бот.
    """
    # concurrent_updates: иначе бот разбирает строго одно сообщение за раз, и
    # любой хендлер, который чего-то ждёт (поход на витрину WB за карточкой,
    # ответ Telegram), держит очередь для всех остальных клиентов.
    app = (
        Application.builder()
        .token(token)
        .concurrent_updates(CONCURRENT_UPDATES)
        .post_init(start_background)
        .post_shutdown(stop_background)
        .build()
    )

    names = register_all(app)
    logger.info("Хендлеры модулей: %s", ", ".join(names) if names else "пока нет")

    register_base(app)
    app.add_error_handler(on_error)

    # Запасной обработчик текста ставится последним, иначе он перехватил бы
    # сообщения, адресованные модулям (ввод токена, себестоимость и прочее).
    from legacy import handlers as legacy_handlers

    legacy_handlers.register(app)

    return app


def run(token: str | None) -> None:
    """Запускает бота. Проблема с токеном объясняется по-русски, без трассировки.

    Пустой токен и токен, который не принял Telegram, это одна и та же ошибка
    настройки, и человек должен получить на них одинаково понятный ответ.
    """
    if not (token or "").strip():
        raise SystemExit(TOKEN_MISSING)

    report = startup()
    logger.info(
        "База: %s, схема %s, админов %s, приём токенов %s",
        report["db_path"],
        report["schema_version"],
        report["admins"],
        "включён" if report["tokens_enabled"] else "выключен",
    )

    try:
        app = build_app(token)
        logger.info("Бот запущен")
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    except InvalidToken as exc:
        raise SystemExit(TOKEN_REJECTED) from exc
