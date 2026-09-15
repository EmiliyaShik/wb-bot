"""Очередь фоновых задач. Шов один: путь к базе.

Проверяется поведение через публичный интерфейс core.queue: постановка,
обещание клиенту, выполнение, повтор, исчерпание попыток, восстановление
после рестарта. Ошибки берутся настоящие, из core.wbapi.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from core import audit, config, db, queue
from core.wbapi import WBAuthError, WBForbiddenError, WBRateLimited, WBUnavailable


@pytest.fixture(autouse=True)
def clean_registry():
    queue.reset()
    yield
    queue.reset()


@pytest.fixture
def client(db_path):
    return db.admin_repo(db_path).ensure_client(100500)


def _move_into_past(db_path, task_id):
    """Делает вид, что пауза перед повтором уже вышла."""
    db.admin_repo(db_path).update_task(task_id, next_run_at="2000-01-01 00:00:00")


def _without_error_codes(text):
    lowered = text.lower()
    return not any(
        word in lowered
        for word in ("401", "403", "429", "500", "таймаут", "timeout", "error")
    )


def test_enqueue_stores_client_task(db_path, client):
    task_id = queue.enqueue(client, "finance_report", {"period": "2026-W37"}, path=db_path)

    row = db.admin_repo(db_path).task(task_id)
    assert row["client_id"] == client
    assert row["kind"] == "finance_report"
    assert json.loads(row["payload"]) == {"period": "2026-W37"}
    assert row["state"] == queue.PENDING
    assert row["attempts"] == 0


def test_enqueue_without_client_uses_admin_layer(db_path):
    task_id = queue.enqueue(None, "broadcast", {"text": "привет"}, path=db_path)

    row = db.admin_repo(db_path).task(task_id)
    assert row["client_id"] is None
    assert row["kind"] == "broadcast"


def test_enqueue_promises_the_client_a_result(db_path, client):
    messages = []
    queue.set_notifier(lambda client_id, text: messages.append((client_id, text)))

    queue.enqueue(client, "finance_report", path=db_path)

    assert messages == [(client, queue.ACCEPTED)]
    assert _without_error_codes(queue.ACCEPTED)


def test_enqueue_stays_silent_when_asked(db_path, client):
    messages = []
    queue.set_notifier(lambda client_id, text: messages.append(text))

    queue.enqueue(client, "rnp_daily", notify=False, path=db_path)
    queue.enqueue(None, "broadcast", path=db_path)

    assert messages == []


@pytest.mark.asyncio
async def test_promise_reaches_an_async_notifier(db_path, client):
    messages = []

    async def notifier(client_id, text):
        messages.append(text)

    queue.set_notifier(notifier)
    queue.enqueue(client, "finance_report", path=db_path)

    await asyncio.sleep(0)
    assert messages == [queue.ACCEPTED]


@pytest.mark.asyncio
async def test_worker_runs_the_task_and_closes_it(db_path, client):
    done = []

    async def handler(task):
        done.append(task)

    queue.register("finance_report", handler)
    task_id = queue.enqueue(client, "finance_report", {"period": "2026-W37"}, path=db_path)

    assert await queue.run_once(path=db_path) is True

    assert [task.payload for task in done] == [{"period": "2026-W37"}]
    assert done[0].client_id == client
    row = db.admin_repo(db_path).task(task_id)
    assert row["state"] == queue.DONE
    assert row["finished_at"]


@pytest.mark.asyncio
async def test_worker_takes_tasks_one_by_one(db_path, client):
    order = []

    async def handler(task):
        order.append(task.payload["n"])

    queue.register("step", handler)
    queue.enqueue(client, "step", {"n": 1}, path=db_path)
    queue.enqueue(client, "step", {"n": 2}, path=db_path)

    await queue.run_once(path=db_path)
    assert order == [1]

    await queue.run_once(path=db_path)
    assert order == [1, 2]

    assert await queue.run_once(path=db_path) is False


def test_restart_returns_unfinished_tasks_to_the_queue(db_path, client):
    task_id = queue.enqueue(client, "finance_report", path=db_path)
    db.admin_repo(db_path).update_task(task_id, state=queue.RUNNING)

    assert queue.recover(path=db_path) == 1

    assert db.admin_repo(db_path).task(task_id)["state"] == queue.PENDING


def test_attempts_come_from_config(monkeypatch):
    monkeypatch.setattr(config, "settings", lambda: {"queue": {"attempts": 5}})
    assert queue.max_attempts() == 5

    monkeypatch.setattr(config, "settings", lambda: {})
    assert queue.max_attempts() == 3


@pytest.mark.asyncio
async def test_unavailable_wb_reschedules_with_growing_pause(db_path, client):
    async def handler(task):
        raise WBUnavailable("сервис не отвечает")

    queue.register("finance_report", handler)
    task_id = queue.enqueue(client, "finance_report", path=db_path)

    await queue.run_once(path=db_path)
    first = db.admin_repo(db_path).task(task_id)
    assert first["state"] == queue.PENDING
    assert first["attempts"] == 1
    assert first["next_run_at"] > "2026-01-01 00:00:00"

    _move_into_past(db_path, task_id)
    await queue.run_once(path=db_path)
    second = db.admin_repo(db_path).task(task_id)
    assert second["attempts"] == 2
    assert second["next_run_at"] > first["next_run_at"]


@pytest.mark.asyncio
async def test_rate_limit_uses_pause_from_the_error(db_path, client):
    async def handler(task):
        raise WBRateLimited("подождите", retry_after=120)

    queue.register("finance_report", handler)
    task_id = queue.enqueue(client, "finance_report", path=db_path)

    await queue.run_once(path=db_path)

    row = db.admin_repo(db_path).task(task_id)
    assert row["state"] == queue.PENDING
    assert row["attempts"] == 1
    planned = datetime.strptime(row["next_run_at"], queue.TIME_FORMAT)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    assert timedelta(seconds=100) < planned - now <= timedelta(seconds=120)


@pytest.mark.asyncio
async def test_attempts_exhausted_plain_text_to_client_record_to_owner(
    db_path, client, monkeypatch
):
    monkeypatch.setattr(config, "settings", lambda: {"queue": {"attempts": 2}})
    messages = []
    queue.set_notifier(lambda client_id, text: messages.append((client_id, text)))

    async def handler(task):
        raise WBUnavailable("сервис не отвечает")

    queue.register("finance_report", handler)
    task_id = queue.enqueue(client, "finance_report", path=db_path)
    assert messages == [(client, queue.ACCEPTED)]

    await queue.run_once(path=db_path)
    assert db.admin_repo(db_path).task(task_id)["state"] == queue.PENDING
    assert len(messages) == 1

    _move_into_past(db_path, task_id)
    await queue.run_once(path=db_path)

    row = db.admin_repo(db_path).task(task_id)
    assert row["state"] == queue.FAILED
    assert row["attempts"] == 2
    assert row["finished_at"]

    assert len(messages) == 2
    recipient, text = messages[-1]
    assert recipient == client
    assert _without_error_codes(text), text

    journal = audit.recent(limit=10, level="error", path=db_path)
    assert any("finance_report" in record["message"] for record in journal)


@pytest.mark.asyncio
async def test_auth_error_cancels_task_without_spending_attempts(db_path, client):
    about_token = []
    queue.set_auth_handler(lambda task, error: about_token.append(task.client_id))

    async def handler(task):
        raise WBAuthError("ключ отклонён")

    queue.register("finance_report", handler)
    task_id = queue.enqueue(client, "finance_report", path=db_path)

    await queue.run_once(path=db_path)

    row = db.admin_repo(db_path).task(task_id)
    assert row["state"] == queue.CANCELLED
    assert row["attempts"] == 0
    assert about_token == [client]
    assert await queue.run_once(path=db_path) is False


@pytest.mark.asyncio
async def test_forbidden_error_cancels_task_and_names_category(db_path, client):
    messages = []

    async def handler(task):
        raise WBForbiddenError("нет категории", category="Финансы")

    queue.register("finance_report", handler)
    task_id = queue.enqueue(client, "finance_report", notify=False, path=db_path)
    queue.set_notifier(lambda client_id, text: messages.append(text))

    await queue.run_once(path=db_path)

    assert db.admin_repo(db_path).task(task_id)["state"] == queue.CANCELLED
    assert len(messages) == 1
    assert "Финансы" in messages[0]
    assert _without_error_codes(messages[0]), messages[0]


@pytest.mark.asyncio
async def test_worker_picks_up_what_the_restart_left(db_path, client):
    done = []

    async def handler(task):
        done.append(task.id)

    queue.register("finance_report", handler)
    task_id = queue.enqueue(client, "finance_report", path=db_path)
    db.admin_repo(db_path).update_task(task_id, state=queue.RUNNING)

    stop = asyncio.Event()

    async def wait_for_it():
        while not done:
            await asyncio.sleep(0.01)
        stop.set()

    await asyncio.wait_for(
        asyncio.gather(
            queue.run_worker(path=db_path, poll_sec=0.01, stop=stop), wait_for_it()
        ),
        timeout=5,
    )

    assert done == [task_id]
    assert db.admin_repo(db_path).task(task_id)["state"] == queue.DONE


@pytest.mark.asyncio
async def test_any_other_error_does_not_kill_the_worker(db_path, client):
    messages = []

    async def broken(task):
        raise RuntimeError("делить на ноль нельзя")

    async def working(task):
        messages.append("вторая задача дошла")

    queue.register("broken_task", broken)
    queue.register("working_task", working)
    task_id = queue.enqueue(client, "broken_task", notify=False, path=db_path)
    queue.enqueue(client, "working_task", notify=False, path=db_path)
    queue.set_notifier(lambda client_id, text: messages.append(text))

    assert await queue.run_once(path=db_path) is True
    assert await queue.run_once(path=db_path) is True

    assert db.admin_repo(db_path).task(task_id)["state"] == queue.FAILED
    assert "вторая задача дошла" in messages
    assert any(_without_error_codes(text) for text in messages if text != "вторая задача дошла")
    journal = audit.recent(limit=10, level="error", path=db_path)
    assert any("broken_task" in record["message"] for record in journal)
