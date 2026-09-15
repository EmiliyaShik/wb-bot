"""Подключение кабинета Wildberries: оферта, токен, проверка, отключение.

Это главная дверь для клиента: всё остальное в сервисе начинается отсюда.
Поэтому здесь много объяснений и мало решений. Данные и решения живут в
core.clients, разбор токена в core.wbapi, доступ к модулям в core.access.

Читатель этих строк селлер, а не программист: где нажать в кабинете WB,
названо по шагам, а слово «категория» объяснено тем, что перестанет работать.

Строка токена не попадает ни в один текст этого модуля, даже обрезанной.
Сообщение с токеном бот удаляет из переписки сразу после ответа.

Путь к базе приходит параметром path, как и у остальных хендлеров проекта.
Транспорт WB хендлер не знает вовсе: он зовёт core.clients, а тот берёт
общее соединение через core.wbapi.
"""

from __future__ import annotations

import functools
import logging
import re
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot import texts
from bot.handlers import tariffs
from core import access, audit, clients, config, crypto, db, queue, scheduler, wbapi

logger = logging.getLogger(__name__)

PREFIX = "connect:"
AGREE = f"{PREFIX}agree"
REPLACE = f"{PREFIX}replace"
KEEP = f"{PREFIX}keep"
WIPE = f"{PREFIX}wipe"

# Токен это JWT: три части через точку, и первая всегда начинается с eyJ,
# потому что это base64 от открывающей фигурной скобки с кавычкой. Привязки
# к началу и концу строки тут нет намеренно: человек пишет «вот мой токен ...»,
# и такое сообщение тоже должно попасть сюда. Иначе токен ушёл бы в запасной
# обработчик и остался висеть в переписке неудалённым. Артикул товара и ссылка
# на карточку под это описание не подходят, старое поведение цело.
TOKEN_PATTERN = r"eyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]*"

# Имя ежедневной работы в расписании. Оно же имя вида задачи в очереди.
REMINDER_JOB = "token_expiry_reminder"

INSTRUCTION = (
    "🔑 <b>Подключение кабинета Wildberries</b>\n\n"
    "Боту нужен токен <b>только на чтение</b>. С таким токеном он ничего не может "
    "изменить в вашем кабинете: ни цены, ни карточки, ни поставки. Он только читает "
    "отчёты и считает деньги.\n\n"
    "<b>Как выпустить токен:</b>\n"
    "1. Откройте личный кабинет продавца на сайте Wildberries.\n"
    "2. Нажмите на название магазина в правом верхнем углу, выберите «Настройки», "
    "затем «Доступ к API».\n"
    "3. Нажмите «Создать новый токен».\n"
    "4. В названии напишите WBRentgen. Это <b>отдельный</b> токен только для этого бота: "
    "отдельный нужен затем, чтобы вы могли отозвать его одной кнопкой, не трогая "
    "остальные свои токены.\n"
    "5. Поставьте галочку <b>«Только на чтение»</b>.\n"
    "6. Отметьте пять категорий: <b>Статистика</b>, <b>Финансы</b>, <b>Аналитика</b>, "
    "<b>Продвижение</b>, <b>Контент</b>.\n"
    "7. Нажмите «Создать токен» и скопируйте его целиком.\n\n"
    "Wildberries показывает токен один раз, поэтому копируйте сразу и целиком.\n"
    "Срок жизни токена 180 дней, я напомню заранее, когда он будет кончаться."
)

SEND_TOKEN = (
    "Теперь пришлите токен одним сообщением. Я сохраню его зашифрованным, "
    "а ваше сообщение с токеном удалю из переписки."
)

NEED_CONSENT = (
    "⛔️ Токен я пока не принял.\n\n"
    "Сначала нужно согласиться с офертой: это одна кнопка ниже. "
    "Пока согласия нет, бот не имеет права хранить ваш токен и данные кабинета, "
    "поэтому присланный токен я не сохранил."
)

CONSENT_SAVED = "✅ Согласие записано, время сохранено."

OFFER_PENDING = (
    "Текст оферты сейчас готовится, ссылки пока нет. "
    "Владелец бота уже знает об этом."
)

READ_ONLY_WARNING = (
    "⚠️ У этого токена не стоит галочка «Только на чтение».\n"
    "Я его принял и работать буду, но лучше выпустите другой, с галочкой. "
    "Бот ничего не пишет в кабинет, а токен с правом записи это лишний риск: "
    "если он утечёт, чужой человек сможет менять ваши данные."
)

BAD_TOKEN_TEXT = (
    "Это не похоже на токен Wildberries. Токен выдаётся в личном кабинете, "
    "в разделе доступа к API, и выглядит как три длинные части через точку. "
    "Скопируйте его целиком и пришлите ещё раз."
)

ACC_WRONG = (
    "⚠️ Это токен типа «{title}», а бот рассчитан на <b>Персональный</b>.\n"
    "Я его принял, но часть отчётов может не открыться: Wildberries пускает "
    "к некоторым методам не все типы токенов. Если увидите отказы, выпустите "
    "в кабинете обычный персональный токен и пришлите его сюда."
)

ACC_TEST = (
    "⚠️ Это тестовый токен, то есть песочница. В нём нет ваших настоящих продаж, "
    "и все цифры в отчётах будут ненастоящими. Для работы нужен обычный токен, "
    "без признака тестового."
)

REVOKED = (
    "Wildberries не принял этот токен. Так бывает, если токен уже отозвали, "
    "у него кончился срок или он выпущен в другом кабинете. "
    "Выпустите новый и пришлите снова."
)

EXPIRED = (
    "У этого токена уже кончился срок: токены Wildberries живут 180 дней. "
    "Выпустите новый по инструкции из команды <code>/connect</code> и пришлите его."
)

ALREADY = (
    "Кабинет уже подключён. Один клиент подключает один кабинет, "
    "поэтому новый токен <b>заменит</b> старый, а вторым кабинетом не станет.\n\n"
    "Заменить токен стоит, если вы отозвали старый, выпустили новый "
    "или добавили ему недостающие категории."
)

KEEP_TEXT = "Хорошо, ничего не меняю. Старый токен продолжает работать."

DISCONNECT_ASK = (
    "🗑 <b>Отключение кабинета</b>\n\n"
    "Я удалю <b>всё</b>: токен, себестоимость, собранные отчёты, счета, "
    "историю доступа и само согласие. Физически, без корзины и без возможности "
    "вернуть. Оплаченные дни при этом сгорают.\n\n"
    "Карточки товаров по артикулу будут работать как раньше: для них ничего "
    "подключать не нужно.\n\n"
    "Точно удаляем?"
)

DISCONNECT_DONE = (
    "Готово. По вашему кабинету в базе не осталось ни одной строки.\n"
    "Захотите вернуться, команда <code>/connect</code> начнёт всё сначала."
)

NOT_CONNECTED = (
    "Кабинет и не подключён: удалять нечего. "
    "Команда <code>/connect</code> расскажет, как подключить."
)

TOKEN_TROUBLE = (
    "⛔️ Wildberries перестал принимать ваш токен.\n\n"
    "Обычно это значит, что токен отозвали в кабинете или у него кончился срок. "
    "Пока он не работает, я поставил ваши модули на паузу: оплаченные дни "
    "не тратятся и дождутся нового токена.\n\n"
    "Выпустите новый токен и пришлите его командой <code>/connect</code>."
)


def find_token(text: str) -> str:
    """Достаёт токен из сообщения, даже если вокруг него написаны слова."""
    found = re.search(TOKEN_PATTERN, text or "")
    return found.group(0) if found else ""


def _client_id(update: Update, path) -> int | None:
    user = update.effective_user
    if user is None:
        return None
    return db.admin_repo(path).ensure_client(user.id)


def offer_line() -> str:
    """Строка про оферту. Пустая переменная это не повод молчать или выдумывать."""
    link = clients.offer_url()
    if not link:
        return OFFER_PENDING
    return f"Оферта, с которой вы соглашаетесь: {link}"


def instruction_text() -> str:
    """Пошаговая инструкция плюс строка про оферту."""
    return f"{INSTRUCTION}\n\n{offer_line()}"


def consent_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Согласен с офертой", callback_data=AGREE)]]
    )


def replace_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Заменить токен", callback_data=REPLACE)],
            [InlineKeyboardButton("Ничего не менять", callback_data=KEEP)],
        ]
    )


def wipe_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Да, удалить всё", callback_data=WIPE)],
            [InlineKeyboardButton("Отмена", callback_data=KEEP)],
        ]
    )


def category_line(name: str) -> str:
    """Название категории и что без неё не заработает. Карта живёт в конфиге."""
    section = config.settings().get("token_categories", {}).get(name, {})
    title = section.get("title") or wbapi.category_title(name)
    note = str(section.get("note") or "").strip()
    return f"• <b>{title}</b>: {note}" if note else f"• <b>{title}</b>"


def missing_text(missing: tuple[str, ...]) -> str:
    """Чего у токена нет и что из-за этого не работает. Пусто - пустая строка."""
    if not missing:
        return ""
    lines = ["⚠️ У токена не хватает категорий:"]
    lines += [category_line(name) for name in missing]
    lines.append(
        "Это поправимо: отметьте их в том же токене или выпустите новый "
        "и пришлите его сюда ещё раз."
    )
    return "\n".join(lines)


def forbidden_text(category: str) -> str:
    """403 это не пауза: сказать, какой категории нет, и работать дальше."""
    lines = [
        "⚠️ Wildberries не дал доступ к части данных: у токена нет нужной категории.",
    ]
    if category:
        lines.append(category_line(category))
    lines.append(
        "Оплаченные дни идут дальше, остальные модули работают как работали. "
        "Чтобы заработало и это, выпустите токен с недостающей категорией "
        "и пришлите его командой <code>/connect</code>."
    )
    return "\n\n".join(lines)


def modules_text(client_id: int, *, path=None, now: datetime | None = None) -> str:
    """Таблица модулей со статусами. Состояния считает core.access."""
    lines = ["<b>Ваши модули:</b>"]
    for item in access.status(client_id, now=now, path=path):
        lines.append(f"• {texts.module_title(item.module)}: {tariffs.state_words(item)}")
    return "\n".join(lines)


def confirm_text(
    result: clients.Connected, *, path=None, now: datetime | None = None
) -> str:
    """Подтверждение: кабинет, срок, тип токена, предупреждения, модули."""
    info = result.info
    blocks = [
        "✅ <b>Кабинет подключён.</b>",
        f"ID продавца: <code>{info.sid}</code>\n"
        f"Тип токена: {info.acc_title}.\n"
        f"Токен действует до {tariffs.local_date(info.expires_at)}, "
        f"это ещё {info.days_left(now)} дн.",
    ]
    if info.is_test:
        blocks.append(ACC_TEST)
    if int(info.acc) != clients.PERSONAL_ACC:
        blocks.append(ACC_WRONG.format(title=info.acc_title))
    if not info.read_only:
        blocks.append(READ_ONLY_WARNING)
    problem = missing_text(result.missing)
    if problem:
        blocks.append(problem)
    blocks.append(modules_text(result.client_id, path=path, now=now))
    return "\n\n".join(blocks)


def reminder_text(days: int) -> str:
    """Напоминание о сроке токена. Два раза за срок, а не каждый день."""
    return (
        f"⏳ Токену Wildberries осталось {days} дн.\n\n"
        "Когда он кончится, я перестану собирать отчёты, а оплаченные дни встанут "
        "на паузу и не сгорят. Чтобы этого не было, выпустите новый токен заранее: "
        "команда <code>/connect</code> напомнит, где в кабинете нажимать.\n"
        "Новый токен заменит старый, ничего перенастраивать не придётся."
    )


async def _reply(message, text: str, keyboard=None) -> None:
    await message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=keyboard,
    )


async def _forget(message) -> None:
    """Убирает сообщение с токеном из переписки. Молча, если не вышло.

    Telegram не всегда даёт удалить чужое сообщение, и это не повод срывать
    подключение: кабинет уже подключён, а клиент уже получил ответ.
    """
    try:
        await message.delete()
    except Exception:  # noqa: BLE001 - удалить чужое сообщение не всегда можно
        logger.info("сообщение с токеном осталось в переписке: удалить не дали")


async def connect_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, path=None
) -> None:
    """Команда /connect: инструкция, согласие, замена токена."""
    message = update.effective_message
    if message is None:
        return
    client_id = _client_id(update, path)
    if client_id is None:
        return
    if not crypto.key_available():
        await _reply(message, texts.TOKENS_DISABLED)
        return
    if clients.connected(client_id, path=path):
        await _reply(message, ALREADY, replace_keyboard())
        return
    if clients.has_consent(client_id, path=path):
        await _reply(message, f"{instruction_text()}\n\n{SEND_TOKEN}")
        return
    await _reply(message, instruction_text(), consent_keyboard())


async def token_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, path=None
) -> None:
    """Пришёл токен. Согласие проверяется до всего остального."""
    message = update.effective_message
    if message is None:
        return
    raw = find_token(message.text or "")
    client_id = _client_id(update, path)
    if client_id is None:
        return
    if not raw:
        # Удаляем и здесь: в переписке не место даже неудачной попытке,
        # человек мог ошибиться при копировании и прислать половину токена.
        await _reply(message, BAD_TOKEN_TEXT)
        await _forget(message)
        return

    if not clients.has_consent(client_id, path=path):
        refusal = NEED_CONSENT + "\n\n" + offer_line()
        await _reply(message, refusal, consent_keyboard())
        await _forget(message)
        return

    try:
        result = await clients.connect(client_id, raw, path=path)
    except wbapi.WBTokenFormatError:
        # Текст для селлера живёт здесь: сообщение чужого модуля может
        # поменяться, а читает его человек, а не программист.
        await _reply(message, BAD_TOKEN_TEXT)
    except clients.TokenExpired:
        await _reply(message, EXPIRED)
    except wbapi.WBAuthError:
        await _reply(message, REVOKED)
    except wbapi.WBUnavailable:
        await _reply(message, texts.WB_UNAVAILABLE)
    except (crypto.MissingKeyError, crypto.DecryptError):
        await _reply(message, texts.TOKENS_DISABLED)
    else:
        await _reply(message, confirm_text(result, path=path))
    await _forget(message)


async def agree_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, path=None
) -> None:
    """Кнопка согласия. Работает и тогда, когда ссылки на оферту ещё нет."""
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    client_id = _client_id(update, path)
    if client_id is None:
        return
    clients.record_consent(client_id, path=path)
    await _reply(query.message, f"{CONSENT_SAVED}\n\n{SEND_TOKEN}")


async def replace_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, path=None
) -> None:
    """Кнопка «Заменить токен»: тот же приём токена, второго кабинета не будет."""
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    await _reply(query.message, f"{instruction_text()}\n\n{SEND_TOKEN}")


async def keep_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, path=None
) -> None:
    """Кнопка «Ничего не менять» и «Отмена»."""
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    await _reply(query.message, KEEP_TEXT)


async def disconnect_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, path=None
) -> None:
    """Команда /disconnect: сначала вопрос, удаление только по кнопке."""
    message = update.effective_message
    if message is None:
        return
    client_id = _client_id(update, path)
    if client_id is None:
        return
    if not clients.connected(client_id, path=path):
        await _reply(message, NOT_CONNECTED)
        return
    await _reply(message, DISCONNECT_ASK, wipe_keyboard())


async def wipe_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, path=None
) -> None:
    """Кнопка подтверждения: удаляет всё, физически и без корзины."""
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    client_id = _client_id(update, path)
    if client_id is None:
        return
    try:
        removed = clients.disconnect(client_id, path=path)
    except clients.DisconnectIncomplete:
        # Ядро пересчитало строки после удаления и что-то нашло. Обещать
        # клиенту пустоту в таком случае нельзя, владелец уже видит ошибку.
        await _reply(query.message, texts.SOMETHING_WENT_WRONG)
        return
    audit.log(
        "disconnect",
        None,
        f"Клиент отключил кабинет, удалено строк по таблицам: {sum(removed.values())}.",
        path=path,
    )
    await _reply(query.message, DISCONNECT_DONE)


async def _send(app, client_id: int, text: str, path=None) -> None:
    """Пишет клиенту в его чат. Молчание Telegram не должно ронять работу."""
    row = db.admin_repo(path).client(client_id)
    if row is None:
        logger.warning("некому написать: клиента %s нет в базе", client_id)
        return
    try:
        await app.bot.send_message(
            chat_id=int(row["telegram_id"]), text=text, parse_mode=ParseMode.HTML
        )
    except Exception:  # noqa: BLE001 - молчание Telegram не наша авария
        logger.exception("не удалось написать клиенту %s", client_id)


def make_reminder(app, *, path=None, now: datetime | None = None):
    """Утренняя работа: напомнить тем, у кого кончается срок токена.

    Кого именно, решает core.clients: порог перейдён и об этом ещё не
    говорили. Отметка ставится после отправки, поэтому второй запуск подряд
    не пишет то же самое дважды, а пропущенный день не теряет напоминание.
    """

    async def job(task) -> None:
        for client_id, left, threshold in clients.due_reminders(now=now, path=path):
            await _send(app, client_id, reminder_text(left), path=path)
            clients.mark_reminded(client_id, threshold, path=path)

    return job


def make_auth_notice(app, *, path=None):
    """Что делает бот, когда WB отклонил токен в фоновой задаче.

    401 это пауза: считать нечем, и оплаченные дни не должны сгорать.
    403 паузой не является, поэтому здесь он только объясняется словами.
    """

    async def notice(task, error: BaseException) -> None:
        client_id = getattr(task, "client_id", None)
        if client_id is None:
            return
        trouble = clients.on_wb_error(client_id, error, path=path)
        if trouble.kind == "forbidden":
            await _send(app, client_id, forbidden_text(trouble.category), path=path)
            return
        await _send(app, client_id, TOKEN_TROUBLE, path=path)

    return notice


def _bound(handler, path):
    """Путь к базе приходит параметром, как договорено для всех хендлеров."""
    return handler if path is None else functools.partial(handler, path=path)


def register(app, *, path=None) -> None:
    """Сам себя регистрирует: bot/app.py никто не трогает."""
    app.add_handler(CommandHandler("connect", _bound(connect_command, path)))
    app.add_handler(CommandHandler("disconnect", _bound(disconnect_command, path)))
    app.add_handler(
        CallbackQueryHandler(_bound(agree_callback, path), pattern=f"^{AGREE}$")
    )
    app.add_handler(
        CallbackQueryHandler(_bound(replace_callback, path), pattern=f"^{REPLACE}$")
    )
    app.add_handler(
        CallbackQueryHandler(_bound(keep_callback, path), pattern=f"^{KEEP}$")
    )
    app.add_handler(
        CallbackQueryHandler(_bound(wipe_callback, path), pattern=f"^{WIPE}$")
    )
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.Regex(TOKEN_PATTERN),
            _bound(token_message, path),
        )
    )
    # Напоминания о сроке токена идут через расписание таска 03, а не своим
    # будильником: один источник времени на весь проект.
    scheduler.register_daily(REMINDER_JOB, make_reminder(app, path=path))
    # Пауза при 401 живёт здесь же, рядом с подключением: снимать её будет
    # тот же обмен токена.
    queue.set_auth_handler(make_auth_notice(app, path=path))
