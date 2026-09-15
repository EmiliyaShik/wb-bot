"""Банковские дни: сколько живёт счёт.

Банковский день это будний день, из которого вычеркнуты праздники. Список
праздников лежит в конфиге и по умолчанию пуст: производственный календарь
РФ меняется раз в год, и дописать в конфиг восемь дат проще, чем тянуть ради
них зависимость, которая всё равно устареет.

Функции тут чистые: дата на входе, дата на выходе. Ни базы, ни времени
запуска, ни часовых поясов.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable

from core import config

SATURDAY = 5

# Подстраховка от бесконечного цикла, если в конфиг попадёт вся неделя.
_MAX_STEPS = 366


def _as_date(value: date | str) -> date:
    """Дату принимаем и строкой ГГГГ-ММ-ДД: в конфиге она именно такая."""
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).strip())


def holidays() -> tuple[date, ...]:
    """Нерабочие дни сверх выходных, из секции [invoice] конфига.

    Неразобранные строки пропускаются молча: опечатка в конфиге не должна
    останавливать выставление счетов, а срок без одного праздника это
    меньшая беда, чем упавший бот.
    """
    raw = config.settings().get("invoice", {}).get("holidays", []) or []
    out: list[date] = []
    for item in raw:
        try:
            out.append(_as_date(item))
        except ValueError:
            continue
    return tuple(sorted(set(out)))


def valid_bank_days() -> int:
    """Сколько банковских дней живёт счёт. Из конфига, в коде числа нет."""
    return int(config.settings().get("invoice", {}).get("valid_bank_days", 5))


def is_bank_day(day: date | str, holidays_list: Iterable[date | str] | None = None) -> bool:
    """Будний и не праздник."""
    moment = _as_date(day)
    if moment.weekday() >= SATURDAY:
        return False
    known = holidays() if holidays_list is None else {_as_date(x) for x in holidays_list}
    return moment not in set(known)


def add_bank_days(
    start: date | str,
    days: int,
    holidays_list: Iterable[date | str] | None = None,
) -> date:
    """Дата через `days` банковских дней после `start`.

    Сам день выставления не считается: счёт, выставленный в пятницу на пять
    банковских дней, действует по следующую пятницу включительно.
    """
    moment = _as_date(start)
    left = int(days)
    if left < 1:
        return moment
    known = holidays() if holidays_list is None else {_as_date(x) for x in holidays_list}
    steps = 0
    while left > 0:
        steps += 1
        if steps > _MAX_STEPS:
            raise ValueError("в конфиге не осталось рабочих дней, проверьте holidays")
        moment += timedelta(days=1)
        if is_bank_day(moment, known):
            left -= 1
    return moment


def due_date(start: date | str, days: int | None = None) -> date:
    """Срок оплаты счёта: настройки из конфига, ничего не додумываем."""
    return add_bank_days(start, valid_bank_days() if days is None else days)
