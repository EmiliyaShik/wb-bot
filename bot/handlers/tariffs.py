"""Тарифы, пробный период и заготовка для платных команд.

Здесь живёт витрина: /tariffs, /trial и общая обёртка require_module, которой
пользуются все платные команды. Обёртка нужна, чтобы команда без доступа
отвечала не сухим отказом, а объяснением: что даёт модуль, сколько стоит и
кнопкой «Оформить».

Цен и сроков в этом файле нет ни одного: всё приходит из core.config, а
состояние доступа из core.access.
"""

from __future__ import annotations

import functools
import logging
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from core import access, config, db, scheduler

logger = logging.getLogger(__name__)

BUY_PREFIX = "buy:"
TRIAL_PREFIX = "trial:"
LIST_TOKEN = "__list__"

# Запасной ответ на «Оформить» стоит в дальней группе: когда появится диалог
# счёта, он вызовет set_buy_dialog, и до заглушки дело не дойдёт.
BUY_GROUP = 50

HEAD = "💼 <b>Тарифы WBРентген</b>"

FOOT = (
    "Оплата по счёту для ИП и ООО: нажмите «Оформить» у нужного модуля.\n"
    "Вопрос по оплате - команда <code>/paysupport</code>."
)

TRIAL_INVITE = "Хотите сначала попробовать? Команда <code>/trial</code>."

BUY_SOON = (
    "Выставление счёта ещё готовится, оно откроется совсем скоро.\n"
    "Напишите владельцу командой <code>/paysupport</code>, и счёт выставят вручную."
)

TRIAL_PICK = "Выберите один модуль: он будет работать бесплатно {days} дн."

TRIAL_DENIED = {
    "no_seller": (
        "Пробный период привязан к кабинету Wildberries, а он ещё не подключён. "
        "Сначала команда <code>/connect</code>."
    ),
    "used": (
        "Пробный период на этот кабинет уже брали. Он даётся один раз, "
        "даже если зайти с другого аккаунта Telegram. "
        "Посмотрите цены командой <code>/tariffs</code>."
    ),
    "not_sold": "Этот модуль пока не продаётся.",
    "too_many": "Пробный период даётся только на один модуль.",
}

STATE_WORDS = {
    access.ACTIVE: "активен до {date}",
    access.GRACE: "льготные дни, до {date} всё работает",
    access.PAUSED: "на паузе, оплаченные дни не тратятся",
    access.OFF: "не подключён",
    access.HIDDEN: "скрыт",
}

# Диалог выставления счёта приходит из другого таска. Пока его нет, кнопка
# «Оформить» отвечает понятным текстом, а не тишиной.
_buy_dialog: Callable | None = None


def set_buy_dialog(callback: Callable | None) -> None:
    """Точка расширения: сюда таск со счетами кладёт свой обработчик кнопки.

    Вызывается из register(app) того модуля. Обработчик получает те же
    аргументы, что и любой хендлер PTB, плюс имя модуля третьим:
    (update, context, module).
    """
    global _buy_dialog
    _buy_dialog = callback


def buy_dialog() -> Callable | None:
    """Кто сейчас обслуживает кнопку «Оформить». None - ещё никто."""
    return _buy_dialog


def rubles(amount: Any) -> str:
    """Деньги строкой: округление до рубля, разряды неразрывным пробелом.

    Округление, а не отбрасывание: цена со скидкой приходит Decimal, и 2672.50
    в витрине должно совпасть с 2673 в счёте. Иначе клиент видит одну сумму,
    а платит другую.
    """
    value = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    whole = int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return f"{whole:,}".replace(",", " ") + " ₽"


def local_date(moment: datetime | None) -> str:
    """Дата в часовом поясе проекта. Пояс один на всех: core.scheduler.tz()."""
    if moment is None:
        return "-"
    return moment.astimezone(scheduler.tz()).strftime("%d.%m.%Y")


def price_line(module: str) -> str:
    """Цены по всем периодам конфига, со скидками оттуда же."""
    parts = []
    for months in config.periods():
        amount = rubles(config.price_decimal(module, months))
        percent = config.discount_percent(months)
        tail = f" (скидка {percent}%)" if percent > 0 else ""
        parts.append(f"{months} мес. {amount}{tail}")
    return "\n".join(parts)


def what_it_gives(module: str) -> str:
    """Что даёт модуль, словами. Строка живёт в конфиге рядом с [modules.*].

    В коде её нет намеренно: эту же строку показывает бесплатная диагностика,
    и переписывать её пришлось бы в двух местах.
    """
    info = config.modules().get(module)
    return (info.gives if info else "").strip()


def state_words(item: access.Access) -> str:
    """Состояние модуля словами, с датой окончания."""
    template = STATE_WORDS.get(item.state, STATE_WORDS[access.OFF])
    return template.format(date=local_date(item.until))


def tariffs_text(
    client_id: int | None = None,
    *,
    now: datetime | None = None,
    path: str | Path | None = None,
) -> str:
    """Витрина: только видимые модули, цены из конфига и текущий статус клиента."""
    current: dict[str, access.Access] = {}
    if client_id is not None:
        current = {
            item.module: item for item in access.status(client_id, now=now, path=path)
        }
    blocks = [HEAD]
    for name, info in config.visible_modules().items():
        title = info.title or name
        block = [f"<b>{title}</b>", price_line(name)]
        gives = what_it_gives(name)
        if gives:
            block.append(f"Что входит: {gives}.")
        item = current.get(name)
        if item is not None:
            block.append(f"Сейчас: {state_words(item)}.")
        blocks.append("\n".join(block))
    blocks.append(FOOT)
    blocks.append(TRIAL_INVITE)
    return "\n\n".join(blocks)


def offer_text(module: str) -> str:
    """Ответ платной команды без доступа: что даёт модуль и сколько стоит."""
    info = config.modules().get(module)
    title = (info.title if info and info.title else module) or module
    lines = [f"🔒 Модуль <b>{title}</b> пока не подключён.", ""]
    gives = what_it_gives(module)
    if gives:
        lines += [f"Что он даёт: {gives}.", ""]
    lines += [price_line(module), "", TRIAL_INVITE]
    return "\n".join(lines)


def offer_keyboard(module: str) -> InlineKeyboardMarkup:
    """Кнопка «Оформить» и ссылка на полную витрину."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Оформить", callback_data=f"{BUY_PREFIX}{module}")],
            [
                InlineKeyboardButton(
                    "Все тарифы", callback_data=f"{BUY_PREFIX}{LIST_TOKEN}"
                )
            ],
        ]
    )


def trial_keyboard() -> InlineKeyboardMarkup:
    """Выбор одного модуля для пробного периода. Пакеты пробно не отдаём."""
    rows = [
        [InlineKeyboardButton(info.title or name, callback_data=f"{TRIAL_PREFIX}{name}")]
        for name, info in config.visible_modules().items()
        if name not in access.packages()
    ]
    return InlineKeyboardMarkup(rows)


def client_id_of(update: Update, path: str | Path | None = None) -> int | None:
    """Внутренний id клиента по Telegram ID. Без пользователя доступа нет."""
    user = update.effective_user
    if user is None:
        return None
    return db.admin_repo(path).ensure_client(user.id)


async def send_offer(update: Update, module: str) -> None:
    """Показывает предложение вместо отказа."""
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(
        offer_text(module),
        parse_mode=ParseMode.HTML,
        reply_markup=offer_keyboard(module),
    )


def require_module(module: str, *, path: str | Path | None = None):
    """Обёртка платной команды: нет доступа - предложение вместо отказа.

    Применяется так:

        @require_module("finance")
        async def profit(update, context): ...

    Хендлер получает управление только при работающем доступе, включая
    льготные дни и паузу. Иначе клиент видит, что даёт модуль, сколько он
    стоит, и кнопку «Оформить». Параметр path нужен тестам: в работе путь к
    базе берётся из конфига.
    """

    def decorator(handler):
        @functools.wraps(handler)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *rest):
            client_id = client_id_of(update, path)
            if client_id is None:
                return None
            if access.has_access(client_id, module, path=path):
                return await handler(update, context, *rest)
            await send_offer(update, module)
            return None

        wrapper.required_module = module
        return wrapper

    return decorator


async def tariffs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(
        tariffs_text(client_id_of(update)),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def trial_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(
        TRIAL_PICK.format(days=access.trial_days()),
        parse_mode=ParseMode.HTML,
        reply_markup=trial_keyboard(),
    )


def granted_text(item: access.Access) -> str:
    """Сообщение о включении. Дата окончания названа обязательно."""
    info = config.modules().get(item.module)
    title = (info.title if info and info.title else item.module) or item.module
    return (
        f"✅ Модуль <b>{title}</b> включён.\n"
        f"Работает до {local_date(item.until)} включительно."
    )


async def buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка «Оформить». Пока диалога счёта нет, отвечает понятной заглушкой."""
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    module = (query.data or "").removeprefix(BUY_PREFIX)
    if module == LIST_TOKEN:
        await query.message.reply_text(
            tariffs_text(client_id_of(update)), parse_mode=ParseMode.HTML
        )
        return
    dialog = buy_dialog()
    if dialog is not None:
        await dialog(update, context, module)
        return
    await query.message.reply_text(BUY_SOON, parse_mode=ParseMode.HTML)


async def trial_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Выбор модуля для пробного периода."""
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    module = (query.data or "").removeprefix(TRIAL_PREFIX)
    client_id = client_id_of(update)
    if client_id is None:
        return
    try:
        granted = access.start_trial(client_id, module)
    except access.TrialDenied as denied:
        await query.message.reply_text(
            TRIAL_DENIED.get(denied.reason, TRIAL_DENIED["not_sold"]),
            parse_mode=ParseMode.HTML,
        )
        return
    await query.message.reply_text(granted_text(granted), parse_mode=ParseMode.HTML)


def register(app) -> None:
    """Сам себя регистрирует: bot/app.py никто не трогает."""
    app.add_handler(CommandHandler("tariffs", tariffs_command))
    app.add_handler(CommandHandler("modules", tariffs_command))
    app.add_handler(CommandHandler("trial", trial_command))
    app.add_handler(CallbackQueryHandler(trial_callback, pattern=f"^{TRIAL_PREFIX}"))
    app.add_handler(
        CallbackQueryHandler(buy_callback, pattern=f"^{BUY_PREFIX}"), group=BUY_GROUP
    )
