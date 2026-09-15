"""Очередь фоновых задач.

Тяжёлая работа не делается в хендлере: она кладётся в таблицу `tasks`,
клиент сразу получает «принято», а результат приходит отдельно. Очередь
лежит в базе, а не в памяти, поэтому перезапуск процесса ничего не теряет.

Здесь только механика. Сами задачи (выгрузка отчёта, суточные данные,
рассылки) регистрируют их владельцы через `register(kind, fn)`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from core import audit, config, db
from core.wbapi import WBAuthError, WBForbiddenError, WBRateLimited, WBUnavailable

logger = logging.getLogger(__name__)

# Состояния задачи. `queued` это «ждёт своей очереди»: так называет его
# слой данных, и по нему же отбирает `AdminRepo.due_tasks`.
PENDING = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


@dataclass(frozen=True)
class Task:
    """Задача такой, какой её видит обработчик."""

    id: int
    client_id: int | None
    kind: str
    payload: dict[str, Any]
    attempts: int


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _stamp(moment: datetime) -> str:
    """Время в том же виде, в каком его пишет база: UTC, без часового пояса."""
    return moment.strftime(TIME_FORMAT)


def enqueue(
    client_id: int | None,
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    run_at: datetime | None = None,
    notify: bool = True,
    path: str | Path | None = None,
) -> int:
    """Ставит задачу в очередь и сразу возвращает управление.

    Клиенту тут же уходит «принято, пришлю, когда будет готово»: ждать
    этого обещания от каждого хендлера нельзя, его даёт сама очередь.
    `notify=False` для работ, о которых клиенту знать не надо: расписание,
    служебные задачи, повторная постановка.
    """
    body = json.dumps(payload or {}, ensure_ascii=False)
    next_run_at = _stamp(run_at) if run_at is not None else None
    if client_id is None:
        task_id = db.admin_repo(path).add_task(kind, body, next_run_at)
    else:
        task_id = db.repo(client_id, path).insert(
            "tasks",
            kind=kind,
            payload=body,
            state=PENDING,
            next_run_at=next_run_at,
        )
    if notify:
        _notify_now(client_id, ACCEPTED)
    return task_id


# --- реестр обработчиков ---

Handler = Callable[[Task], Any]

_handlers: dict[str, Handler] = {}


def register(kind: str, fn: Handler) -> None:
    """Связывает вид задачи с обработчиком. Зовут владельцы самих задач."""
    _handlers[kind] = fn


def handlers() -> dict[str, Handler]:
    return dict(_handlers)


def reset() -> None:
    """Очищает реестр и обработчики оповещений. Нужно тестам."""
    global _notifier, _auth_handler
    _handlers.clear()
    _notifier = None
    _auth_handler = None


def _payload(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _task_from_row(row: Any) -> Task:
    return Task(
        id=int(row["id"]),
        client_id=row["client_id"],
        kind=row["kind"],
        payload=_payload(row["payload"]),
        attempts=int(row["attempts"] or 0),
    )


def recover(path: str | Path | None = None) -> int:
    """Возвращает в очередь задачи, застрявшие в `running` после рестарта."""
    repo = db.admin_repo(path)
    stuck = repo.tasks(state=RUNNING, limit=1000)
    for row in stuck:
        repo.update_task(int(row["id"]), state=PENDING)
    return len(stuck)


async def _call(fn: Handler, task: Task) -> None:
    result = fn(task)
    if hasattr(result, "__await__"):
        await result


async def run_once(path: str | Path | None = None) -> bool:
    """Берёт одну задачу, которой пора, и выполняет её.

    Возвращает False, если очередь пуста. По одной задаче за раз: так
    фоновая работа не обгоняет бюджет запросов `core.wbapi`.
    """
    repo = db.admin_repo(path)
    rows = repo.due_tasks(_stamp(_now()), limit=1)
    if not rows:
        return False

    task = _task_from_row(rows[0])
    repo.update_task(task.id, state=RUNNING)
    fn = _handlers.get(task.kind)
    if fn is None:
        repo.update_task(
            task.id,
            state=FAILED,
            last_error=f"нет обработчика для задачи {task.kind}",
            finished_at=_stamp(_now()),
        )
        return True

    try:
        await _call(fn, task)
    except WBAuthError as error:
        await _cancel(repo, task, error, path, token=True)
    except WBForbiddenError as error:
        await _cancel(repo, task, error, path, token=False)
    except (WBRateLimited, WBUnavailable) as error:
        await _retry_or_give_up(repo, task, error, path)
    except Exception as error:  # noqa: BLE001 - воркер не имеет права упасть
        repo.update_task(
            task.id,
            state=FAILED,
            attempts=task.attempts + 1,
            last_error=str(error),
            finished_at=_stamp(_now()),
        )
        audit.log(
            "queue",
            None,
            f"задача {task.kind} #{task.id} клиента {task.client_id} сорвалась: {error}",
            level="error",
            path=path,
        )
        await _notify(task.client_id, GENERIC_FAILED_TEXT)
    else:
        repo.update_task(task.id, state=DONE, finished_at=_stamp(_now()))
    return True


async def _cancel(
    repo: Any, task: Task, error: BaseException, path: Any, *, token: bool
) -> None:
    """Повторять бессмысленно: дело в ключе, а не в доступности WB."""
    repo.update_task(
        task.id, state=CANCELLED, last_error=str(error), finished_at=_stamp(_now())
    )
    if token:
        audit.log(
            "queue",
            task.client_id,
            f"задача {task.kind} отменена: Wildberries не принял ключ доступа",
            level="warning",
            path=path,
        )
        # Про токен говорит тот, кто умеет ставить модули на паузу.
        # Если обработчика нет, клиент всё равно не остаётся в неведении.
        if not await _safe_call(_auth_handler, task, error):
            await _notify(task.client_id, TOKEN_TEXT)
        return

    category = str(getattr(error, "category", "") or "")
    audit.log(
        "queue",
        task.client_id,
        f"задача {task.kind} отменена: ключу не хватает категории {category or 'доступа'}",
        level="warning",
        path=path,
    )
    await _notify(task.client_id, forbidden_text(category))


async def _retry_or_give_up(repo: Any, task: Task, error: BaseException, path: Any) -> None:
    """WB не ответил: повторить с растущей паузой или сдаться понятным текстом."""
    attempts = task.attempts + 1
    limit = max_attempts()
    if attempts >= limit:
        repo.update_task(
            task.id,
            state=FAILED,
            attempts=attempts,
            last_error=str(error),
            finished_at=_stamp(_now()),
        )
        audit.log(
            "queue",
            None,
            f"задача {task.kind} #{task.id} клиента {task.client_id} остановлена"
            f" после {attempts} попыток: {error}",
            level="error",
            path=path,
        )
        await _notify(task.client_id, FAILED_TEXT)
        return

    # Паузу после 429 называет сам WB: это поле retry_after его ошибки.
    pause = error.retry_after if isinstance(error, WBRateLimited) else None
    delay = float(pause) if pause else _backoff(attempts)
    repo.update_task(
        task.id,
        state=PENDING,
        attempts=attempts,
        last_error=str(error),
        next_run_at=_stamp(_now() + timedelta(seconds=delay)),
    )


async def run_worker(
    *,
    path: str | Path | None = None,
    poll_sec: float = 5.0,
    stop: asyncio.Event | None = None,
) -> None:
    """Вечный цикл: подобрать брошенное после рестарта и разбирать очередь.

    Задачи берутся по одной, поэтому бюджет запросов WB не обходится.
    """
    recover(path)
    while stop is None or not stop.is_set():
        try:
            busy = await run_once(path=path)
        except Exception:  # noqa: BLE001 - воркер не имеет права упасть
            logger.exception("сбой цикла очереди")
            busy = False
        if busy:
            continue
        if stop is None:
            await asyncio.sleep(poll_sec)
            continue
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll_sec)
        except asyncio.TimeoutError:
            pass


# --- тексты клиенту. Русский, без кодов ошибок и слова «таймаут» ---

ACCEPTED = "Принято. Соберу и пришлю результат отдельным сообщением."

FAILED_TEXT = (
    "Не получилось собрать данные: Wildberries сейчас не отвечает. "
    "Я повторил несколько раз и остановился. Попробуйте позже, "
    "владелец бота уже видит эту заминку в журнале."
)

GENERIC_FAILED_TEXT = (
    "Не получилось выполнить эту работу. Запись об этом уже у владельца бота, "
    "он разберётся. Попробуйте повторить позже."
)

TOKEN_TEXT = (
    "Wildberries больше не принимает ваш ключ доступа. Выпустите новый "
    "в личном кабинете и пришлите его командой /connect. Пока ключ не обновлён, "
    "разборы по вашим продажам не собираются."
)


def forbidden_text(category: str = "") -> str:
    """Что сказать клиенту, когда у ключа не хватает категории доступа."""
    if category:
        return (
            f"Для этой работы ключу Wildberries не хватает категории «{category}». "
            "Выпустите ключ заново и отметьте её, тогда всё заработает."
        )
    return (
        "Ключу Wildberries не хватает прав на эти данные. Выпустите ключ заново "
        "и отметьте все категории, которые просит бот."
    )


# --- оповещения. Очередь не знает про телеграм, ей дают обработчики ---

_notifier: Callable[[int | None, str], Any] | None = None
_auth_handler: Callable[[Task, BaseException], Any] | None = None


def set_notifier(fn: Callable[[int | None, str], Any] | None) -> None:
    """Чем сообщать клиенту. Ставит поверхность бота при старте."""
    global _notifier
    _notifier = fn


def set_auth_handler(fn: Callable[[Task, BaseException], Any] | None) -> None:
    """Что делать, когда WB отклонил ключ: снять модули с паузы и написать."""
    global _auth_handler
    _auth_handler = fn


async def _safe_call(fn: Callable[..., Any] | None, *args: Any) -> bool:
    if fn is None:
        return False
    try:
        result = fn(*args)
        if hasattr(result, "__await__"):
            await result
    except Exception:  # noqa: BLE001 - оповещение не роняет воркер
        logger.exception("не удалось выполнить обработчик очереди")
    return True


async def _notify(client_id: int | None, text: str) -> None:
    if client_id is None:
        return
    await _safe_call(_notifier, client_id, text)


async def _await_quietly(awaitable: Any) -> None:
    try:
        await awaitable
    except Exception:  # noqa: BLE001 - молчание Telegram не наша авария
        logger.exception("не удалось отправить сообщение о постановке задачи")


def _notify_now(client_id: int | None, text: str) -> None:
    """Сообщение из синхронного места: постановка задачи ничего не ждёт.

    Обработчик в боте асинхронный, поэтому отправка уходит в текущий цикл
    событий. Цикла нет (скрипт, тест без asyncio) - зовём как обычную
    функцию и молчим, если отправить некому.
    """
    if client_id is None or _notifier is None:
        return
    try:
        result = _notifier(client_id, text)
    except Exception:  # noqa: BLE001 - оповещение не роняет постановку задачи
        logger.exception("не удалось сообщить клиенту %s о постановке задачи", client_id)
        return
    if not hasattr(result, "__await__"):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        result.close()
        logger.warning("сообщать клиенту %s некому: цикл событий не запущен", client_id)
        return
    loop.create_task(_await_quietly(result))


# --- настройки повторов ---


def _conf() -> dict[str, Any]:
    try:
        return dict(config.settings().get("queue", {}))
    except Exception:  # noqa: BLE001 - конфиг не должен ронять воркер
        return {}


def max_attempts() -> int:
    """Сколько раз пробовать, прежде чем сдаться. Из конфига, секция queue."""
    try:
        return max(1, int(_conf().get("attempts", 3)))
    except (TypeError, ValueError):
        return 3


def _backoff(attempts: int) -> float:
    conf = _conf()
    try:
        base = float(conf.get("retry_base_sec", 60))
        factor = float(conf.get("retry_factor", 3))
        cap = float(conf.get("retry_max_sec", 3600))
    except (TypeError, ValueError):
        base, factor, cap = 60.0, 3.0, 3600.0
    return min(base * (factor ** max(0, attempts - 1)), cap)
