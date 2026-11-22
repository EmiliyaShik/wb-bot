"""Те, кто пришёл к боту, но ничего не оформил.

Отдельный модуль, потому что правила тут не такие, как у клиентов. Клиенту
бот пишет по договору: напомнить об окончании оплаченного периода можно и
нужно. Человеку, который ничего не купил, писать можно **только с его
согласия**: это реклама, а реклама по сетям связи разрешена с
предварительного согласия (статья 18 закона «О рекламе»), и доказывать
согласие придётся тому, кто написал.

Отсюда всё устройство:

* согласие и отказ хранятся явно, с датой, в настройках клиента;
* отказ хранится дольше самого человека: без списка «не писать» бот через
  год напишет снова и получит жалобу, а жалоба в Telegram стоит бота;
* число напоминаний и пауза между ними ограничены ровно теми числами,
  которые названы в политике конфиденциальности;
* данные того, кто не вернулся, удаляются по сроку из `config.toml`, как
  той же политикой и обещано.

Два предиката, и они разные намеренно. **Не оформил доступ** (`unpaid`) это
«ни одной выдачи доступа не было»: такому человеку можно напоминать, даже
если кабинет он подключил. **Нечего хранить** (`bare`) это вдобавок «кабинет
не подключён»: только у такого человека данные удаляются по молчанию, потому
что политика обещает подключённому кабинету хранение всё время, пока он
подключён. Слить эти два условия значит либо не напоминать тем, кому можно,
либо удалить данные работающего кабинета.

Сроки и количества живут в конфиге, в коде их нет.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from core import clients, config, db, scheduler

logger = logging.getLogger(__name__)

# Ключ в clients.settings. Соседи по словарю чужие, поэтому читаем и пишем
# весь словарь целиком, как велит core.clients.save_settings.
SETTINGS_KEY = "leads"

YES = "yes"
NO = "no"


def _conf() -> dict[str, Any]:
    try:
        return dict(config.settings().get("leads", {}))
    except Exception:  # noqa: BLE001 - конфиг не должен ронять чистку
        logger.exception("секцию [leads] конфига не разобрать")
        return {}


def _number(key: str, default: int, *, least: int = 0) -> int:
    try:
        return max(least, int(_conf().get(key, default)))
    except (TypeError, ValueError):
        logger.warning("значение %s в [leads] не разобрать, беру %s", key, default)
        return default


def max_reminders() -> int:
    """Сколько напоминаний всего. Ноль означает «не напоминать никому»."""
    return _number("max_reminders", 3)


def gap_days() -> int:
    """Пауза между напоминаниями. Чаще раза в месяц это навязчивость."""
    return _number("gap_days", 30, least=1)


def quiet_days() -> int:
    """Сколько дней молчания до удаления данных. Ноль выключает чистку."""
    return _number("quiet_days", 180)


def refusal_days() -> int:
    """Сколько держать отказ в списке «не писать» после удаления остального."""
    return _number("refusal_days", 365)


def _state(client_id: int, path: str | Path | None) -> dict[str, Any]:
    raw = clients.settings_of(client_id, path=path).get(SETTINGS_KEY)
    return dict(raw) if isinstance(raw, dict) else {}


def _save(client_id: int, state: dict[str, Any], path: str | Path | None) -> None:
    data = clients.settings_of(client_id, path=path)
    data[SETTINGS_KEY] = state
    clients.save_settings(client_id, data, path=path)


def _today(today: date | None = None) -> date:
    return today or datetime.now(scheduler.tz()).date()


def _as_date(raw: Any) -> date | None:
    try:
        return date.fromisoformat(str(raw)[:10])
    except (TypeError, ValueError):
        return None


def payload_date(task: Any) -> date:
    """Сегодняшний день работы расписания: он лежит в задаче, а не в часах.

    Воркер может взять утреннюю работу позже, чем она поставлена, и тогда
    «сегодня» у неё должно остаться тем же днём, за который она поставлена.
    """
    day = _as_date((getattr(task, "payload", None) or {}).get("date"))
    return day or _today()


# --- согласие ---------------------------------------------------------------


def choice(client_id: int, *, path: str | Path | None = None) -> str | None:
    """Что человек ответил про напоминания: `YES`, `NO` или None, если молчал."""
    value = _state(client_id, path).get("reminders")
    return value if value in (YES, NO) else None


def asked(client_id: int, *, path: str | Path | None = None) -> bool:
    """Спрашивали ли уже. Второй раз спрашивать значит навязываться."""
    return choice(client_id, path=path) is not None


def allowed(client_id: int, *, path: str | Path | None = None) -> bool:
    return choice(client_id, path=path) == YES


def answered_at(client_id: int, *, path: str | Path | None = None) -> date | None:
    """Когда человек ответил. Эту дату спросят, если согласие оспорят."""
    return _as_date(_state(client_id, path).get("answered_at"))


def set_choice(
    client_id: int,
    permit: bool,
    *,
    today: date | None = None,
    path: str | Path | None = None,
) -> None:
    """Сохраняет согласие или отказ вместе с датой: её спросят при споре."""
    state = _state(client_id, path)
    state["reminders"] = YES if permit else NO
    state["answered_at"] = _today(today).isoformat()
    if not permit:
        # Счётчик обнуляется, чтобы отказ нельзя было «переждать»: ни одного
        # напоминания после отказа не уйдёт, даже если лимит не исчерпан.
        state["sent"] = 0
    _save(client_id, state, path)


# --- последнее обращение ------------------------------------------------------


def seen(
    client_id: int, *, today: date | None = None, path: str | Path | None = None
) -> None:
    """Отмечает день, когда человек обращался к боту.

    Запись идёт не чаще раза в сутки: отметка нужна для срока хранения, а не
    для статистики, и писать в базу на каждое сообщение незачем.
    """
    day = _today(today).isoformat()
    state = _state(client_id, path)
    if state.get("seen_at") == day:
        return
    state["seen_at"] = day
    _save(client_id, state, path)


def last_seen(client_id: int, *, path: str | Path | None = None) -> date | None:
    return _as_date(_state(client_id, path).get("seen_at"))


def sent_count(client_id: int, *, path: str | Path | None = None) -> int:
    try:
        return int(_state(client_id, path).get("sent", 0))
    except (TypeError, ValueError):
        return 0


def note_mailing(
    client_id: int, *, today: date | None = None, path: str | Path | None = None
) -> None:
    """Отмечает день любой отправки: по нему считается обещанная пауза.

    Часы тут одни на всё, что бот шлёт сам, и это не упрощение. Политика
    обещает «не чаще одного раза в 30 дней», а не «по разу от каждой
    рассылки»: заведи новостям свой счёт, и человек получит напоминание и
    новость в одну неделю, причём оба раза «по правилам».
    """
    state = _state(client_id, path)
    state["sent_at"] = _today(today).isoformat()
    _save(client_id, state, path)


def note_sent(
    client_id: int, *, today: date | None = None, path: str | Path | None = None
) -> None:
    """Отмечает отправленное напоминание: число и день.

    Счётчик растёт только здесь. Предел в три письма это предел кампании
    напоминаний тому, кто ничего не оформил, а не предел переписки вообще:
    новость, отправленная платящему клиенту, его исчерпать не может.
    """
    state = _state(client_id, path)
    state["sent"] = sent_count(client_id, path=path) + 1
    state["sent_at"] = _today(today).isoformat()
    _save(client_id, state, path)


# --- кто считается «не оформившим» и у кого нечего хранить ----------------------


def unpaid(client_id: int, *, path: str | Path | None = None) -> bool:
    """Ни одной выдачи доступа не было: ни оплаты, ни пробного периода.

    Считается по строкам `module_access`, а не по состоянию доступа: тот, у
    кого доступ кончился, остаётся клиентом, и правила рекламы к нему не те.
    """
    return not db.repo(client_id, path).count("module_access")


def connected(client_id: int, *, path: str | Path | None = None) -> bool:
    return bool(db.repo(client_id, path).count("wb_tokens"))


@dataclass(frozen=True)
class Lead:
    """Человек без оплат: кто он, когда приходил, что ответил про напоминания."""

    client_id: int
    telegram_id: int
    last_seen: date | None
    choice: str | None
    sent: int
    connected: bool

    @property
    def bare(self) -> bool:
        """Нечего хранить: ни кабинета, ни оплат, только идентификатор."""
        return not self.connected


def leads(*, path: str | Path | None = None) -> list[Lead]:
    """Все, кто доступ не оформил. Сквозного запроса по клиентским таблицам
    тут нет: данные каждого читаются своим `repo(client_id)`."""
    found: list[Lead] = []
    for row in db.admin_repo(path).all_clients():
        client_id = int(row["id"])
        try:
            if not unpaid(client_id, path=path):
                continue
            state = _state(client_id, path)
            found.append(
                Lead(
                    client_id=client_id,
                    telegram_id=int(row["telegram_id"]),
                    # Не приходил ни разу после записи - считаем с записи:
                    # иначе у старых строк срок хранения не начался бы никогда.
                    last_seen=(
                        _as_date(state.get("seen_at")) or _as_date(row["created_at"])
                    ),
                    choice=choice(client_id, path=path),
                    sent=sent_count(client_id, path=path),
                    connected=connected(client_id, path=path),
                )
            )
        except Exception:  # noqa: BLE001 - один клиент не ломает обход
            logger.exception("не удалось разобрать клиента %s", client_id)
    return found


def due(*, today: date | None = None, path: str | Path | None = None) -> list[Lead]:
    """Кому пора напомнить: согласие есть, лимит не исчерпан, пауза прошла."""
    day = _today(today)
    limit, gap = max_reminders(), gap_days()
    if limit <= 0:
        return []
    ready: list[Lead] = []
    for lead in leads(path=path):
        if lead.choice != YES or lead.sent >= limit:
            continue
        # Отсчёт идёт от прошлого напоминания, а если его не было - от
        # последнего обращения: ответивший «напоминать» ждёт напоминания
        # потом, а не в ту же минуту.
        last = _as_date(_state(lead.client_id, path).get("sent_at")) or lead.last_seen
        if last is not None and day - last < timedelta(days=gap):
            continue
        ready.append(lead)
    return ready


@dataclass(frozen=True)
class Recipient:
    """Кому можно отправить новость: только номер клиента и чат."""

    client_id: int
    telegram_id: int


def news_due(
    *, today: date | None = None, path: str | Path | None = None
) -> list[Recipient]:
    """Кому можно отправить новость: согласие есть и пауза прошла.

    Отличий от `due` два, и оба намеренные. Платящий клиент сюда входит:
    новость про новый модуль нужна в первую очередь тому, кто уже платит, а
    `due` ищет только неоформивших, у них своя кампания. И предел в три
    письма тут не при чём: он про напоминания, а не про переписку вообще.

    Общее у двух рассылок одно, и оно главное: пауза. Считается она по тому
    же `sent_at`, то есть новость не уйдёт следом за напоминанием.
    """
    day = _today(today)
    gap = gap_days()
    found: list[Recipient] = []
    for row in db.admin_repo(path).all_clients():
        client_id = int(row["id"])
        try:
            if choice(client_id, path=path) != YES:
                continue
            last = _as_date(_state(client_id, path).get("sent_at"))
            if last is not None and day - last < timedelta(days=gap):
                continue
            found.append(
                Recipient(client_id=client_id, telegram_id=int(row["telegram_id"]))
            )
        except Exception:  # noqa: BLE001 - один клиент не ломает рассылку
            logger.exception("не удалось разобрать клиента %s", client_id)
    return found


def stale(*, today: date | None = None, path: str | Path | None = None) -> list[Lead]:
    """Чьи данные пора удалить: молчит дольше срока, кабинета и оплат нет.

    Отказавшийся живёт дольше: его идентификатор и есть список «не писать»,
    и удалить его значит однажды написать снова.
    """
    day = _today(today)
    quiet, refusal = quiet_days(), refusal_days()
    found: list[Lead] = []
    for lead in leads(path=path):
        if not lead.bare or lead.last_seen is None:
            continue
        limit = refusal if lead.choice == NO else quiet
        if limit <= 0:
            continue
        if day - lead.last_seen >= timedelta(days=limit):
            found.append(lead)
    return found
