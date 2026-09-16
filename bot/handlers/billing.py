"""Счета в боте: модуль, период, ИНН, PDF, кнопка владельца «Оплачен».

Как это устроено.

Кнопка «Оформить» на витрине тарифов приводит сюда: таск с тарифами оставил
для этого точку `tariffs.set_buy_dialog`, и `register(app)` в неё встаёт.
Дальше диалог идёт кнопками (период, оплата картой), а ИНН и реквизиты
приходят обычным текстом.

Текстовый шаг зарегистрирован в отдельной, более ранней группе и
останавливает цепочку только тогда, когда диалог правда идёт. Иначе он молча
пропускает сообщение дальше, и артикул товара попадает к старому обработчику,
как и раньше.

Доступ здесь не включается: кнопка «Оплачен» зовёт `core.billing.mark_paid`,
а та - `core.access.grant_access` с номером счёта в `payment_ref`. Оттуда и
берётся идемпотентность: второе нажатие приходит с тем же номером и ничего
не продлевает.

Реквизитов и цен в этом файле нет. Пока переменные SELLER_* пусты, счёт не
формируется: клиент получает вежливое «счёт готовится», а владелец список
ровно тех переменных, которых не хватает.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot.handlers import tariffs
from core import audit, billing, config, db, scheduler
from core.billing import counterparty
from core.billing import pdf as invoice_pdf

logger = logging.getLogger(__name__)

PREFIX = "inv:"
PAY_PREFIX = "invpay:"

# Ключ диалога в user_data.
STATE = "invoice_dialog"

# Текстовый шаг стоит раньше обычных хендлеров, иначе запасной обработчик
# артикула перехватил бы ИНН. Цепочку он останавливает только в диалоге.
DIALOG_GROUP = -10

# Имя ежедневной работы в расписании.
DAILY_NAME = "invoices_overdue"

# --- тексты. Только русский, длинных тире нет ---

PICK_PERIOD = (
    "Модуль <b>{title}</b>. Выберите срок: чем длиннее, тем дешевле месяц."
)

ASK_INN = (
    "Счёт выставляется на организацию или ИП. Пришлите ИНН одним сообщением: "
    "10 цифр для компании, 12 для ИП.\n\n"
    "Если платить будете картой или через СБП, нажмите кнопку ниже."
)

BAD_INN = (
    "Это не похоже на ИНН: проверьте цифры и пришлите ещё раз. "
    "Должно быть 10 цифр для компании или 12 для ИП."
)

FOUND = (
    "Нашёл: <b>{name}</b>\n{address}\n\n"
    "Если всё верно, нажмите «Всё верно». Если нет, пришлите наименование "
    "текстом."
)

ASK_NAME = (
    "Реквизиты по этому ИНН подтянуть не удалось, это не страшно. "
    "Пришлите наименование организации или ИП одной строкой, как в документах."
)

ASK_ADDRESS = "Теперь адрес одной строкой: город, улица, дом."

READY = (
    "Счёт <b>{number}</b> на {amount}.\n"
    "Оплатить до {due} включительно, это {days} банковских дней.\n\n"
    "Как только деньги придут, я включу модуль и напишу вам. "
    "Вопрос по оплате - команда <code>/paysupport</code>."
)

WAIT_FOR_DETAILS = (
    "Счёт готовится: у сервиса пока не заполнены платёжные реквизиты. "
    "Я передал вашу заявку владельцу, он свяжется с вами и выставит счёт вручную.\n\n"
    "Ничего делать не нужно, заявка уже у него."
)

CANCELLED = "Хорошо, счёт не выставляю. Витрина тарифов - команда <code>/tariffs</code>."

PAYSUPPORT = (
    "💬 <b>Вопрос по оплате</b>\n\n"
    "Напишите владельцу сервиса: {contact}\n"
    "Он ответит по счёту, оплате картой и возврату."
)

PAYSUPPORT_EMPTY = (
    "💬 <b>Вопрос по оплате</b>\n\n"
    "Контакт для связи ещё готовится. Напишите сюда, в этот чат: "
    "я передам сообщение владельцу сервиса."
)

CARD = (
    "Картой или через СБП оплата идёт напрямую владельцу сервиса: {contact}\n"
    "Напишите ему, он подскажет, как перевести и включит модуль."
)

CARD_EMPTY = (
    "Оплата картой и через СБП пока настраивается. Напишите сюда, в этот чат, "
    "и владелец сервиса свяжется с вами."
)

OVERDUE_NOTE = (
    "Счёт {number} на {amount} просрочен: срок оплаты был до {due}.\n"
    "Если он ещё нужен, выставлю новый: команда <code>/tariffs</code>."
)

PAID_DONE = "Счёт {number} проведён, модуль включён, клиенту написал."
PAID_DUPLICATE = "Счёт {number} уже проводили, доступ не продлён."
PAID_UNKNOWN = "Счёта {number} в базе нет."

# --- тексты владельцу ---

OWNER_NEW = (
    "🧾 <b>Новый счёт {number}</b>\n"
    "Клиент: {client}\n"
    "Модуль: {title}, {period}\n"
    "Сумма: {amount}\n"
    "Плательщик: {payer}\n"
    "Оплатить до: {due}\n\n"
    "Проверьте выписку и нажмите «Оплачен»."
)

OWNER_NO_DETAILS = (
    "⚠️ <b>Счёт не выставлен</b>\n"
    "Клиент {client} хотел оплатить: {title}, {period}, {amount}.\n\n"
    "Не заданы переменные окружения:\n{missing}\n\n"
    "Пока они пусты, бот счета не формирует и ничего не выдумывает. "
    "Заполните их и попросите клиента повторить, либо выставьте счёт вручную."
)

OWNER_NO_VAT = (
    "ℹ️ В конфиге пуст ключ <code>invoice.vat_note</code>: налоговый режим "
    "в счёте не указан. Заполните его, когда бухгалтер подскажет формулировку."
)

OWNER_NO_FONT = (
    "⚠️ <b>Счёт {number} не нарисован</b>\n{hint}\n"
    "Номер уже занят, счёт лежит в базе: отправьте его клиенту вручную."
)


# --- вспомогательное ---


def owner_contact() -> str:
    return billing.owner_contact()


def paysupport_text() -> str:
    """Ответ на /paysupport. Контакта нет - не выдумываем его."""
    contact = owner_contact()
    return PAYSUPPORT.format(contact=contact) if contact else PAYSUPPORT_EMPTY


def card_text() -> str:
    """Оплата картой или СБП. Бот про кассу не знает ничего."""
    contact = owner_contact()
    return CARD.format(contact=contact) if contact else CARD_EMPTY


def module_title(module: str) -> str:
    info = config.modules().get(module)
    return (info.title if info and info.title else module) or module


def period_keyboard(module: str) -> InlineKeyboardMarkup:
    """Сроки и суммы со скидкой. И то и другое из конфига."""
    rows = []
    for months in config.periods():
        amount = tariffs.rubles(config.price_decimal(module, months))
        percent = config.discount_percent(months)
        tail = f", скидка {percent}%" if percent > 0 else ""
        rows.append(
            [
                InlineKeyboardButton(
                    f"{billing.months_words(months)} - {amount}{tail}",
                    callback_data=f"{PREFIX}p:{module}:{months}",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton("Картой или СБП", callback_data=f"{PREFIX}card")]
    )
    rows.append([InlineKeyboardButton("Отмена", callback_data=f"{PREFIX}cancel")])
    return InlineKeyboardMarkup(rows)


def inn_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Картой или СБП", callback_data=f"{PREFIX}card")],
            [InlineKeyboardButton("Отмена", callback_data=f"{PREFIX}cancel")],
        ]
    )


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Всё верно", callback_data=f"{PREFIX}ok")],
            [InlineKeyboardButton("Отмена", callback_data=f"{PREFIX}cancel")],
        ]
    )


def paid_keyboard(number: str) -> InlineKeyboardMarkup:
    """Кнопка владельца. Номер счёта в ней и есть будущий payment_ref."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Оплачен", callback_data=f"{PAY_PREFIX}{number}")]]
    )


def owner_new_text(item: billing.Invoice) -> str:
    """Уведомление владельцу: кто, что, сколько, номер.

    «Кто» это внутренний id, как везде в админке, а не Telegram ID. Наружу не
    идут ни имена, ни аккаунты (R159), и одно исключение ради удобства
    сделало бы правило необязательным. Выдать доступ по этому числу владелец
    может: команды понимают внутренний id.
    """
    return OWNER_NEW.format(
        number=item.number,
        client=f"клиент #{item.client_id}",
        title=module_title(item.module),
        period=billing.months_words(item.period_months),
        amount=tariffs.rubles(item.amount),
        payer=item.customer or "не указан",
        due=item.due_at.strftime("%d.%m.%Y") if item.due_at else "-",
    )


def owner_no_details_text(
    module: str, months: int, client_id: int, missing: tuple[str, ...]
) -> str:
    """Список ровно тех переменных, которых не хватает.

    Клиент назван внутренним id по той же причине, что и в уведомлении о
    счёте: в админке Telegram-аккаунтов не показывают.
    """
    return OWNER_NO_DETAILS.format(
        client=f"клиент #{client_id}",
        title=module_title(module),
        period=billing.months_words(months),
        amount=tariffs.rubles(config.price_decimal(module, months)),
        missing="\n".join(f"<code>{name}</code>" for name in missing),
    )


async def tell_owner(bot: Any, text: str, reply_markup: Any = None) -> None:
    """Пишет всем владельцам. Пустой ADMIN_TELEGRAM_IDS это не авария."""
    ids = config.admin_ids()
    if not ids:
        audit.log("billing", None, "некому сообщить о счёте: ADMIN_TELEGRAM_IDS пуст",
                  level="warning")
        return
    extra = {"reply_markup": reply_markup} if reply_markup is not None else {}
    for chat_id in ids:
        try:
            await bot.send_message(
                chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, **extra
            )
        except Exception:  # noqa: BLE001 - молчание Telegram не наша авария
            logger.exception("не удалось сообщить владельцу %s", chat_id)


async def tell_client(bot: Any, client_id: int, text: str, **kwargs: Any) -> None:
    """Пишет клиенту по внутреннему id."""
    row = db.admin_repo().client(client_id)
    if row is None:
        logger.warning("некому написать: клиента %s нет в базе", client_id)
        return
    try:
        await bot.send_message(
            chat_id=int(row["telegram_id"]), text=text, parse_mode=ParseMode.HTML, **kwargs
        )
    except Exception:  # noqa: BLE001
        logger.exception("не удалось написать клиенту %s", client_id)


# --- диалог ---


async def start_dialog(
    update: Update, context: ContextTypes.DEFAULT_TYPE, module: str
) -> None:
    """Точка входа с витрины тарифов: выбран модуль, спрашиваем срок."""
    message = update.effective_message
    if message is None:
        return
    if module not in config.visible_modules():
        await message.reply_text(tariffs.tariffs_text(), parse_mode=ParseMode.HTML)
        return
    if context is not None and context.user_data is not None:
        context.user_data[STATE] = {"module": module, "step": "period"}
    await message.reply_text(
        PICK_PERIOD.format(title=module_title(module)),
        parse_mode=ParseMode.HTML,
        reply_markup=period_keyboard(module),
    )


async def period_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопки диалога: срок, оплата картой, подтверждение, отмена."""
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    data = (query.data or "").removeprefix(PREFIX)
    state = context.user_data.get(STATE, {}) if context.user_data is not None else {}

    if data == "card":
        await query.message.reply_text(card_text(), parse_mode=ParseMode.HTML)
        return

    if data == "cancel":
        if context.user_data is not None:
            context.user_data.pop(STATE, None)
        await query.message.reply_text(CANCELLED, parse_mode=ParseMode.HTML)
        return

    if data == "ok":
        if not state.get("inn"):
            return
        await _issue(update, context, state)
        return

    if data.startswith("p:"):
        _, module, raw = data.split(":", 2)
        if module not in config.visible_modules() or int(raw) not in config.periods():
            return
        if context.user_data is not None:
            context.user_data[STATE] = {
                "module": module,
                "months": int(raw),
                "step": "inn",
            }
        amount = tariffs.rubles(config.price_decimal(module, int(raw)))
        await query.message.reply_text(
            f"{module_title(module)}, {billing.months_words(int(raw))}: {amount}.\n\n"
            + ASK_INN,
            parse_mode=ParseMode.HTML,
            reply_markup=inn_keyboard(),
        )


async def text_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Текстовые шаги диалога. Вне диалога молчит и пропускает сообщение дальше.

    Остановка цепочки тут не украшение: без неё запасной обработчик артикула
    из старого бота ответил бы на ИНН «товар не найден», а с ней он получает
    все сообщения, кроме тех, что бот сам попросил.
    """
    if context is None or context.user_data is None:
        return
    state = context.user_data.get(STATE)
    if not state or state.get("step") not in ("inn", "name", "address"):
        return

    message = update.effective_message
    text = (getattr(message, "text", "") or "").strip()
    if not text:
        return

    step = state["step"]
    if step == "inn":
        if not counterparty.inn_is_valid(text):
            await message.reply_text(BAD_INN, parse_mode=ParseMode.HTML)
            raise ApplicationHandlerStop
        state["inn"] = counterparty.normalize(text)
        found = await counterparty.lookup(state["inn"])
        if found is not None and found.name:
            state.update(
                {"name": found.name, "address": found.address, "step": "confirm"}
            )
            await message.reply_text(
                FOUND.format(name=found.name, address=found.address or ""),
                parse_mode=ParseMode.HTML,
                reply_markup=confirm_keyboard(),
            )
            raise ApplicationHandlerStop
        state["step"] = "name"
        await message.reply_text(ASK_NAME, parse_mode=ParseMode.HTML)
        raise ApplicationHandlerStop

    if step == "name":
        state["name"] = text
        state["step"] = "address"
        await message.reply_text(ASK_ADDRESS, parse_mode=ParseMode.HTML)
        raise ApplicationHandlerStop

    state["address"] = text
    await _issue(update, context, state)
    raise ApplicationHandlerStop


async def _issue(update: Update, context: ContextTypes.DEFAULT_TYPE, state: dict) -> None:
    """Выставляет счёт, отдаёт PDF клиенту и сообщает владельцу.

    Пустые реквизиты обрабатываются тут же и до номера: клиент получает
    вежливое ожидание, владелец точный список переменных. Ничего похожего на
    реквизиты бот не подставляет.
    """
    message = update.effective_message
    bot = getattr(context, "bot", None)
    client_id = tariffs.client_id_of(update)
    if client_id is None or message is None:
        return
    module = state["module"]
    months = int(state["months"])

    try:
        item = billing.create_invoice(
            client_id,
            module,
            months,
            inn=state.get("inn", ""),
            org_name=state.get("name", ""),
            org_address=state.get("address", ""),
        )
    except billing.DetailsMissing as gap:
        if context.user_data is not None:
            context.user_data.pop(STATE, None)
        await message.reply_text(WAIT_FOR_DETAILS, parse_mode=ParseMode.HTML)
        await tell_owner(
            bot,
            owner_no_details_text(module, months, client_id, gap.missing),
        )
        audit.log(
            "invoice.blocked",
            client_id,
            "счёт не выставлен, пустые переменные: " + ", ".join(gap.missing),
            level="warning",
        )
        return

    if context.user_data is not None:
        context.user_data.pop(STATE, None)

    await message.reply_text(
        READY.format(
            number=item.number,
            amount=tariffs.rubles(item.amount),
            due=item.due_at.strftime("%d.%m.%Y") if item.due_at else "-",
            days=_bank_days(),
        ),
        parse_mode=ParseMode.HTML,
    )

    try:
        data = invoice_pdf.build(item)
        await message.reply_document(
            document=data,
            filename=invoice_pdf.file_name(item),
            caption=f"Счёт {item.number}",
        )
    except invoice_pdf.FontMissing as gap:
        await tell_owner(
            bot, OWNER_NO_FONT.format(number=item.number, hint=str(gap))
        )
    except billing.DetailsMissing as gap:
        await tell_owner(
            bot,
            owner_no_details_text(module, months, client_id, gap.missing),
        )

    # Уведомление и кнопка одним сообщением: владелец видит, за что жмёт.
    await tell_owner(
        bot,
        owner_new_text(item),
        reply_markup=paid_keyboard(item.number),
    )
    if billing.vat_note_missing():
        await tell_owner(bot, OWNER_NO_VAT)


def _bank_days() -> int:
    from core.billing import bankdays

    return bankdays.valid_bank_days()


# --- кнопка владельца ---


async def paid_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«Оплачен»: включает доступ через grant_access и пишет клиенту.

    Повторное нажатие ничего не продлевает: payment_ref это номер счёта, и
    дверь доступа узнаёт дубль сама.
    """
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    user = update.effective_user
    if user is None or not config.is_admin(user.id):
        return
    number = (query.data or "").removeprefix(PAY_PREFIX).strip()
    bot = getattr(context, "bot", None)

    try:
        result = billing.mark_paid(number, actor=str(user.id))
    except KeyError:
        await query.message.reply_text(PAID_UNKNOWN.format(number=number))
        return

    if result.duplicate:
        await query.message.reply_text(PAID_DUPLICATE.format(number=number))
        return

    await query.message.reply_text(PAID_DONE.format(number=number))
    if result.granted is not None:
        await tell_client(bot, result.invoice.client_id, tariffs.granted_text(result.granted))


async def paysupport_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(
        paysupport_text(), parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


# --- ежедневная проверка ---


def make_overdue_job(app: Any):
    """Ежедневная работа: перевести просроченные счета и напомнить клиентам.

    Напоминание уходит ровно один раз: `expire_overdue` возвращает только те
    счета, которые сменили статус именно сейчас.
    """

    async def job(task: Any) -> None:
        payload = getattr(task, "payload", None) or {}
        now = _day_start(payload.get("date"))
        for item in billing.expire_overdue(now=now):
            await tell_client(
                app.bot,
                item.client_id,
                OVERDUE_NOTE.format(
                    number=item.number,
                    amount=tariffs.rubles(item.amount),
                    due=item.due_at.strftime("%d.%m.%Y") if item.due_at else "-",
                ),
            )

    return job


def _day_start(day: str | None) -> datetime | None:
    """Дата из расписания в момент времени. Нет даты - значит сейчас."""
    if not day:
        return None
    try:
        return datetime.strptime(str(day)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def register(app) -> None:
    """Сам себя регистрирует: bot/app.py никто не трогает."""
    tariffs.set_buy_dialog(start_dialog)
    app.add_handler(CommandHandler("paysupport", paysupport_command))
    app.add_handler(CallbackQueryHandler(period_callback, pattern=f"^{PREFIX}"))
    app.add_handler(CallbackQueryHandler(paid_callback, pattern=f"^{PAY_PREFIX}"))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_step), group=DIALOG_GROUP
    )
    scheduler.register_daily(DAILY_NAME, make_overdue_job(app))
