"""Выбор периода отчёта: кнопки месяцев и ручной ввод дат.

Зачем это есть. Относительные кнопки («последние 7 дней» и остальные)
отсчитывают дни назад от сегодня и берут каждую неделю Wildberries, которая
отрезок хотя бы задела. Для разговора «что выросло» этого достаточно, а для
бухгалтерии нет: «Месяц» в середине октября это не сентябрь, и свести такой
отчёт с декларацией нельзя. Поэтому рядом живут кнопки названных месяцев и
ручной период, и считаются они точно по датам операций.

Кнопка месяца и ручной ввод это одно и то же, просто у первого есть имя:
границы обоих проверяет `agents.finance.period_of`, а не клавиатура.
`callback_data` приходит от клиента и подделывается свободно, поэтому
нарисованные кнопки ничего не гарантируют.

Как разведён текстовый шаг. Диалог счёта (`bot.handlers.billing`) стоит в
группе -10 и останавливает цепочку только тогда, когда правда идёт. Ввод дат
стоит следом, в группе -9, и ведёт себя так же: вне диалога молчит и
пропускает сообщение дальше, в диалоге отвечает и цепочку останавливает.
Отсюда и порядок: человек, который вводит ИНН для счёта, до ввода дат не
доходит вовсе, а человек, который вводит даты, в диалог счёта не попадает,
потому что тот до своей остановки не доходит. Группа у каждого своя
намеренно: в одной группе телеграм отдаёт сообщение только первому
подходящему хендлеру, и второй текстовый шаг не получил бы его никогда.

Своей команды у этого файла нет: он приставка к `/finance`, `/profit` и
соседям. Поэтому в меню команд Telegram он не попадает, а `register(app)` у
него есть ради одного текстового шага и кнопки «Отмена», общих на всех.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Awaitable, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from agents import finance
from bot.texts import Safe, fill

logger = logging.getLogger(__name__)

# Хвосты callback_data, общие для всех отчётов. Сам префикс у каждого свой
# («fin:», «profit:»), чтобы кнопка приводила в свой же хендлер.
CUSTOM = "custom"
MARK = "d:"

PREFIX = "per:"
CANCEL = f"{PREFIX}cancel"

# Ключ диалога в user_data. Один на все отчёты: человек вводит даты для того
# отчёта, который спросил последним, а двух таких вводов сразу не бывает.
STATE = "period_dialog"

# Текстовый шаг стоит после диалога счёта (-10) и до обычных хендлеров.
# Цепочку останавливает только в диалоге, иначе артикул товара перестал бы
# доходить до запасного обработчика.
DIALOG_GROUP = -9

CUSTOM_BUTTON = "Свои даты"

ASK_DATES = (
    "Напишите период двумя датами, одним сообщением. Например:\n"
    "<code>{example}</code>\n\n"
    "Посчитаю точно по датам операций: в отчёт попадёт ровно то, что вы "
    "попросили, без лишних дней соседнего месяца.\n\n"
    "Данные отчёта о реализации начинаются {since}, предел периода {limit}."
)

BAD_DATES = {
    finance.BAD_FORMAT: (
        "Не разобрал даты. Нужны две: начало и конец, одним сообщением. "
        "Например: <code>{example}</code>"
    ),
    finance.BAD_ORDER: (
        "Начало периода позже его конца. Сначала начало, потом конец: "
        "<code>{example}</code>"
    ),
    finance.TOO_EARLY: (
        "Так далеко данных нет: отчёты о реализации Wildberries отдаёт "
        "с {since}. Возьмите период позже."
    ),
    finance.TOO_LATE: (
        "Начало периода ещё не наступило. Будущих продаж у Wildberries нет, "
        "возьмите период до сегодняшнего дня."
    ),
    finance.TOO_LONG: (
        "Период слишком длинный: предел {limit}. Отчёт о реализации "
        "Wildberries отдаёт по одной странице в минуту, поэтому за раз "
        "считается период покороче. Разбейте его на части."
    ),
}

CANCELLED = "Хорошо, даты не спрашиваю. Период можно выбрать кнопкой."

# Даты разобраны, а отчёта, который их просил, в боте нет. Случается это
# только при сбое сборки (отчёт не зарегистрировался), и молчать нельзя:
# человек прислал период и ждёт ответа.
NO_OWNER = (
    "Не получилось принять период: отчёт, который его просил, сейчас не "
    "работает. Владелец уже видит это в журнале. Выберите период кнопкой "
    "ещё раз чуть позже."
)

# Что показать, если период приехал кнопкой и не прошёл проверку. Своими
# кнопками такое не нажать: это либо подделанный callback_data, либо кнопка
# из очень старого сообщения.
STALE_BUTTON = (
    "Этот период я посчитать не могу. Выберите период кнопкой ещё раз: "
    "команда <code>{command}</code>."
)

MONTH_BUTTONS = (
    "Январь",
    "Февраль",
    "Март",
    "Апрель",
    "Май",
    "Июнь",
    "Июль",
    "Август",
    "Сентябрь",
    "Октябрь",
    "Ноябрь",
    "Декабрь",
)

# Кому отдать период, когда человек прислал даты. Ключ это имя отчёта,
# значение его обработчик. Ставит себе каждый отчёт сам, в своём register.
_OWNERS: dict[str, Callable[..., Awaitable[Any]]] = {}


def set_owner(name: str, handler: Callable[..., Awaitable[Any]]) -> None:
    """Чей это ввод дат: `handler(update, context, period)`."""
    _OWNERS[name] = handler


def _example() -> str:
    """Пример периода в подсказке: прошлый месяц целиком, как его напишут."""
    months = finance.recent_months(1)
    if not months:
        return "01.09.2026 - 30.09.2026"
    window = months[0]
    return f"{_day(window.date_from)} - {_day(window.date_to)}"


def _day(value: date) -> str:
    return value.strftime("%d.%m.%Y")


def ask_text(max_days: int | None = None) -> Safe:
    """Подсказка про ввод дат. Числа из конфига, пример из календаря.

    Предел называется тот, который правда действует: у рекламы он свой и
    короче, и обещать там 366 дней значило бы отказать человеку после того,
    как он набрал период.
    """
    return fill(
        ASK_DATES,
        example=_example(),
        since=_day(finance.DATA_SINCE),
        limit=finance.days_words(max_days or finance.custom_max_days()),
    )


def error_text(error: finance.PeriodError) -> Safe:
    """Подсказка с примером вместо отказа. Неизвестная причина это разбор."""
    template = BAD_DATES.get(error.reason, BAD_DATES[finance.BAD_FORMAT])
    return fill(
        template,
        example=_example(),
        since=_day(finance.DATA_SINCE),
        limit=finance.days_words(error.limit or finance.custom_max_days()),
    )


def stale_text(command: str) -> Safe:
    return fill(STALE_BUTTON, command=f"/{command}")


def month_label(window: finance.Period, today: date | None = None) -> str:
    """Подпись кнопки месяца. Год дописывается там, где без него непонятно."""
    name = MONTH_BUTTONS[window.date_from.month - 1]
    now = today or date.today()
    return name if window.date_from.year == now.year else f"{name} {window.date_from.year}"


def callback_data(prefix: str, window: finance.Period) -> str:
    """Период в кнопке: датами. Проверять их всё равно будет функция."""
    return f"{prefix}{MARK}{window.date_from.isoformat()}:{window.date_to.isoformat()}"


def month_rows(
    prefix: str, *, today: date | None = None
) -> list[list[InlineKeyboardButton]]:
    """Ряды кнопок месяцев, по две в ряд, и кнопка «Свои даты» последней.

    По две в ряд потому, что читатель смотрит в телефон: полгода назад это три
    коротких ряда вместо шести длинных.
    """
    buttons = [
        InlineKeyboardButton(
            month_label(window, today), callback_data=callback_data(prefix, window)
        )
        for window in finance.recent_months(today=today)
    ]
    rows = [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
    rows.append(
        [InlineKeyboardButton(CUSTOM_BUTTON, callback_data=f"{prefix}{CUSTOM}")]
    )
    return rows


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Отмена", callback_data=CANCEL)]])


def period_of_callback(
    tail: str, *, today: date | None = None, max_days: int | None = None
) -> finance.Period | None:
    """Период из нажатой кнопки. `None` значит «такой период не считаем».

    Единственный разбор `callback_data` с датами на весь проект. Проверяет не
    клавиатура, а эта функция: кнопки рисуем мы, но данные присылает клиент, и
    подделанный период не должен ни уронить бота, ни отправить его просить у
    Wildberries десять лет.
    """
    parts = str(tail or "").split(":")
    if len(parts) != 3 or parts[0] != MARK[:-1]:
        return None
    try:
        first, last = date.fromisoformat(parts[1]), date.fromisoformat(parts[2])
    except ValueError:
        return None
    try:
        return finance.period_of(
            finance.DATES,
            today,
            date_from=first,
            date_to=last,
            max_days=max_days,
        )
    except finance.PeriodError:
        return None


async def ask(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    owner: str,
    *,
    max_days: int | None = None,
) -> None:
    """Кнопка «Свои даты»: спрашивает период текстом и запоминает, чей он."""
    message = update.effective_message
    if message is None:
        return
    if context is not None and getattr(context, "user_data", None) is not None:
        context.user_data[STATE] = {"owner": owner, "max_days": max_days}
    await message.reply_text(
        ask_text(max_days), parse_mode=ParseMode.HTML, reply_markup=cancel_keyboard()
    )


async def cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«Отмена»: ввод дат закрыт, обычные сообщения снова идут как обычно."""
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    if context is not None and getattr(context, "user_data", None) is not None:
        context.user_data.pop(STATE, None)
    if query.message is not None:
        await query.message.reply_text(CANCELLED, parse_mode=ParseMode.HTML)


async def text_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Текстовый шаг ввода дат. Вне диалога молчит и пропускает дальше.

    Остановка цепочки обязательна ровно в диалоге: без неё период уехал бы
    запасному обработчику артикула, и человек получил бы «товар не найден» на
    свои даты. А вне диалога остановки быть не должно: артикул товара это
    обычное сообщение боту, и перехватывать его нельзя.
    """
    if context is None or getattr(context, "user_data", None) is None:
        return
    state = context.user_data.get(STATE)
    if not state:
        return
    message = update.effective_message
    text = (getattr(message, "text", "") or "").strip()
    if not text:
        return

    try:
        first, last = finance.parse_dates(text)
        window = finance.period_of(
            finance.DATES,
            date_from=first,
            date_to=last,
            max_days=state.get("max_days"),
        )
    except finance.PeriodError as error:
        # Диалог не закрываем: человек правит даты и присылает ещё раз, а
        # закончить его можно кнопкой «Отмена».
        await message.reply_text(
            error_text(error), parse_mode=ParseMode.HTML, reply_markup=cancel_keyboard()
        )
        raise ApplicationHandlerStop

    handler = _OWNERS.get(str(state.get("owner") or ""))
    context.user_data.pop(STATE, None)
    if handler is None:
        logger.warning("ввод дат некому отдать: отчёт %s", state.get("owner"))
        await message.reply_text(NO_OWNER, parse_mode=ParseMode.HTML)
        raise ApplicationHandlerStop
    await handler(update, context, window)
    raise ApplicationHandlerStop


def register(app, *, path: str | None = None) -> None:
    """Сам себя регистрирует: bot/app.py никто не трогает.

    Обработчик текста один на все отчёты, поэтому и стоит он здесь, а не в
    каждом из них: четыре одинаковых текстовых шага в одной группе никогда бы
    не сработали все, а в разных группах разъехались бы поведением.
    """
    app.add_handler(CallbackQueryHandler(cancel_callback, pattern=f"^{CANCEL}$"))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_step), group=DIALOG_GROUP
    )
