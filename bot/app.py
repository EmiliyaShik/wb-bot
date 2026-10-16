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
import os
import time
import traceback

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import InvalidToken, NetworkError
from telegram.ext import Application, CommandHandler, ContextTypes

from bot import netwatch, texts
from bot.handlers import menu, register_all
from core import audit, config, crypto, db, queue, scheduler

logger = logging.getLogger(__name__)

TOKEN_MISSING = (
    "Не задан TELEGRAM_BOT_TOKEN. Создайте файл .env на основе .env.example "
    "и положите туда токен бота, полученный у @BotFather в Telegram."
)

START_AGAIN = (
    "Запуск не удался, связи с Telegram нет ({error}). Подожду {pause} с и "
    "попробую снова. Настройки тут не при чём: токен на месте, не отвечает сеть."
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

# Сколько раз пробовать поздороваться с Telegram на запуске. Минус один это
# «бесконечно», и это не настройка, а правило: обрыв связи в момент запуска не
# повод не запускаться. По умолчанию библиотека ставит здесь ноль, то есть
# первая же неудача getMe роняет процесс с трассировкой, а на хостинге это
# значит «деплой не поднялся из-за помех у провайдера». Паузу между этими
# попытками библиотека держит сама, секунду.
BOOTSTRAP_RETRIES_FOREVER = -1


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


def make_owner_sender(app):
    """Слово владельцу, которое не прячет отказ отправки.

    `make_notifier` гасит отказ намеренно: очередь не должна падать из-за
    молчащего Telegram. Присмотру за связью нужно обратное, именно знать, дошло
    ли слово: не дошло - он повторит его, когда связь вернётся.
    """

    async def send(text: str) -> None:
        for chat_id in config.admin_ids():
            await app.bot.send_message(chat_id=chat_id, text=text)

    return send


async def start_background(app) -> None:
    """Поднимает фон: сначала подбираем брошенное, потом расписание, потом воркер."""
    queue.set_notifier(make_notifier(app))
    netwatch.set_notifier(make_owner_sender(app))

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

    # Меню команд заявляется при каждом запуске, а не руками у @BotFather:
    # список команд живёт в коде, и правка описания доезжает до клиентов сама.
    # Это поход в сеть, и уронить запуск он не имеет права: отказ Telegram
    # остаётся записью в журнале, бот работает дальше командами, как раньше.
    await menu.publish(app.bot)

    stop = asyncio.Event()
    app.bot_data["queue_stop"] = stop
    app.bot_data["queue_worker"] = asyncio.create_task(
        queue.run_worker(poll_sec=WORKER_POLL_SEC, stop=stop)
    )
    logger.info("Воркер очереди запущен")


async def stop_background(app) -> None:
    """Останавливает воркер без обрыва текущей задачи."""
    # Говорить владельцу про связь больше некуда: приложение уже погашено.
    netwatch.set_notifier(None)
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


# Библиотеки, которые пишут в журнал полный адрес запроса. Токен бота это
# часть адреса (https://api.telegram.org/bot<ТОКЕН>/getMe), а на хостинге
# журнал хранится и бывает виден, так что утечка настоящая. Свой журнал мы
# чистим сами (core.audit.redact), но чужая библиотека пишет мимо него.
URL_WRITING_LOGGERS = ("httpx", "httpcore", "telegram.request")


def hush_url_logging() -> None:
    """Поднимает порог болтливым библиотекам: ошибки видны, адреса нет."""
    for name in URL_WRITING_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


class SecretsHidden(logging.Filter):
    """Сторож вывода: из каждой строчки логов вычищаются секреты.

    Порог болтливым библиотекам закрывает обычный путь, но не ошибки. Текст
    исключения и кадры стека пишутся как есть, а адрес запроса к Telegram
    содержит токен бота целиком; на хостинге этот вывод хранится и читается
    не только владельцем. Один раз бот уже напечатал туда свой токен.

    Чистка та же, что у журнала (`core.audit.redact`), второй её в проекте
    нет. Сам сторож не роняет ничего: сбой чистки оставляет строку как была,
    потому что потерянная запись хуже неидеальной.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = audit.redact(record.getMessage())
            record.args = ()
            if record.exc_info:
                text = record.exc_text or "".join(
                    traceback.format_exception(*record.exc_info)
                )
                record.exc_text = audit.redact(text)
        except Exception:  # noqa: BLE001 - сторож логов не ломает то, что пишет
            return True
        return True


def hide_secrets_in_logs() -> None:
    """Вешает сторож на вывод. Повторный вызов ничего не удваивает.

    Фильтр нужен и на обработчиках, и на корневом журнале: записи дочерних
    журналов проходят через обработчики корня, а записи самого корня через
    его собственные фильтры.
    """
    root = logging.getLogger()
    for target in (root, *root.handlers):
        if not any(isinstance(one, SecretsHidden) for one in target.filters):
            target.addFilter(SecretsHidden())


def startup() -> dict:
    """Готовит бота к работе: папка данных, миграции, честный отчёт о ключах.

    Ничего не роняет: без ключа шифрования и без списка админов бот работает,
    просто часть возможностей выключена, и об этом написано в журнале.
    """
    hush_url_logging()
    hide_secrets_in_logs()
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


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    path: str | None = None,
) -> None:
    # Кнопки рисует bot.handlers.menu: они зовут те же функции, что и команды,
    # и набор их зависит от того, подключён ли кабинет.
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(
        texts.INTRO,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=menu.main_keyboard(update, path=path),
    )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    path: str | None = None,
) -> None:
    # effective_message, а не message: сюда же приходит кнопка «Что я умею», а
    # у нажатой кнопки собственного message нет.
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(
        texts.HELP,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        # Без кнопки «Что я умею»: она показала бы ровно это же сообщение.
        reply_markup=menu.main_keyboard(update, path=path, without=(menu.HELP,)),
    )


async def on_error(update: object, context) -> None:
    """Единственный обработчик ошибок: клиент получает ответ, владелец запись.

    Без него сбой в хендлере оставлял клиента вообще без ответа, а владельца
    без строчки в журнале: искать было нечего и негде.
    """
    error = getattr(context, "error", None)

    # Неудачная попытка опроса приезжает сюда без апдейта и без работы
    # расписания, и своя запись у неё уже есть: bot.netwatch пишет начало
    # обрыва, его затягивание и возвращение связи. Вторая запись на каждую
    # попытку превратила бы журнал в поток одинаковых строк и вытеснила бы из
    # него всё остальное: за одиннадцать минут обрыва это десятки строк.
    # Спрашиваем присмотр, а не тип ошибки: в этой библиотеке BadRequest тоже
    # наследник NetworkError, и по типу обрыв связи от испорченного запроса не
    # отличить.
    if update is None and getattr(context, "job", None) is None and netwatch.failures():
        return

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
    # get_updates_request: транспорт опроса под присмотром bot.netwatch. Обрыв
    # связи приходит сюда первым, и только здесь его видно целиком, включая
    # таймаут, который библиотека повторяет молча и без паузы.
    app = (
        Application.builder()
        .token(token)
        .concurrent_updates(CONCURRENT_UPDATES)
        .get_updates_request(netwatch.updates_request())
        .post_init(start_background)
        .post_shutdown(stop_background)
        .build()
    )
    # Признак «опрос ещё идёт» библиотека снимает раньше, чем делает последний
    # заход за прочитанным, и по нему присмотр отличает обрыв от выключения.
    netwatch.set_polling(lambda: app.updater is not None and app.updater.running)

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

    attempt = 0
    while True:
        try:
            _run_once(token)
            return
        except InvalidToken as exc:
            raise SystemExit(TOKEN_REJECTED) from exc
        except NetworkError as error:
            # Обрыв связи в момент запуска это не повод не запускаться. Сюда
            # приводит getMe внутри initialize(): библиотека зовёт его, чтобы
            # проверить токен, и повторов на нём не держит никаких, а
            # bootstrap_retries до этого места не доходит. Без этого цикла
            # деплой во время помех у провайдера кончался трассировкой.
            attempt += 1
            pause = netwatch.pause_sec(attempt)
            audit.log(
                "telegram",
                None,
                START_AGAIN.format(
                    error=f"{type(error).__name__}: {error}"[:200], pause=int(pause)
                ),
                level="warning",
            )
            time.sleep(pause)


def _run_once(token: str) -> None:
    """Одна попытка поднять бота. Возвращается, когда бота погасили.

    Свой цикл событий на попытку, и закрывает его тот же, кто открыл. Иначе
    второй попытки не было бы вовсе: библиотека берёт текущий цикл, а
    закрытый цикл падает на первом же ожидании.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        app = build_app(token)
        logger.info("Бот запущен")
        app.run_polling(
            allowed_updates=Update.ALL_TYPES,
            bootstrap_retries=BOOTSTRAP_RETRIES_FOREVER,
            close_loop=False,
        )
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def main() -> None:
    """Запуск из любой точки входа: `python bot.py` и `python -m bot`.

    Хостинг собирает образ сам и ставит свою команду запуска: Bothost собрал
    `python -m bot.app`, хотя в Procfile написано `python bot.py`. Модуль без
    этой пары строк просто импортировался и завершался с кодом ноль, то есть
    для хостинга «успешно», а бот при этом не поднимался вовсе и сообщения не
    читал никто. Поэтому запуск умеет оба способа, а живёт он здесь один.
    """
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )
    hide_secrets_in_logs()
    run(os.getenv("TELEGRAM_BOT_TOKEN"))


if __name__ == "__main__":
    main()
