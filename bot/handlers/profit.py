"""Прибыльность артикулов: команда `/profit` и доставка отчёта.

Тексты живут здесь, рядом с хендлером, и только по-русски. Считает всё
`agents.profit`, здесь его результат превращается в строки, понятные селлеру:
«кто кормит, а кто ест», а не «margin=41.25».

Три правила, которые видно в коде.

Первое: в WB отсюда никто не ходит. Кнопка периода ставит задачу в очередь и
молчит, «принято» клиенту говорит сама очередь. За расходом рекламы идёт
обработчик задачи: у `fullstats` лимит 3 запроса в минуту, а повтор при
недоступности Wildberries живёт только в очереди.

Второе: доступ к платному модулю проверяет `require_module`, своей проверки
здесь нет. Прибыльность входит в модуль «Финансы».

Третье: чего нет, о том говорится вслух. Нет себестоимости - артикул назван
в отдельном блоке, и прибыль по нему не посчитана. Нет категории
«Продвижение» - в колонке «реклама» стоит «нет данных», а прибыль посчитана
без рекламы, и в сообщении написано, почему.
"""

from __future__ import annotations

import functools
import logging
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from agents import profit
from bot.handlers.tariffs import client_id_of, require_module

logger = logging.getLogger(__name__)

PREFIX = "profit:"

BUTTONS = (
    ("week", "Последняя неделя"),
    ("month", "Месяц"),
    ("quarter", "Квартал"),
    ("year", "Год"),
)

ASK_PERIOD = (
    "📈 <b>Прибыль по артикулам</b>\n\n"
    "Покажу, какие товары зарабатывают, а какие съедают деньги: выручка, "
    "себестоимость, комиссия, логистика, хранение, штрафы, реклама и возвраты "
    "по каждому артикулу.\n\n"
    "За какой период посчитать?"
)

LONG_PERIOD = (
    "Год Wildberries отдаёт медленно, это надолго. Соберу и пришлю, закрывать "
    "бот не нужно."
)

NO_DATA = (
    "За этот период считать нечего: Wildberries не отдал отчётов о реализации. "
    "Так бывает, когда продаж ещё не было или кабинет подключён недавно. "
    "Попробуйте период побольше."
)

HEADER = "📈 <b>Прибыль по артикулам: {title}</b>\n{period}"

FEED = "\n<b>Кто кормит</b>"
EAT = "\n<b>Кто ест</b>"

NO_COSTS = (
    "\n\n⚠️ Артикулов без себестоимости: {count}. Прибыль по ним не посчитана, "
    "они собраны в отдельном блоке файла. Пришлите себестоимость командой "
    "/costs, и они встанут в общий ряд."
)

LOSSES = "\n\n🔻 Убыточных артикулов: {count}. Все они есть в файле, блок «{sheet}»."

NO_COMMISSION = (
    "\n\n⚠️ У {count} артикулов комиссия Wildberries неизвестна: их строки "
    "сохранены без исходных полей отчёта. Прибыль по ним не посчитана, "
    "подставлять вместо комиссии соседнее поле я не стал. Запросите этот "
    "период заново, и строки перезапишутся."
)

FACELESS = (
    "\n\n🧾 Расходы без артикула (обезличка): {amount}. Я разнёс их по артикулам "
    "пропорционально выручке, правило записано на листе «Методология»."
)

FACELESS_LEFT = (
    " Не разнесённый остаток {amount} в прибыль не вошёл: разносить его не по чему."
)

ADS_MISSING = {
    profit.ADS_NO_CATEGORY: (
        "\n\n⚠️ Расход рекламы посчитать не вышло: в токене нет категории "
        "«Продвижение». В колонке «реклама» стоит «нет данных», прибыль "
        "посчитана без неё, поэтому она завышена. Выдайте категорию в кабинете "
        "WB и подключите токен заново командой /connect."
    ),
    profit.ADS_UNAVAILABLE: (
        "\n\n⚠️ Wildberries не отдал статистику рекламы. В колонке «реклама» "
        "стоит «нет данных», прибыль посчитана без неё, поэтому она завышена. "
        "Запросите отчёт ещё раз чуть позже."
    ),
    profit.ADS_NOT_REQUESTED: (
        "\n\n⚠️ Расход рекламы в этот отчёт не попал. В колонке «реклама» стоит "
        "«нет данных», прибыль посчитана без неё."
    ),
}

UNVERIFIED = (
    "\n\n⚠️ Эти недели Wildberries не подтвердил итогами отчёта: {weeks}. "
    "Считайте цифры по ним предварительными."
)

INCOMPLETE = (
    "\n\n🚫 Данные за эти недели неполные: {weeks}. Выгрузка упёрлась в предел "
    "страниц, и прибыль по ним занижена. Сторож расходов такие недели из "
    "сравнения выбрасывает, а я считаю: это не спор, а два разных взгляда на "
    "одну неделю. Чтобы цифры сошлись с кабинетом, запросите период короче."
)

FILE_NOTE = (
    "\n\nПолная таблица в файле: все артикулы, отдельный блок «убыточные, "
    "обезличка, без себестоимости» и лист «Методология»."
)

CENT = Decimal("0.01")


def _money(value: Decimal | int | float | None) -> str:
    """Рубли с разделителем тысяч. Копейки показываем, только если они есть."""
    if value is None:
        return "нет данных"
    amount = (value if isinstance(value, Decimal) else Decimal(str(value))).quantize(
        CENT, rounding=ROUND_HALF_UP
    )
    whole, _, cents = f"{amount:.2f}".partition(".")
    sign = "-" if whole.startswith("-") else ""
    digits = whole.lstrip("-")
    grouped = f"{int(digits):,}".replace(",", " ")
    tail = "" if cents == "00" else f",{cents}"
    return f"{sign}{grouped}{tail} ₽"


def _percent(value: float | None) -> str:
    if value is None:
        return "нет данных"
    text = f"{Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP):.2f}"
    return f"{text.rstrip('0').rstrip('.').replace('.', ',')}%"


def _day(text: str) -> str:
    """ГГГГ-ММ-ДД в привычное ДД.ММ.ГГГГ."""
    parts = str(text)[:10].split("-")
    return ".".join(reversed(parts)) if len(parts) == 3 else str(text)


def _week_days(week: Any) -> str:
    """Неделя обеими датами: ДД.ММ.ГГГГ - ДД.ММ.ГГГГ."""
    return f"{_day(week.date_from)} - {_day(week.date_to)}"


def _line(number: int, item: Any) -> str:
    """Одна строка топа: артикул, прибыль, маржинальность, доля."""
    name = f"{item.nm_id}"
    if item.vendor_code:
        name += f" ({item.vendor_code})"
    parts = [f"{number}. {name}: {_money(item.profit)}"]
    if item.margin is not None:
        parts.append(f"маржа {_percent(item.margin)}")
    if item.share is not None:
        parts.append(f"доля {_percent(item.share)}")
    return ", ".join(parts)


def keyboard() -> InlineKeyboardMarkup:
    """Кнопки периода: последняя неделя, месяц, квартал, год."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(title, callback_data=f"{PREFIX}{key}")]
            for key, title in BUTTONS
        ]
    )


def summary_text(report: Any) -> str:
    """Топ 5 и антитоп 5 в сообщении. Полная таблица уходит файлом."""
    if report.empty:
        return NO_DATA

    period = f"{_day(report.date_from.isoformat())} - {_day(report.date_to.isoformat())}"
    lines = [
        HEADER.format(title=report.title, period=period),
        "",
        f"Прибыль за период: <b>{_money(report.total_profit)}</b>",
        f"Выручка за вычетом возвратов: {_money(report.total_revenue)}",
        f"Реклама: {_money(report.total_ad_spend)}",
        f"Артикулов в отчёте: {len(report.articles)}",
    ]

    if report.top:
        lines.append(FEED)
        lines += [_line(number, item) for number, item in enumerate(report.top, 1)]
    if report.bottom:
        lines.append(EAT)
        lines += [_line(number, item) for number, item in enumerate(report.bottom, 1)]

    text = "\n".join(lines)
    if report.losses:
        text += LOSSES.format(count=len(report.losses), sheet=profit.PROBLEMS_SHEET)
    if report.without_cost:
        text += NO_COSTS.format(count=len(report.without_cost))
    if report.without_commission:
        text += NO_COMMISSION.format(count=len(report.without_commission))
    if report.unallocated > 0:
        text += FACELESS.format(amount=_money(report.unallocated))
        if report.unallocated_left:
            text += FACELESS_LEFT.format(amount=_money(report.unallocated_left))
    if not report.ads.available:
        text += ADS_MISSING.get(report.ads.reason, ADS_MISSING[profit.ADS_NOT_REQUESTED])
    # Неделя называется обеими датами, а не одной. Сторож расходов неполные
    # недели пропускает, а тут они посчитаны: без точных границ два ответа
    # выглядели бы как противоречие, а не как разные взгляды на одну неделю.
    for template, weeks in ((INCOMPLETE, report.incomplete), (UNVERIFIED, report.unverified)):
        if weeks:
            text += template.format(weeks="; ".join(_week_days(week) for week in weeks))
    return text + FILE_NOTE


# --- команда и кнопки --------------------------------------------------------


async def profit_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    path: str | Path | None = None,
) -> None:
    """`/profit`: спрашивает период. Считать тут нечего, это делает очередь."""
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(ASK_PERIOD, parse_mode=ParseMode.HTML, reply_markup=keyboard())


async def period_chosen(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    path: str | Path | None = None,
) -> None:
    """Кнопка периода: ставит задачу в очередь и отвечает сразу."""
    query = update.callback_query
    if query is None:
        return
    period = str(query.data or "")[len(PREFIX) :]
    if period not in profit.PERIODS:
        await query.answer()
        return

    client_id = client_id_of(update, path)
    if client_id is None:
        await query.answer()
        return

    profit.request_report(client_id, period, path=path)
    await query.answer("Принято")
    if period == "year" and query.message is not None:
        await query.message.reply_text(LONG_PERIOD)


# --- доставка готового отчёта ------------------------------------------------


def make_sender(app: Any, path: str | Path | None = None):
    """Чем агент отдаёт готовый отчёт: сводка сообщением, таблица файлом."""
    from core import db

    async def send(client_id: int, report: Any, data: bytes) -> None:
        row = db.admin_repo(path).client(client_id)
        if row is None:
            logger.warning("некому отправить отчёт о прибыли: клиента %s нет", client_id)
            return
        chat_id = int(row["telegram_id"])
        await app.bot.send_message(
            chat_id=chat_id, text=summary_text(report), parse_mode=ParseMode.HTML
        )
        if not report.empty:
            await app.bot.send_document(
                chat_id=chat_id, document=data, filename=profit.file_name(report)
            )

    return send


def register(app, *, path: str | Path | None = None) -> None:
    """Хендлер ставит себя сам: bot/app.py никто не трогает."""
    profit.set_sender(make_sender(app, path))
    profit.register_jobs()

    guard = require_module(profit.MODULE, path=path)
    app.add_handler(
        CommandHandler("profit", guard(functools.partial(profit_command, path=path)))
    )
    app.add_handler(
        CallbackQueryHandler(
            guard(functools.partial(period_chosen, path=path)), pattern=f"^{PREFIX}"
        )
    )
