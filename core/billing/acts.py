"""Реестр оплаченных счетов: то, из чего владелец готовит акты.

Реестр собирается из счетов, поэтому и живёт рядом с ними, а не в учёте
вызовов. Состав колонок из ТЗ и меняться не должен: номер, дата, ИНН,
название клиента, модуль, период, сумма.

Здесь же границы месяца: у счёта дата оплаты, и «месяц» для реестра и для
сводки владельца должен быть один и тот же отрезок. Пояс проекта один -
`core.scheduler.tz()`, месяц кончается в московскую полночь.

Таблицу счетов этот модуль не читает: счета ему приносят готовыми. Так он
остаётся про форму реестра, а правило «к данным клиента только через
repo(client_id)» держится там, где живёт выборка.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from core import config, xlsx

ACTS_SHEET = "Оплаченные счета"
ACTS_HEADERS = (
    "Номер счёта",
    "Дата оплаты",
    "ИНН",
    "Название клиента",
    "Модуль",
    "Период, мес.",
    "Сумма, ₽",
)

MONTHS = (
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
)


class BadPeriod(ValueError):
    """Месяц написан не так. Текст готов для владельца."""


def _tz():
    from core import scheduler  # поздний импорт: расписание знает про очередь

    return scheduler.tz()


def _local_today(today: date | None = None) -> date:
    return today if today is not None else datetime.now(_tz()).date()


def _local_midnight(day: date) -> datetime:
    """Полночь по часовому поясу проекта, переведённая в UTC."""
    return datetime(day.year, day.month, day.day, tzinfo=_tz()).astimezone(timezone.utc)


def _first_of(day: date) -> date:
    return date(day.year, day.month, 1)


def _parse_period(period: Any, today: date | None) -> date:
    """Месяц из «ГГГГ-ММ», из даты или, если не сказали, текущий."""
    if period is None or period == "":
        return _first_of(_local_today(today))
    if isinstance(period, datetime):
        return _first_of(period.astimezone(_tz()).date())
    if isinstance(period, date):
        return _first_of(period)
    text = str(period).strip()
    try:
        year, month = (int(part) for part in text.split("-", 1))
        return date(year, month, 1)
    except (ValueError, TypeError) as exc:
        raise BadPeriod(text) from exc


def month_bounds(period: Any = None, *, today: date | None = None) -> tuple[datetime, datetime]:
    """Границы месяца в UTC: [первое число, первое следующего)."""
    first = _parse_period(period, today)
    following = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
    return _local_midnight(first), _local_midnight(following)


def previous_month_bounds(today: date | None = None) -> tuple[datetime, datetime]:
    """Границы прошлого месяца: реестр первого числа собирает именно его."""
    first = _first_of(_local_today(today))
    previous = first - timedelta(days=1)
    return _local_midnight(_first_of(previous)), _local_midnight(first)


def month_title(start: datetime) -> str:
    """Месяц словами: «сентябрь 2026»."""
    local = start.astimezone(_tz())
    return f"{MONTHS[local.month - 1]} {local.year}"


def month_key(start: datetime) -> str:
    local = start.astimezone(_tz())
    return f"{local.year}-{local.month:02d}"


def in_period(moment: datetime | None, start: datetime, end: datetime) -> bool:
    """Попал ли момент в отрезок. Наивное время считается UTC, как в базе."""
    if moment is None:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return start <= moment.astimezone(timezone.utc) < end


def _module_title(module: str) -> str:
    info = config.modules().get(module)
    return info.title if info else module


def _day(moment: datetime | None) -> str:
    if moment is None:
        return "-"
    return moment.astimezone(_tz()).strftime("%d.%m.%Y")


def acts_rows(invoices: Iterable[Any]) -> list[list[Any]]:
    """Строки реестра: номер, дата, ИНН, название, модуль, период, сумма."""
    return [
        [
            item.number,
            _day(item.paid_at),
            item.inn or "",
            item.org_name or "",
            _module_title(item.module),
            item.period_months,
            float(item.amount),
        ]
        for item in invoices
    ]


def acts_book(invoices: Iterable[Any], *, path: str | Path | None = None) -> bytes:
    """Готовая книга Excel по уже собранным счетам. Собирает её core.xlsx."""
    sheet = xlsx.Sheet(ACTS_SHEET, list(ACTS_HEADERS), acts_rows(invoices))
    return xlsx.write_book([sheet], path=path)


def acts_file_name(start: datetime) -> str:
    return f"Оплаченные счета {month_key(start)}.xlsx"
