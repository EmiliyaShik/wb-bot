"""Модуль «План-факт»: команды /plan и /rnp и утренний отчёт.

Тексты живут здесь, рядом с хендлером, и только по-русски. Считает всё
`agents.rnp`, здесь его результат превращается в понятные селлеру строки:
«на складе осталось на 9 дней», а не «stock_days=9».

Два правила, которые видно в коде.

Первое: в WB отсюда никто не ходит. Команда ставит задачу в очередь и
молчит, «принято» клиенту говорит сама очередь. Повтор при недоступности
Wildberries живёт только там.

Второе: доступ к платному модулю проверяет `require_module`, своей проверки
здесь нет. Без доступа клиент видит, что даёт модуль, цену и кнопку
«Оформить», а не отказ.

Разметку в сообщении ставит только бот: отчёт уходит с `ParseMode.HTML`, а
внутрь попадают числа и артикулы из базы. Подстановка идёт через общий
`bot.texts.fill`, граница стоит на ней, а не у каждого поля.
"""

from __future__ import annotations

import functools
import logging
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from telegram.constants import ParseMode
from telegram.ext import CommandHandler

from agents import rnp
from bot.handlers.tariffs import client_id_of, require_module, rubles
from bot.texts import Safe, fill
from core import db, scheduler

logger = logging.getLogger(__name__)

MONTHS_OF = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)
MONTHS = (
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
)

HEADER = "📈 <b>Отчёт за {day}</b>"

NO_DATA = (
    "За {day} данных от Wildberries пока нет. Так бывает в первый день "
    "работы бота и когда кабинет ещё не отдал статистику. Я соберу их "
    "сегодня ночью, завтрашний отчёт уже будет полным."
)

HOW_TO_SET_PLAN = (
    "План на месяц ставится одной строкой: сначала выручка в рублях, потом "
    "заказы.\n\n"
    "Например: <code>/plan 500000 300</code> это 500 000 рублей выручки и "
    "300 заказов за месяц.\n\n"
    "Можно указать только выручку: <code>/plan 500000</code>.\n"
    "План касается текущего месяца, поменять его можно в любой день."
)

SHORT_BASE = (
    "Данных пока немного: я собираю их с первого дня подключения. Для нового "
    "кабинета это нормально, чем дальше, тем точнее будет сравнение."
)

PLAN_SAVED = "План на {month} принят.\n{lines}\n\nБуду считать выполнение каждое утро."

NOT_A_NUMBER = (
    "Не понял числа. Выручку и заказы пишите цифрами, например: "
    "<code>/plan 500000 300</code>."
)

TOO_BIG = (
    "Слишком большая сумма, проверьте ввод. План на месяц пишется в рублях, "
    "например: <code>/plan 500000 300</code>."
)

# Потолок плана на месяц: триллион рублей выручки и миллиард заказов. Это
# больше, чем оборот всего Wildberries за год, то есть заведомо промах по
# клавише, а не цель.
#
# Проверка стоит до записи в базу, и это здесь важнее самой границы. Decimal
# не переполняется молча: тридцатизначное число спокойно ляжет в план, а
# упадёт потом rubles() - каждое утро, в каждом отчёте этого клиента. Починить
# такое селлер не может, отчёт умирает раньше, чем покажет ему план.
MAX_PLAN_REVENUE = Decimal("1000000000000")
MAX_PLAN_ORDERS = 1_000_000_000


def _date_ru(moment: date) -> str:
    return f"{moment.day} {MONTHS_OF[moment.month - 1]}"


def _month_name(year_month: str) -> str:
    """Название месяца по ключу ГГГГ-ММ, как он лежит в плане."""
    try:
        return MONTHS[int(str(year_month)[5:7]) - 1]
    except (ValueError, IndexError):
        return "месяц"


def _percent(value: Decimal | None) -> str:
    """Процент без лишних нулей: 25.00 это 25, а 7.50 это 7,5."""
    if value is None:
        return ""
    text = format(value.normalize(), "f")
    return text.replace(".", ",") + "%"


def _pieces(count: int) -> str:
    """Штуки по-русски: 1 штука, 2 штуки, 5 штук."""
    tail = count % 100
    if 11 <= tail <= 14:
        return f"{count} штук"
    return {1: f"{count} штука", 2: f"{count} штуки", 3: f"{count} штуки", 4: f"{count} штуки"}.get(
        count % 10, f"{count} штук"
    )


def _days(count: int) -> str:
    tail = count % 100
    if 11 <= tail <= 14:
        return f"{count} дней"
    return {1: f"{count} день", 2: f"{count} дня", 3: f"{count} дня", 4: f"{count} дня"}.get(
        count % 10, f"{count} дней"
    )


def _orders(count: int) -> str:
    tail = count % 100
    if 11 <= tail <= 14:
        return f"{count} заказов"
    return {1: f"{count} заказ", 2: f"{count} заказа", 3: f"{count} заказа", 4: f"{count} заказа"}.get(
        count % 10, f"{count} заказов"
    )


def report_text(report: rnp.RnpReport) -> Safe:
    """Утренний отчёт словами.

    Блока план-факт нет, если плана нет: это требование R125 дословно.
    Отчёт при этом приходит целиком, а не заменяется словами «план не задан».
    """
    lines = [fill(HEADER, day=_date_ru(report.date)), ""]

    if not report.has_data:
        lines.append(fill(NO_DATA, day=_date_ru(report.date)))
        return Safe("\n".join(lines))

    lines.append(
        fill(
            "Заказы: {count}{against}",
            count=report.orders,
            against=_against(report.avg_orders, _orders, report.avg_days),
        )
    )
    lines.append(
        fill(
            "Выручка: {amount}{against}",
            amount=rubles(report.revenue),
            against=_against(report.avg_revenue, _money, report.avg_days),
        )
    )
    if 0 < report.avg_days < rnp.WINDOW_DAYS:
        lines.append(SHORT_BASE)

    if report.plan is not None:
        # Месяц берётся у самого плана, а не у вчерашней даты: первого числа
        # вчера это ещё прошлый месяц, а план уже новый.
        lines.extend(
            ["", fill("<b>План на {month}</b>", month=_month_name(report.plan.year_month))]
        )
        lines.extend(_plan_lines(report))

    lines.extend(["", "<b>Реклама за вчера</b>"])
    if report.ad_spend > 0:
        share = _percent(report.drr)
        spent = rubles(report.ad_spend)
        lines.append(
            fill("Расход: {amount}, ДРР {share}", amount=spent, share=share)
            if share
            else fill(
                "Расход: {amount}, выручки за день нет, ДРР не считается", amount=spent
            )
        )
    else:
        lines.append("Расхода не было.")

    if report.risks:
        lines.extend(["", "<b>Скоро закончится</b>"])
        lines.extend(_risk_line(risk) for risk in report.risks)
    return Safe("\n".join(lines))


def _risk_line(risk: rnp.StockRisk) -> Safe:
    """Строка про остаток. «Хватит на 0 дней» не пишем: это не срок, а конец."""
    if risk.stock <= 0:
        return fill(
            "Артикул {nm_id}: на складе пусто, товар закончился.", nm_id=risk.nm_id
        )
    if risk.days <= 0:
        return fill(
            "Артикул {nm_id}: осталось {stock}, при нынешней скорости это меньше дня.",
            nm_id=risk.nm_id,
            stock=_pieces(risk.stock),
        )
    return fill(
        "Артикул {nm_id}: осталось {stock}, при нынешней скорости хватит на {days}.",
        nm_id=risk.nm_id,
        stock=_pieces(risk.stock),
        days=_days(risk.days),
    )


def _money(value: Decimal) -> str:
    return rubles(value)


def _against(average: Decimal | None, shape, avg_days: int) -> str:
    """Сравнение со средним. Средних нет, если суток ещё не набралось.

    Два слова тут неслучайны. «В день» - потому что «в среднем за неделю
    10 заказов» читается и как недельный итог. Настоящее число собранных
    суток - потому что назвать двое суток неделей значит подсунуть селлеру
    базу сравнения, которой нет: он решит, что вчера хуже обычного, а
    «обычного» ещё не было.
    """
    if average is None or avg_days <= 0:
        return ""
    value = shape(int(average)) if shape is _orders else shape(average)
    if avg_days >= rnp.WINDOW_DAYS:
        return f", в среднем {value} в день за прошлую неделю"
    return f", в среднем {value} в день, но собрано пока {_days(avg_days)}"


def _plan_lines(report: rnp.RnpReport) -> list[Safe]:
    plan = report.plan
    lines: list[Safe] = []
    if plan.revenue is not None:
        share = _percent(report.revenue_percent)
        done = rubles(report.month_revenue)
        target = rubles(plan.revenue)
        lines.append(
            fill("Выручка: {done} из {target}, это {share}", done=done, target=target, share=share)
            if share
            else fill("Выручка: {done} из {target}", done=done, target=target)
        )
    else:
        lines.append(
            fill("Выручка с начала месяца: {amount}", amount=rubles(report.month_revenue))
        )
    if plan.orders is not None:
        share = _percent(report.orders_percent)
        lines.append(
            fill(
                "Заказы: {done} из {target}, это {share}",
                done=report.month_orders,
                target=plan.orders,
                share=share,
            )
            if share
            else fill(
                "Заказы: {done} из {target}",
                done=report.month_orders,
                target=plan.orders,
            )
        )
    else:
        lines.append(fill("Заказов с начала месяца: {count}", count=report.month_orders))
    lines.append(
        fill(
            "По нынешнему темпу к концу месяца выйдет {revenue} и {orders}.",
            revenue=rubles(report.forecast_revenue),
            orders=_orders(report.forecast_orders),
        )
    )
    return lines


# --- команды ---


def _number(raw: str) -> Decimal | None:
    """Число из того, как его пишут люди: 500000, 500 000, 500000р."""
    digits = re.sub(r"[^\d.,]", "", raw).replace(",", ".")
    if not digits:
        return None
    try:
        return Decimal(digits)
    except InvalidOperation:
        return None


def _today(today: date | None) -> date:
    """Сегодня по часовому поясу расписания. Своего разбора пояса тут нет."""
    return today or datetime.now(scheduler.tz()).date()


async def plan_command(
    update: Any,
    context: Any,
    *,
    path: str | Path | None = None,
    today: date | None = None,
) -> None:
    """`/plan`: план на месяц по выручке и заказам."""
    message = update.effective_message
    client_id = client_id_of(update, path)
    if message is None or client_id is None:
        return

    moment = _today(today)
    month = rnp.year_month(moment)
    args = [str(item) for item in (getattr(context, "args", None) or [])]

    if not args:
        await message.reply_text(_current_plan_text(client_id, month, moment, path),
                                 parse_mode=ParseMode.HTML)
        return

    numbers = [_number(item) for item in args]
    if any(value is None for value in numbers):
        await message.reply_text(NOT_A_NUMBER, parse_mode=ParseMode.HTML)
        return

    revenue = numbers[0]
    orders = int(numbers[1]) if len(numbers) > 1 else None
    if revenue > MAX_PLAN_REVENUE or (orders is not None and orders > MAX_PLAN_ORDERS):
        await message.reply_text(TOO_BIG, parse_mode=ParseMode.HTML)
        return
    rnp.set_plan(client_id, month, revenue=revenue, orders=orders, path=path)

    lines = [fill("Выручка: {amount}", amount=rubles(revenue))]
    if orders is not None:
        lines.append(fill("Заказы: {count}", count=orders))
    await message.reply_text(
        fill(
            PLAN_SAVED,
            month=MONTHS[moment.month - 1],
            lines=Safe("\n".join(lines)),
        ),
        parse_mode=ParseMode.HTML,
    )


def _current_plan_text(client_id: int, month: str, moment: date, path) -> Safe:
    plan = rnp.plan_of(client_id, month, path=path)
    if plan is None:
        return Safe(HOW_TO_SET_PLAN)
    lines = [fill("План на {month}:", month=MONTHS[moment.month - 1])]
    if plan.revenue is not None:
        lines.append(fill("Выручка: {amount}", amount=rubles(plan.revenue)))
    if plan.orders is not None:
        lines.append(fill("Заказы: {count}", count=plan.orders))
    lines.append("")
    lines.append(HOW_TO_SET_PLAN)
    return Safe("\n".join(lines))


async def rnp_command(
    update: Any,
    context: Any,
    *,
    path: str | Path | None = None,
    today: date | None = None,
) -> None:
    """`/rnp`: отчёт по требованию. Ставится в очередь, а не считается тут."""
    message = update.effective_message
    client_id = client_id_of(update, path)
    if message is None or client_id is None:
        return
    rnp.request_report(client_id, day=_today(today), path=path)


# --- доставка утреннего отчёта ---


def make_delivery(app: Any, path: str | Path | None = None):
    """Чем агент отправляет готовый отчёт. Текст собирается здесь."""

    async def deliver(client_id: int, report: rnp.RnpReport) -> None:
        row = db.admin_repo(path).client(client_id)
        if row is None:
            logger.warning("некому отправить отчёт: клиента %s нет в базе", client_id)
            return
        await app.bot.send_message(
            chat_id=int(row["telegram_id"]),
            text=report_text(report),
            parse_mode=ParseMode.HTML,
        )

    return deliver


def register(app, *, path: str | Path | None = None) -> None:
    """Хендлер ставит себя сам: bot/app.py никто не трогает."""
    rnp.set_delivery(make_delivery(app, path))
    rnp.register_jobs()

    guard = require_module(rnp.MODULE, path=path)
    app.add_handler(
        CommandHandler("plan", guard(functools.partial(plan_command, path=path)))
    )
    app.add_handler(CommandHandler("rnp", guard(functools.partial(rnp_command, path=path))))
