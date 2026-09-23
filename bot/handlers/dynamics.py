"""Сторож скрытых расходов в телеграме: недельные алерты и `/dynamics`.

Считает всё `agents.watchdog`, здесь его результат превращается в строки,
которые читает селлер. Главное правило текста тут одно: рубли вместо пунктов.
«Эквайринг вырос с 1,4% до 2,1%, это +0,7 п.п. При выручке 840 000 ₽ за
неделю это 5 880 ₽» - и человек сразу знает, о каких деньгах речь.

Два правила, которые видно в коде.

Первое: в Wildberries отсюда никто не ходит, и сторож туда тоже не ходит.
Неделя уже лежит в базе после агента 1, команда отвечает сразу, без очереди.

Второе: доступ к платному модулю проверяет `require_module`, своей проверки
здесь нет.

Разметку в сообщении ставит только бот: и алерт, и таблица уходят с
`ParseMode.HTML`, а внутрь идут названия показателей и границы недель из
базы. Подстановка одна на файл, общий `bot.texts.fill`, и граница стоит на
ней, а не у каждого поля. Таблица заворачивается в `<pre>` тем же способом:
теги ставит шаблон, строки едут внутрь как значение.
"""

from __future__ import annotations

import functools
import logging
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from telegram.constants import ParseMode
from telegram.ext import CommandHandler

from agents import finance, watchdog
from bot.handlers.tariffs import client_id_of, require_module, rubles
from bot.texts import Safe, fill
from core import db, scheduler

logger = logging.getLogger(__name__)

MONTHS_OF = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)

# Как называется период в команде. По-русски, потому что так его и напишут.
PERIOD_WORDS: dict[str, str] = {
    "неделя": "week",
    "неделю": "week",
    "месяц": "month",
    "месяца": "month",
    "квартал": "quarter",
    "квартала": "quarter",
    "год": "year",
    "года": "year",
}

HEADER = "⚠️ <b>Расходы за неделю {week}</b>"
QUIET = (
    "✅ <b>Неделя {week}</b>\n\n"
    "Расходы в порядке: ничего не выросло заметно по сравнению со средним "
    "за {baseline}."
)
NOT_ENOUGH = (
    "📊 <b>Расходы за неделю</b>\n\n"
    "Пока мало данных для сравнения. Чтобы понять, что расход вырос, мне "
    "нужно {needed} подряд: одна свежая и {baseline} до неё. Сейчас собрано "
    "{have}, осталось подождать ещё {missing}."
)
NO_WEEKS = (
    "📊 Пока мало данных для сравнения. Я ещё не получил ни одного недельного "
    "отчёта от Wildberries. Первый отчёт появится после ближайшей выгрузки, "
    "дальше буду сравнивать каждую неделю со средним за предыдущие."
)
DOUBTFUL = (
    "\n\nОговорка: данные последней недели Wildberries ещё не подтвердил "
    "сверкой, цифры могут немного измениться."
)
COST_LINE = "При выручке {revenue} за неделю это {rubles}."

# У СПП та же сумма значит другое. Это не удержание, а на сколько изменилась
# скидка площадки, и сложить её с расходами нельзя: получится неправда.
SPP_LINE = (
    "На {rubles} изменилась скидка площадки за неделю при выручке {revenue}. "
    "Это не удержание, к расходам эту сумму не прибавляют."
)

# Почему процент здесь и процент в /finance могут не совпасть. Молчать об этом
# нельзя: селлер увидит две цифры об одной и той же неделе.
WITHHELD = (
    "\n\nКомиссия и эквайринг посчитаны от удержанных рублей: сколько "
    "Wildberries забрал на самом деле, к продажам недели. В /finance тот же "
    "процент показан так, как его отдаёт сам Wildberries, то есть по тарифу. "
    "Расходятся они ровно тогда, когда удержали не по тарифу, и именно эта "
    "разница и есть утечка."
)

# Показатели, к которым относится оговорка про удержанную долю.
WITHHELD_METRICS = ("commission", "acquiring", "logistics_share", "storage_share")

NO_DYNAMICS = (
    "За {title} недельных отчётов от Wildberries пока нет. Как только они "
    "появятся, я покажу здесь, как менялись комиссия, эквайринг, СПП, "
    "логистика и хранение."
)
DYNAMICS_HEADER = "📈 <b>Динамика за {title}</b>"
DYNAMICS_NOTE = (
    "Комиссия, эквайринг, логистика и хранение показаны долей от выручки "
    "недели: это то, что Wildberries удержал на самом деле. В /finance "
    "комиссия и эквайринг стоят в процентах самого Wildberries, то есть по "
    "тарифу, поэтому цифры могут отличаться. Разница между ними и есть то, "
    "что я ищу. СПП это не расход, а скидка площадки, её процент взят как "
    "есть."
)
HOW_TO = (
    "Период можно выбрать: <code>/dynamics месяц</code>, "
    "<code>/dynamics квартал</code>, <code>/dynamics год</code>."
)

# Как назван показатель в строке алерта. Род у слов разный, поэтому не
# «шаблон плюс название», а готовая пара «вырос / снизился» на каждый.
PHRASES: dict[str, tuple[str, str]] = {
    "commission": ("Фактическая комиссия площадки выросла", "Фактическая комиссия площадки снизилась"),
    "acquiring": ("Эквайринг вырос", "Эквайринг снизился"),
    "spp": ("СПП вырос", "СПП снизился"),
    "logistics_share": ("Доля логистики в выручке выросла", "Доля логистики в выручке снизилась"),
    "storage_share": ("Доля хранения в выручке выросла", "Доля хранения в выручке снизилась"),
}

# Столбцы таблицы `/dynamics`: заголовок и показатель.
COLUMNS: tuple[tuple[str, str], ...] = (
    ("Комис", "commission"),
    ("Эквай", "acquiring"),
    ("СПП", "spp"),
    ("Логис", "logistics_share"),
    ("Хранн", "storage_share"),
)


# --- слова и числа -----------------------------------------------------------


def _weeks_word(count: int) -> str:
    tail = count % 100
    if 11 <= tail <= 14:
        return f"{count} недель"
    return {1: f"{count} неделя", 2: f"{count} недели", 3: f"{count} недели", 4: f"{count} недели"}.get(
        count % 10, f"{count} недель"
    )


def _weeks_accusative(count: int) -> str:
    """«осталось ещё 2 недели», «нужно 5 недель»."""
    tail = count % 100
    if 11 <= tail <= 14:
        return f"{count} недель"
    return {1: f"{count} неделю", 2: f"{count} недели", 3: f"{count} недели", 4: f"{count} недели"}.get(
        count % 10, f"{count} недель"
    )


def _percent(value: Decimal | None) -> str:
    """Процент с десятой долей, без лишнего нуля: 15% и 2,1%."""
    if value is None:
        return "-"
    rounded = Decimal(value).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    text = format(rounded, "f").replace(".", ",")
    if text.endswith(",0"):
        text = text[:-2]
    return text + "%"


def _points(delta: Decimal) -> str:
    """Изменение в пунктах со знаком: +0,7 п.п. и -6 п.п."""
    sign = "+" if delta > 0 else "-"
    return sign + _percent(abs(delta)).rstrip("%") + " п.п."


def _day_month(value: str) -> str:
    try:
        moment = date.fromisoformat(str(value))
    except ValueError:
        return str(value)
    return f"{moment.day} {MONTHS_OF[moment.month - 1]}"


def _week_days(week: Any) -> str:
    """«7-13 сентября», а через границу месяца «31 августа - 6 сентября»."""
    if week is None:
        return ""
    try:
        start = date.fromisoformat(str(week.date_from))
        end = date.fromisoformat(str(week.date_to))
    except ValueError:
        return f"{week.date_from} - {week.date_to}"
    if start.month == end.month:
        return f"{start.day}-{end.day} {MONTHS_OF[end.month - 1]}"
    return f"{_day_month(week.date_from)} - {_day_month(week.date_to)}"


def _short_day(value: str) -> str:
    try:
        moment = date.fromisoformat(str(value))
    except ValueError:
        return str(value)[:5]
    return f"{moment.day:02d}.{moment.month:02d}"


# --- тексты ------------------------------------------------------------------


def alert_lines(alert: watchdog.Alert) -> list[Safe]:
    """Один алерт двумя строками: что изменилось и сколько это стоит."""
    grew, fell = PHRASES.get(alert.metric, (f"{alert.title} вырос", f"{alert.title} снизился"))
    template = SPP_LINE if alert.metric == "spp" else COST_LINE
    return [
        fill(
            "{phrase} с {was} до {now}, это {delta}",
            phrase=grew if alert.grew else fell,
            was=_percent(alert.was),
            now=_percent(alert.now),
            delta=_points(alert.delta),
        ),
        fill(template, revenue=rubles(alert.revenue), rubles=rubles(alert.rubles)),
        Safe(""),
    ]


def alerts_text(watch: watchdog.Watch) -> Safe:
    """Недельное сообщение. Молчания нет ни в одном из трёх случаев."""
    baseline = _weeks_word(max(0, watch.needed - 1))
    if not watch.enough:
        if watch.have == 0:
            return Safe(NO_WEEKS)
        return fill(
            NOT_ENOUGH,
            needed=_weeks_accusative(watch.needed),
            baseline=_weeks_accusative(watch.needed - 1),
            have=_weeks_word(watch.have),
            missing=_weeks_accusative(watch.missing),
        )

    days = _week_days(watch.week)
    if not watch.alerts:
        text = fill(QUIET, week=days, baseline=baseline)
        return Safe(text + DOUBTFUL) if watch.doubtful else text

    lines = [fill(HEADER, week=days), ""]
    for alert in watch.alerts:
        lines.extend(alert_lines(alert))
    text = Safe("\n".join(lines).rstrip())
    text = Safe(
        text + fill("\n\nСравнение со средним за {baseline} до этой.", baseline=baseline)
    )
    if any(alert.metric in WITHHELD_METRICS for alert in watch.alerts):
        text = Safe(text + WITHHELD)
    return Safe(text + DOUBTFUL) if watch.doubtful else text


def table_text(table: watchdog.Dynamics) -> Safe:
    """Таблица показателей по неделям. Моноширинная, чтобы столбцы сошлись."""
    if table.empty:
        return Safe(fill(NO_DYNAMICS, title=table.title) + "\n\n" + HOW_TO)

    head = f"{'Неделя':<7}{'Выручка':>10}" + "".join(f"{title:>7}" for title, _ in COLUMNS)
    rows = [head]
    for week in table.weeks:
        revenue = f"{int(week.revenue):,}".replace(",", " ")
        line = f"{_short_day(week.date_from):<7}{revenue:>10}"
        line += "".join(f"{_percent(week.value(name)):>7}" for _, name in COLUMNS)
        rows.append(line)

    # Таблицу заворачивает в теги шаблон, а строки едут внутрь значением:
    # ширина столбцов считается по исходным символам, и правило «разметку
    # ставит только бот» от этого не меняется.
    return Safe(
        fill(DYNAMICS_HEADER, title=table.title)
        + fill("\n<pre>{rows}</pre>\n", rows="\n".join(rows))
        + DYNAMICS_NOTE
        + "\n\n"
        + HOW_TO
    )


# --- команда -----------------------------------------------------------------


def period_of(args: Any) -> str:
    """Период из аргументов команды. По умолчанию месяц."""
    for item in args or ():
        word = str(item).strip().lower()
        if word in PERIOD_WORDS:
            return PERIOD_WORDS[word]
        if word in finance.PERIODS:
            return word
    return "month"


async def dynamics_command(
    update: Any,
    context: Any,
    *,
    path: str | Path | None = None,
    today: date | None = None,
) -> None:
    """`/dynamics`: таблица показателей по неделям за выбранный период.

    Отвечает сразу: считать нечего, всё уже собрано агентом 1.
    """
    message = update.effective_message
    client_id = client_id_of(update, path)
    if message is None or client_id is None:
        return
    moment = today or datetime.now(scheduler.tz()).date()
    table = watchdog.dynamics(
        client_id, period_of(getattr(context, "args", None)), today=moment, path=path
    )
    await message.reply_text(table_text(table), parse_mode=ParseMode.HTML)


# --- доставка недельного осмотра ---------------------------------------------


def make_delivery(app: Any, path: str | Path | None = None):
    """Чем сторож отправляет недельный осмотр. Текст собирается здесь."""

    async def deliver(client_id: int, watch: watchdog.Watch) -> None:
        row = db.admin_repo(path).client(client_id)
        if row is None:
            logger.warning("некому отправить алерты: клиента %s нет в базе", client_id)
            return
        await app.bot.send_message(
            chat_id=int(row["telegram_id"]),
            text=alerts_text(watch),
            parse_mode=ParseMode.HTML,
        )

    return deliver


def register(app, *, path: str | Path | None = None) -> None:
    """Хендлер ставит себя сам: bot/app.py никто не трогает."""
    watchdog.set_delivery(make_delivery(app, path))
    watchdog.register_jobs(path=path)

    guard = require_module(watchdog.MODULE, path=path)
    app.add_handler(
        CommandHandler("dynamics", guard(functools.partial(dynamics_command, path=path)))
    )
