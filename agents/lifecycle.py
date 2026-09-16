"""Жизненный цикл клиента: рассылки, продление, льготный период, удаление.

Своей бизнес-логики здесь почти нет. Модуль соединяет уже готовые части:
расписание (core.scheduler), состояния доступа (core.access), отчёты агентов
(agents.finance, agents.rnp), счета (bot.handlers.billing) и физическое
удаление данных (core.clients.disconnect).

**Почему он в agents, а не в core.** Он знает про агентов: какую задачу
кому поставить и когда. Зависимость в проекте идёт в одну сторону, агенты
на core, и модуль, которому нужны агенты, в core лежать не может: первому
же агенту, которому понадобятся тумблеры или сроки хранения, достался бы
круговой импорт. Здесь же это обычный сосед по слою.

В WB отсюда никто не ходит и отчётов никто не строит: выгрузку жизненный
цикл заказывает задачей по её виду (finance.request_collect), а готовое
отдаёт сам агент (finance.deliver строит по базе и в WB не ходит).

Два правила, которые держат всё остальное:

1. **Выключенная рассылка значит «не присылай мне отчёт», а не «не собирай
   данные».** Сбор суточных данных идёт всегда и у всех подключённых
   кабинетов: потерянную историю WB не отдаст обратно, а подписку клиент
   оформит и завтра. Тумблеры гасят только доставку.
2. **Все сроки из конфига,** секция [access]. В коде нет ни пятёрки, ни
   тройки, ни тридцатки.

Текстов здесь нет: их пишет bot/handlers/settings.py, ему же и отправлять.
"""

from __future__ import annotations

import functools
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from agents import finance, rnp
from core import access, audit, clients, config, db, queue, scheduler

logger = logging.getLogger(__name__)

__all__ = [
    "Prefs",
    "Event",
    "prefs",
    "set_daily",
    "set_weekly",
    "set_daily_time",
    "daily_enabled",
    "weekly_enabled",
    "renewal_notice_days",
    "retention_days",
    "retention_notice_days",
    "grace_days",
    "check",
    "daily_job",
    "weekly_job",
    "fan_out_daily",
    "new_report_of",
    "set_notifier",
    "register_jobs",
    "DAILY_JOB",
    "WEEKLY_JOB",
    "REPORTS_JOB",
    "COLLECT_JOB",
    "fan_out_collect",
    "RENEWAL",
    "GRACE",
    "SHUTDOWN",
    "RETENTION",
    "DELETED",
]

# Ключ в clients.settings, где живут тумблеры и время рассылки. Рядом с ним
# в том же словаре лежит token_reminders таска 05: читаем и пишем аккуратно.
SETTINGS_KEY = "notifications"
# Ключ с отметками об уже отправленных предупреждениях жизненного цикла.
MARKS_KEY = "lifecycle"

# Виды событий. Они же имена отметок в settings.
RENEWAL = "renewal"      # скоро конец доступа, пора предложить счёт
GRACE = "grace"          # срок вышел, идут льготные дни
SHUTDOWN = "shutdown"    # льготные дни кончились, модуль выключен
RETENTION = "retention"  # скоро удаление данных
DELETED = "deleted"      # данные удалены

# Имена работ в расписании.
DAILY_JOB = "lifecycle_daily"      # обход сроков: продление, льгота, удаление
REPORTS_JOB = "lifecycle_reports"  # утренняя рассылка: тумблеры и время клиента
COLLECT_JOB = "lifecycle_finance_collect"  # суточный сбор финансовых данных
WEEKLY_JOB = "lifecycle_weekly"    # новый финотчёт WB появился

_TIME_FORMAT = "%H:%M"


# --- сроки из конфига ---------------------------------------------------------


def _access_conf() -> dict[str, Any]:
    try:
        return dict(config.settings().get("access", {}))
    except Exception:  # noqa: BLE001 - жизненный цикл не роняет бота
        return {}


def _days(name: str, default: int) -> int:
    try:
        return max(0, int(_access_conf().get(name, default)))
    except (TypeError, ValueError):
        logger.warning("значение %s в конфиге не разобрать, беру %s", name, default)
        return default


def grace_days() -> int:
    """Льготные дни. Считает core.access, второго счёта тут не заводим."""
    return access.grace_days()


def renewal_notice_days() -> int:
    """За сколько дней до конца доступа предложить продлить."""
    return _days("renewal_notice_days", 5)


def retention_days() -> int:
    """Сколько данные живут после выключения всех модулей."""
    return _days("retention_days", 30)


def retention_notice_days() -> int:
    """За сколько дней до удаления предупредить."""
    return _days("retention_notice_days", 3)


# --- настройки клиента --------------------------------------------------------


@dataclass(frozen=True)
class Prefs:
    """Что клиент выбрал в /settings."""

    client_id: int
    daily: bool = True
    weekly: bool = True
    daily_at: time = time(9, 0)

    @property
    def daily_at_text(self) -> str:
        return self.daily_at.strftime(_TIME_FORMAT)


def _settings(client_id: int, path: str | Path | None) -> dict:
    """Весь словарь настроек клиента. Испорченный JSON это пустой словарь."""
    row = db.admin_repo(path).client(client_id)
    if row is None:
        return {}
    try:
        data = json.loads(row["settings"] or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(client_id: int, data: dict, path: str | Path | None) -> None:
    # Пишем весь словарь, но только после чтения: рядом живут чужие ключи.
    db.admin_repo(path).set_client_fields(
        client_id, settings=json.dumps(data, ensure_ascii=False)
    )


def parse_time(value: Any) -> time:
    """«ЧЧ:ММ» или time в time. Мусор это ValueError, а не тихое 09:00."""
    if isinstance(value, time):
        return value.replace(second=0, microsecond=0)
    try:
        parsed = datetime.strptime(str(value).strip(), _TIME_FORMAT)
    except (TypeError, ValueError):
        raise ValueError(f"время {value!r} не разобрать, нужно ЧЧ:ММ") from None
    return parsed.time()


def prefs(client_id: int, *, path: str | Path | None = None) -> Prefs:
    """Тумблеры и время клиента. По умолчанию обе рассылки включены."""
    raw = _settings(client_id, path).get(SETTINGS_KEY)
    raw = raw if isinstance(raw, dict) else {}
    try:
        at = parse_time(raw.get("daily_at")) if raw.get("daily_at") else scheduler.daily_time()
    except ValueError:
        at = scheduler.daily_time()
    return Prefs(
        client_id=int(client_id),
        daily=bool(raw.get("daily", True)),
        weekly=bool(raw.get("weekly", True)),
        daily_at=at,
    )


def _set(client_id: int, key: str, value: Any, path: str | Path | None) -> Prefs:
    data = _settings(client_id, path)
    current = data.get(SETTINGS_KEY)
    current = dict(current) if isinstance(current, dict) else {}
    current[key] = value
    data[SETTINGS_KEY] = current
    _save(client_id, data, path)
    return prefs(client_id, path=path)


def set_daily(client_id: int, enabled: bool, *, path: str | Path | None = None) -> Prefs:
    """Тумблер ежедневной рассылки. Сбор данных он не трогает."""
    return _set(client_id, "daily", bool(enabled), path)


def set_weekly(client_id: int, enabled: bool, *, path: str | Path | None = None) -> Prefs:
    """Тумблер еженедельной рассылки. Сбор данных он не трогает."""
    return _set(client_id, "weekly", bool(enabled), path)


def set_daily_time(
    client_id: int, value: Any, *, path: str | Path | None = None
) -> Prefs:
    """Время ежедневной рассылки по часовому поясу расписания."""
    return _set(client_id, "daily_at", parse_time(value).strftime(_TIME_FORMAT), path)


def daily_enabled(client_id: int, *, path: str | Path | None = None) -> bool:
    return prefs(client_id, path=path).daily


def weekly_enabled(client_id: int, *, path: str | Path | None = None) -> bool:
    return prefs(client_id, path=path).weekly


# --- отметки об отправленном --------------------------------------------------


def _marks(client_id: int, path: str | Path | None) -> dict:
    raw = _settings(client_id, path).get(MARKS_KEY)
    return dict(raw) if isinstance(raw, dict) else {}


def _marked(
    client_id: int, kind: str, key: str, stamp: str, path: str | Path | None
) -> bool:
    """Про это уже говорили? Ключ это модуль, значение это срок, о котором речь.

    Сравнивается именно срок, а не факт отметки: продлил доступ - срок стал
    другим, и предупреждение о новом конце придёт как положено.
    """
    section = _marks(client_id, path).get(kind)
    section = section if isinstance(section, dict) else {}
    return str(section.get(key, "")) == str(stamp)


def _mark(
    client_id: int, kind: str, key: str, value: str, path: str | Path | None
) -> None:
    data = _settings(client_id, path)
    marks = data.get(MARKS_KEY)
    marks = dict(marks) if isinstance(marks, dict) else {}
    section = dict(marks.get(kind) or {})
    section[key] = value
    marks[kind] = section
    data[MARKS_KEY] = marks
    _save(client_id, data, path)


# --- кому вообще что-то шлём --------------------------------------------------


def connected_clients(path: str | Path | None = None) -> list[int]:
    """Клиенты с подключённым кабинетом. Подписка тут ни при чём."""
    found: list[int] = []
    for row in db.admin_repo(path).all_clients():
        client_id = int(row["id"])
        try:
            if clients.connected(client_id, path=path):
                found.append(client_id)
        except Exception:  # noqa: BLE001 - один клиент не ломает обход
            logger.exception("не удалось проверить кабинет клиента %s", client_id)
    return found


def _payload(task: Any) -> dict:
    payload = getattr(task, "payload", None)
    return payload if isinstance(payload, dict) else {}


def _payload_date(task: Any) -> date:
    raw = _payload(task).get("date")
    try:
        return datetime.strptime(str(raw), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return datetime.now(scheduler.tz()).date()


# --- ежедневная рассылка ------------------------------------------------------


def fan_out_daily(task: Any, *, path: str | Path | None = None) -> list[int]:
    """Утренняя рассылка РНП: кому и когда.

    Рассылка это жизненный цикл, а не агент: она одна знает про тумблеры и
    про выбранное клиентом время. Сбор суточных данных остаётся за агентом
    и тумблеров не спрашивает - иначе выключенная рассылка стоила бы клиенту
    истории, которую WB больше не отдаст.

    Время клиента передаётся очереди как `run_at`: бот просыпается один раз
    утром, а задача ждёт своего часа в базе и переживает перезапуск.
    """
    day = _payload_date(task)
    stamp = day.isoformat()
    queued: list[int] = []
    for client_id in connected_clients(path):
        if not access.has_access(client_id, rnp.MODULE, path=path):
            continue
        if not daily_enabled(client_id, path=path):
            continue
        # Очередь хранит время в UTC и сравнивает его с UTC: момент клиента
        # переводим здесь, иначе задача ждала бы лишние часы.
        moment = datetime.combine(
            day, prefs(client_id, path=path).daily_at, scheduler.tz()
        ).astimezone(timezone.utc)
        queued.append(
            queue.enqueue(
                client_id,
                rnp.REPORT_ONE,
                {"date": stamp},
                run_at=moment,
                notify=False,
                path=path,
            )
        )
    return queued


# --- еженедельная рассылка ----------------------------------------------------


def new_report_of(client_id: int, *, path: str | Path | None = None) -> int | None:
    """Появился ли у клиента финотчёт WB, о котором мы ещё не говорили.

    Смотрим в базу, а не в календарь: недельная раскладка уходит тогда,
    когда данные появились, а не в назначенный день недели. Строки в
    fin_weeks кладёт сбор финансиста (см. scan_client ниже).
    """
    try:
        rows = db.repo(client_id, path).rows("fin_weeks")
    except Exception:  # noqa: BLE001 - один клиент не ломает обход
        logger.exception("не удалось проверить финотчёты клиента %s", client_id)
        return None
    if not rows:
        return None
    latest = max(int(row["report_id"]) for row in rows)
    seen = _marks(client_id, path).get("weekly_report")
    try:
        if seen is not None and int(latest) <= int(seen):
            return None
    except (TypeError, ValueError):
        pass
    return latest


async def weekly_job(
    task: Any, *, client_id: int | None = None, path: str | Path | None = None
) -> Any:
    """Новый финотчёт появился: отдать недельную раскладку тем, кто её ждёт.

    Отчёт строит и отправляет финансист по уже собранному: данные к этому
    моменту в базе, их положил суточный сбор, и повторная выгрузка из WB тут
    не нужна - она самая дорогая работа в проекте.

    Отметка о том, что отчёт учтён, ставится в любом случае: выключенная
    рассылка не должна превращать один финотчёт в вечную очередь задач.
    """
    client_id = int(client_id if client_id is not None else task.client_id)
    report_id = _payload(task).get("report_id")

    data = _settings(client_id, path)
    marks = data.get(MARKS_KEY)
    marks = dict(marks) if isinstance(marks, dict) else {}
    if report_id is not None:
        marks["weekly_report"] = int(report_id)
    data[MARKS_KEY] = marks
    _save(client_id, data, path)

    if not access.has_access(client_id, finance.MODULE, path=path):
        return None
    if not weekly_enabled(client_id, path=path):
        return None
    return await finance.deliver(client_id, "week", path=path)


# --- суточный сбор финансовых данных ------------------------------------------


def fan_out_collect(task: Any, *, path: str | Path | None = None) -> list[int]:
    """Раз в сутки поставить финансисту сбор недели. Задача по виду, не вызов.

    **Собираем только тем, у кого модуль finance работает, и это решение, а
    не экономия на спичках.** Отчёт о реализации WB отдаёт с 29 января 2024
    года, то есть задним числом его можно забрать всегда: не собранное
    сегодня не теряется, в отличие от суточной воронки РНП, где сбор идёт у
    всех подключённых. А лимит у этого отчёта самый жёсткий в проекте, один
    запрос в минуту на кабинет, и потраченный на неплательщика он забран у
    того, кто платит. Оплатив, клиент получает историю сразу: команда
    /finance за месяц или квартал поднимет её задним числом, а недельная
    рассылка пойдёт с ближайшего нового финотчёта.

    Тумблер рассылки здесь не спрашивается: он про «не присылай мне отчёт»,
    а не про «не собирай данные».
    """
    return [
        finance.request_collect(client_id, "week", path=path)
        for client_id in connected_clients(path)
        if access.has_access(client_id, finance.MODULE, path=path)
    ]


# --- доставка -----------------------------------------------------------------

_notifier: Callable[[int, "Event"], Any] | None = None


def set_notifier(fn: Callable[[int, "Event"], Any] | None) -> None:
    """Чем уходят предупреждения клиенту. Текстов здесь нет, их пишет бот."""
    global _notifier
    _notifier = fn


def notifier() -> Callable[[int, "Event"], Any] | None:
    return _notifier


# --- события жизненного цикла -------------------------------------------------


@dataclass(frozen=True)
class Event:
    """Что случилось с доступом клиента. Текст по событию пишет бот."""

    kind: str
    client_id: int
    module: str = ""
    days_left: int = 0
    until: datetime | None = None

    @property
    def key(self) -> str:
        """Чему принадлежит отметка: модуль, а для хранения данных - весь клиент."""
        return self.module or "*"

    @property
    def stamp(self) -> str:
        return "" if self.until is None else self.until.strftime("%Y-%m-%d %H:%M:%S")


def _utc(moment: datetime | None) -> datetime:
    if moment is None:
        return datetime.now(timezone.utc)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _modules_of(client_id: int, path: str | Path | None) -> list[str]:
    """Модули, которые клиент действительно покупал или пробовал.

    Берём строки доступа, а не список из конфига: пакет «всё сразу» иначе
    превратил бы одно предупреждение в три.
    """
    try:
        rows = db.repo(client_id, path).rows("module_access")
    except Exception:  # noqa: BLE001 - один клиент не ломает обход
        logger.exception("не удалось прочитать доступы клиента %s", client_id)
        return []
    return [str(row["module"]) for row in rows]


def off_since(
    client_id: int, *, now: datetime | None = None, path: str | Path | None = None
) -> datetime | None:
    """Когда у клиента перестал работать последний модуль.

    None, если что-то ещё работает или если модулей не было вовсе: хранение
    считается от выключения, а не от регистрации. Клиент, который ничего не
    включал, под удаление не попадает - удалять ему нечего, а ошибиться тут
    нельзя.
    """
    moment = _utc(now)
    last: datetime | None = None
    for module in _modules_of(client_id, path):
        item = access.access_of(client_id, module, now=moment, path=path)
        if item.works:
            return None
        if item.until is None:
            continue
        stopped = item.until + timedelta(days=grace_days())
        last = stopped if last is None or stopped > last else last
    return last


def check(
    client_id: int, *, now: datetime | None = None, path: str | Path | None = None
) -> list[Event]:
    """Что нужно сказать клиенту сегодня. Ничего не шлёт и ничего не меняет.

    Отметки об уже сказанном учитываются здесь же: повторов не будет, а
    пропущенный день не потеряет предупреждение - условие «порог перейдён»,
    а не «сегодня ровно столько-то».
    """
    moment = _utc(now)

    gone = off_since(client_id, now=moment, path=path)
    if gone is not None:
        left = retention_days() - (moment - gone).days
        if left <= 0:
            # Удаление старше любых предупреждений: говорить про продление
            # тому, чьи данные уже пора стирать, поздно и незачем.
            return [
                Event(
                    kind=DELETED,
                    client_id=int(client_id),
                    days_left=0,
                    until=gone + timedelta(days=retention_days()),
                )
            ]

    events: list[Event] = []
    for module in _modules_of(client_id, path):
        item = access.access_of(client_id, module, now=moment, path=path)
        event: Event | None = None
        if item.state == access.ACTIVE and item.until is not None:
            if item.days_left <= renewal_notice_days():
                event = Event(RENEWAL, int(client_id), module, item.days_left, item.until)
        elif item.state == access.GRACE and item.until is not None:
            ends = item.until + timedelta(days=grace_days())
            event = Event(GRACE, int(client_id), module, (ends - moment).days, item.until)
        elif item.state == access.OFF and item.until is not None:
            event = Event(SHUTDOWN, int(client_id), module, 0, item.until)
        if event is not None and not _marked(
            client_id, event.kind, event.key, event.stamp or "1", path
        ):
            events.append(event)

    if gone is not None:
        left = retention_days() - (moment - gone).days
        if left <= retention_notice_days():
            warning = Event(
                kind=RETENTION,
                client_id=int(client_id),
                days_left=max(0, left),
                until=gone + timedelta(days=retention_days()),
            )
            if not _marked(client_id, RETENTION, "*", warning.stamp or "1", path):
                events.append(warning)
    return events


def remember(client_id: int, event: Event, *, path: str | Path | None = None) -> None:
    """Отмечает, что предупреждение отправлено.

    Отметка привязана к сроку: продлил доступ - срок другой, и предупреждение
    о новом конце придёт снова, само собой, без чистки отметок.
    """
    if event.kind == DELETED:
        return
    stamp = event.stamp or "1"
    _mark(client_id, event.kind, event.key, stamp, path)


def erase(client_id: int, *, path: str | Path | None = None) -> dict[str, int]:
    """Физическое удаление всех данных клиента. Отменить это нельзя.

    Самого удаления здесь нет: оно уже написано в core.clients.disconnect и
    там же проверяет, что не осталось ни строки. Второго удаления в проекте
    быть не должно.
    """
    removed = clients.disconnect(client_id, path=path)
    # Клиента в журнале нет: строки клиента удалены, и запись остаётся
    # обезличенной - сколько чего стёрли, без того, чьё оно было.
    audit.log(
        DELETED,
        None,
        f"Удалил данные клиента по сроку хранения: {removed or 'пусто'}.",
        path=path,
    )
    return removed


async def daily_job(
    task: Any = None, *, now: datetime | None = None, path: str | Path | None = None
) -> list[Event]:
    """Утренний обход: продление, льготный период, выключение, удаление.

    Порядок внутри одного клиента важен: сообщение уходит раньше удаления,
    иначе писать будет уже некому.
    """
    moment = _utc(now)
    done: list[Event] = []
    for row in db.admin_repo(path).all_clients():
        client_id = int(row["id"])
        try:
            events = check(client_id, now=moment, path=path)
        except Exception:  # noqa: BLE001 - один клиент не ломает обход
            logger.exception("не удалось проверить клиента %s", client_id)
            continue
        for event in events:
            if _notifier is not None:
                try:
                    result = _notifier(client_id, event)
                    if hasattr(result, "__await__"):
                        await result
                except Exception:  # noqa: BLE001 - молчание Telegram не авария
                    logger.exception("не удалось предупредить клиента %s", client_id)
            try:
                if event.kind == DELETED:
                    erase(client_id, path=path)
                else:
                    remember(client_id, event, path=path)
            except Exception:  # noqa: BLE001 - один клиент не ломает обход
                logger.exception("не удалось закрыть событие клиента %s", client_id)
                continue
            done.append(event)
    return done


# --- регистрация в расписании -------------------------------------------------


def register_jobs(*, path: str | Path | None = None) -> None:
    """Ставит работы жизненного цикла в расписание.

    Чужих работ здесь не регистрируется: каждый агент ставит свои сам,
    иначе порядок старта бота зависел бы от того, кто кого импортировал
    первым.

    Утренняя рассылка РНП стоит под своим именем и принадлежит жизненному
    циклу: только он знает про тумблеры и про выбранное клиентом время.
    Агент её больше не регистрирует, поэтому рассылка в расписании одна.

    Суточный сбор финансиста стоит здесь же: без него новых недель в базе
    не появлялось бы, а недельная рассылка ждёт именно появления новой.
    """
    scheduler.register_daily(REPORTS_JOB, functools.partial(fan_out_daily, path=path))
    scheduler.register_daily(DAILY_JOB, functools.partial(daily_job, path=path))
    scheduler.register_daily(COLLECT_JOB, functools.partial(fan_out_collect, path=path))
    scheduler.set_report_probe(functools.partial(new_report_of, path=path))
    scheduler.register_weekly(WEEKLY_JOB, functools.partial(weekly_job, path=path))
