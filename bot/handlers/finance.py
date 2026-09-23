"""Модуль «Финансы»: команда `/finance` и доставка недельной раскладки.

Тексты живут здесь, рядом с хендлером, и только по-русски. Считает всё
`agents.finance`, здесь его результат превращается в понятные селлеру строки.

Два правила, которые видно в коде.

Первое: в WB отсюда никто не ходит. Кнопка периода ставит задачу в очередь и
молчит, «принято» клиенту говорит сама очередь. У отчёта о реализации лимит
1 запрос в минуту, год выгружается страницами и идёт долго, а повтор при
недоступности Wildberries живёт только в очереди.

Второе: доступ к платному модулю проверяет `require_module`, своей проверки
здесь нет. Без доступа клиент видит, что даёт модуль, цену и кнопку
«Оформить», а не отказ.

Разметку в сообщении ставит только бот. Заголовок отчёта и даты недель
приходят из базы, то есть из ответа Wildberries, а сводка уходит с
`ParseMode.HTML`. Поэтому подстановка идёт через общий `bot.texts.fill`:
граница стоит на подстановке, а не у каждого поля.
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

from agents import finance
from bot.handlers.tariffs import client_id_of, require_module
from bot.texts import Safe, fill
from core import db, queue

logger = logging.getLogger(__name__)

PREFIX = "fin:"

BUTTONS = (
    ("week", "Последняя неделя"),
    ("month", "Месяц"),
    ("quarter", "Квартал"),
    ("year", "Год"),
)

ASK_PERIOD = (
    "💰 <b>Финансы</b>\n\n"
    "Покажу, сколько денег забрал Wildberries и за что: комиссия, эквайринг, "
    "логистика, хранение, приёмка, штрафы, прочие удержания.\n\n"
    "За какой период посчитать?"
)

LONG_PERIOD = (
    "Год Wildberries отдаёт по одной странице в минуту, это надолго. "
    "Соберу и пришлю, закрывать бот не нужно."
)

NO_DATA = (
    "За этот период Wildberries отчётов о реализации не отдал. Так бывает, "
    "когда продаж ещё не было или кабинет подключён недавно. Попробуйте "
    "период побольше."
)

HEADER = "💰 <b>Финансы: {title}</b>\n{period}"

MISMATCH = (
    "\n\n⚠️ Мои суммы разошлись с итогами Wildberries по неделям: {weeks}. "
    "Подробности на листе «Недели», колонка «Сверка с отчётом WB». "
    "Чаще всего это значит, что Wildberries досчитал отчёт уже после выгрузки: "
    "запросите период ещё раз через сутки."
)

UNVERIFIED = (
    "\n\n⚠️ Wildberries не отдал итоги отчёта за недели: {weeks}. Сверить свой "
    "расчёт с кабинетом я по ним не смог, поэтому считайте эти цифры "
    "предварительными. Запросите период ещё раз чуть позже."
)

INCOMPLETE = (
    "\n\n🚫 Данные за недели {weeks} неполные: выгрузка упёрлась в предел "
    "страниц, и часть строк отчёта в неё не попала. Цифры по этим неделям "
    "занижены. Запросите период короче, тогда они сойдутся с кабинетом WB."
)

FILE_NOTE = "\n\nПодробности в файле: недели, месяцы, артикулы и методология."

CENT = Decimal("0.01")


def _money(value: Decimal | int | float) -> str:
    """Рубли с разделителем тысяч. Копейки показываем, только если они есть."""
    amount = (value if isinstance(value, Decimal) else Decimal(str(value))).quantize(
        CENT, rounding=ROUND_HALF_UP
    )
    whole, _, cents = f"{amount:.2f}".partition(".")
    sign = "-" if whole.startswith("-") else ""
    digits = whole.lstrip("-")
    grouped = f"{int(digits):,}".replace(",", " ")
    tail = "" if cents == "00" else f",{cents}"
    return f"{sign}{grouped}{tail} ₽"


def _percent(value: Decimal | None) -> str:
    if value is None:
        return "нет данных"
    amount = value.quantize(CENT, rounding=ROUND_HALF_UP)
    text = f"{amount:.2f}".rstrip("0").rstrip(".")
    return f"{text.replace('.', ',')}%"


def _day(text: str) -> str:
    """ГГГГ-ММ-ДД в привычное ДД.ММ.ГГГГ."""
    parts = str(text)[:10].split("-")
    return ".".join(reversed(parts)) if len(parts) == 3 else str(text)


def keyboard() -> InlineKeyboardMarkup:
    """Кнопки периода: последняя неделя, месяц, квартал, год."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(title, callback_data=f"{PREFIX}{key}")]
            for key, title in BUTTONS
        ]
    )


def summary_text(report: Any) -> Safe:
    """Короткая сводка в сообщении. Всё остальное в файле."""
    if report.empty:
        return Safe(NO_DATA)

    totals = report.totals
    period = f"{_day(report.date_from.isoformat())} - {_day(report.date_to.isoformat())}"
    lines = [
        fill(HEADER, title=report.title, period=period),
        "",
        fill("Продажи: {amount}", amount=_money(totals.revenue)),
        fill("Возвраты: {amount}", amount=_money(totals.returns_amount)),
        fill("<b>К перечислению: {amount}</b>", amount=_money(totals.for_pay)),
        "",
        "Забрал Wildberries:",
    ]

    last = report.weeks[-1]
    lines += [
        fill(
            "комиссия {amount} ({percent})",
            amount=_money(totals.commission),
            percent=_percent(last.commission_percent),
        ),
        fill(
            "эквайринг {amount} ({percent})",
            amount=_money(totals.acquiring),
            percent=_percent(last.acquiring_percent),
        ),
        fill("логистика {amount}", amount=_money(totals.logistics)),
        fill("хранение {amount}", amount=_money(totals.storage)),
        fill("приёмка {amount}", amount=_money(totals.acceptance)),
        fill("штрафы {amount}", amount=_money(totals.penalties)),
        fill("прочие удержания {amount}", amount=_money(totals.deductions)),
        "",
        fill("СПП: {percent}", percent=_percent(last.spp)),
        fill("Недель в отчёте: {count}", count=len(report.weeks)),
    ]

    text = Safe("\n".join(lines) + FILE_NOTE)
    # Три оговорки, и они разные: расхождение, непроверенная неделя и
    # обрезанная выгрузка. Молчать ни про одну нельзя.
    for template, weeks in (
        (INCOMPLETE, report.incomplete),
        (MISMATCH, report.mismatches),
        (UNVERIFIED, report.unverified),
    ):
        if weeks:
            text = Safe(
                text
                + fill(template, weeks=", ".join(_day(week.date_from) for week in weeks))
            )
    return text


# --- команда и кнопки --------------------------------------------------------


async def finance_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    path: str | Path | None = None,
) -> None:
    """`/finance`: спрашивает период. Считать тут нечего, это делает очередь."""
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
    if period not in finance.PERIODS:
        await query.answer()
        return

    client_id = client_id_of(update, path)
    if client_id is None:
        await query.answer()
        return

    # Задача могла уже стоять: тогда честный ответ «уже считаю», а не второе
    # «принято». Текст берётся у очереди, чтобы всплывающее окно и сообщение
    # не рассказывали клиенту разное.
    task = finance.request_report(client_id, period, path=path)
    await query.answer("Принято" if task.created else queue.ALREADY_QUEUED)
    if period == "year" and query.message is not None:
        await query.message.reply_text(LONG_PERIOD)


# --- доставка готового отчёта ------------------------------------------------


def make_sender(app: Any, path: str | Path | None = None):
    """Чем агент отдаёт готовый отчёт: сводка сообщением, раскладка файлом."""

    async def send(client_id: int, report: Any, data: bytes) -> None:
        row = db.admin_repo(path).client(client_id)
        if row is None:
            logger.warning("некому отправить финансовый отчёт: клиента %s нет", client_id)
            return
        chat_id = int(row["telegram_id"])
        await app.bot.send_message(
            chat_id=chat_id, text=summary_text(report), parse_mode=ParseMode.HTML
        )
        if not report.empty:
            await app.bot.send_document(
                chat_id=chat_id, document=data, filename=finance.file_name(report)
            )

    return send


def register(app, *, path: str | Path | None = None) -> None:
    """Хендлер ставит себя сам: bot/app.py никто не трогает."""
    finance.set_sender(make_sender(app, path))
    finance.register_jobs()

    guard = require_module(finance.MODULE, path=path)
    app.add_handler(
        CommandHandler("finance", guard(functools.partial(finance_command, path=path)))
    )
    app.add_handler(
        CallbackQueryHandler(
            guard(functools.partial(period_chosen, path=path)), pattern=f"^{PREFIX}"
        )
    )
