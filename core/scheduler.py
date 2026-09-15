"""Расписание фоновых работ.

Две повторяющиеся истории. Ежедневная: утром по Москве, время и часовой
пояс из конфига. Еженедельная: не по дню недели, а когда у Wildberries
появился новый финансовый отчёт.

Здесь только механика. Что именно делать утром и по новому отчёту,
регистрируют владельцы самих задач: `register_daily`, `register_weekly`.
Работа всегда идёт через очередь, поэтому переживает перезапуск и
получает повторы при недоступности WB.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from core import config, db, queue

logger = logging.getLogger(__name__)

DEFAULT_TIMEZONE = "Europe/Moscow"
DEFAULT_DAILY_AT = "09:00"
DEFAULT_WEEKLY_CHECK_HOURS = 6  # запасное значение, если ключа в конфиге нет

# Смещения на случай, когда на машине нет базы часовых поясов. Москва
# живёт на UTC+3 круглый год, перевода часов нет с 2014 года.
FIXED_OFFSETS = {"Europe/Moscow": 3, "UTC": 0}

_daily: dict[str, Callable[..., Any]] = {}
_weekly: dict[str, Callable[..., Any]] = {}
_report_probe: Callable[[int], Any] | None = None
_clients_provider: Callable[..., Any] | None = None


def reset() -> None:
    """Очищает расписание и подставленные обработчики. Нужно тестам."""
    global _report_probe, _clients_provider
    _daily.clear()
    _weekly.clear()
    _report_probe = None
    _clients_provider = None


# --- конфиг ---


def _schedule_conf() -> dict[str, Any]:
    try:
        return dict(config.settings().get("schedule", {}))
    except Exception:  # noqa: BLE001 - расписание не роняет бота
        return {}


def timezone_name() -> str:
    return str(_schedule_conf().get("timezone") or DEFAULT_TIMEZONE)


def tz() -> Any:
    """Часовой пояс расписания.

    Если на машине нет базы часовых поясов, берётся фиксированное смещение:
    лучше правильное время по известному городу, чем падение при старте.
    """
    name = timezone_name()
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - на Windows базы поясов может не быть
        hours = FIXED_OFFSETS.get(name)
        if hours is None:
            logger.warning("часовой пояс %s неизвестен, расписание пойдёт по UTC", name)
            hours = 0
        return timezone(timedelta(hours=hours))


def daily_time() -> time:
    """Время ежедневного запуска, по умолчанию 09:00."""
    raw = str(_schedule_conf().get("daily_at") or DEFAULT_DAILY_AT)
    try:
        hour, minute = (int(part) for part in raw.split(":", 1))
        return time(hour=hour, minute=minute)
    except (TypeError, ValueError):
        logger.warning("время %s в конфиге не разобрать, беру %s", raw, DEFAULT_DAILY_AT)
        return time(hour=9, minute=0)


def weekly_check_hours() -> int:
    """Часы между проверками нового финотчёта. Из конфига, секция schedule."""
    raw = _schedule_conf().get("weekly_check_hours", DEFAULT_WEEKLY_CHECK_HOURS)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        logger.warning("значение %s в конфиге не разобрать, беру %s часов", raw, DEFAULT_WEEKLY_CHECK_HOURS)
        return DEFAULT_WEEKLY_CHECK_HOURS


# --- регистрация ---


def register_daily(name: str, fn: Callable[..., Any]) -> None:
    """Работа каждое утро. `fn(task)` выполнится воркером очереди."""
    _daily[name] = fn
    queue.register(name, fn)


def register_weekly(name: str, fn: Callable[..., Any]) -> None:
    """Работа при появлении нового финотчёта WB. `fn(task)` в очереди.

    В `task.payload` лежит `report_id` отчёта, который это вызвал.
    """
    _weekly[name] = fn
    queue.register(name, fn)


def set_report_probe(fn: Callable[[int], Any] | None) -> None:
    """Чем узнавать про новый финотчёт: `fn(client_id) -> report_id | None`."""
    global _report_probe
    _report_probe = fn


def set_clients_provider(fn: Callable[..., Any] | None) -> None:
    """Кого обходить при проверке. По умолчанию все клиенты базы."""
    global _clients_provider
    _clients_provider = fn


def daily_names() -> tuple[str, ...]:
    return tuple(_daily)


def weekly_names() -> tuple[str, ...]:
    return tuple(_weekly)


# --- запуск ---


def _today() -> str:
    return datetime.now(tz()).strftime("%Y-%m-%d")


def _already_queued(rows: list[Any], field: str, value: Any) -> bool:
    """Такая задача уже стоит или уже отработала."""
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get(field) == value:
            return True
    return False


def run_daily(path: str | Path | None = None) -> list[int]:
    """Ставит в очередь утренние задачи за сегодня.

    Повтор за тот же день ничего не добавляет: перезапуск бота утром не
    заставит клиентов получить отчёт дважды.
    """
    today = _today()
    repo = db.admin_repo(path)
    queued: list[int] = []
    for name in _daily:
        # Отбор по виду задачи делает запрос, а не срез последних строк:
        # иначе утренняя задача продублировалась бы, как только очередь
        # подрастёт задачами других видов.
        if _already_queued(repo.tasks_by_kind(name), "date", today):
            continue
        # Расписание клиенту ничего не обещало, «принято» тут ни к чему.
        queued.append(queue.enqueue(None, name, {"date": today}, notify=False, path=path))
    return queued


def _clients(path: str | Path | None) -> list[int]:
    if _clients_provider is not None:
        return [int(value) for value in _clients_provider()]
    return [int(row["id"]) for row in db.admin_repo(path).all_clients()]


def check_weekly(path: str | Path | None = None) -> list[int]:
    """Ставит недельные задачи тем клиентам, у кого появился новый финотчёт.

    Ничего не появилось - ничего и не ставится: это и есть привязка к
    данным вместо будильника по дню недели.
    """
    if not _weekly or _report_probe is None:
        return []

    queued: list[int] = []
    for client_id in _clients(path):
        try:
            report_id = _report_probe(client_id)
        except Exception:  # noqa: BLE001 - один клиент не ломает обход
            logger.exception("не удалось проверить финотчёт клиента %s", client_id)
            continue
        if report_id is None:
            continue
        repo = db.repo(client_id, path)
        for name in _weekly:
            # Отбор по виду задачи делает сам запрос, а не срез последних строк.
            if _already_queued(repo.rows("tasks", kind=name), "report_id", report_id):
                continue
            queued.append(
                queue.enqueue(
                    client_id, name, {"report_id": report_id}, notify=False, path=path
                )
            )
    return queued


def install(job_queue: Any, path: str | Path | None = None) -> list[str]:
    """Ставит джобы в `JobQueue` бота. Возвращает имена поставленных работ."""
    installed: list[str] = []
    moment = daily_time().replace(tzinfo=tz())

    if _daily:
        async def morning_job(_context: Any = None) -> None:
            run_daily(path=path)

        job_queue.run_daily(morning_job, time=moment, name="wbrentgen_daily")
        installed.append("wbrentgen_daily")

    if _weekly:
        async def report_check_job(_context: Any = None) -> None:
            check_weekly(path=path)

        job_queue.run_repeating(
            report_check_job,
            interval=timedelta(hours=weekly_check_hours()),
            first=timedelta(seconds=30),
            name="wbrentgen_weekly_check",
        )
        installed.append("wbrentgen_weekly_check")

    return installed
