"""Расписание: ежедневные задачи по Москве, недельные по новому финотчёту.

Шов тот же, что у очереди: путь к базе. Сам PTB здесь не поднимается,
JobQueue подменяется записной книжкой вызовов.
"""

import json
from datetime import time, timedelta

import pytest

from core import config, db, queue, scheduler


@pytest.fixture(autouse=True)
def clean_registry():
    queue.reset()
    scheduler.reset()
    yield
    queue.reset()
    scheduler.reset()


@pytest.fixture
def client(db_path):
    return db.admin_repo(db_path).ensure_client(100500)


async def nothing(task):
    return None


def tasks_of(db_path, kind):
    return db.admin_repo(db_path).tasks_by_kind(kind)


def test_time_and_timezone_come_from_config():
    assert scheduler.daily_time() == time(9, 0)
    assert scheduler.timezone_name() == "Europe/Moscow"
    assert scheduler.tz() is not None


def test_time_can_be_changed_in_config(monkeypatch):
    monkeypatch.setattr(
        config, "settings", lambda: {"schedule": {"daily_at": "07:30", "timezone": "UTC"}}
    )
    assert scheduler.daily_time() == time(7, 30)
    assert scheduler.timezone_name() == "UTC"


def test_report_check_interval_comes_from_config(monkeypatch):
    assert scheduler.weekly_check_hours() == config.settings()["schedule"]["weekly_check_hours"]

    monkeypatch.setattr(config, "settings", lambda: {"schedule": {"weekly_check_hours": 2}})
    assert scheduler.weekly_check_hours() == 2

    monkeypatch.setattr(config, "settings", lambda: {"schedule": {}})
    assert scheduler.weekly_check_hours() == scheduler.DEFAULT_WEEKLY_CHECK_HOURS


def test_registration_makes_the_function_a_queue_handler():
    scheduler.register_daily("rnp_daily", nothing)
    scheduler.register_weekly("finance_weekly", nothing)

    assert queue.handlers()["rnp_daily"] is nothing
    assert queue.handlers()["finance_weekly"] is nothing


def test_daily_task_is_queued_once_a_day(db_path):
    scheduler.register_daily("rnp_daily", nothing)

    queued = scheduler.run_daily(path=db_path)
    assert len(queued) == 1
    assert len(tasks_of(db_path, "rnp_daily")) == 1

    assert scheduler.run_daily(path=db_path) == []
    assert len(tasks_of(db_path, "rnp_daily")) == 1


def test_daily_dedup_survives_a_long_task_history(db_path, client):
    """Отбор идёт запросом по виду задачи, а не срезом последних строк."""
    scheduler.register_daily("rnp_daily", nothing)

    assert len(scheduler.run_daily(path=db_path)) == 1
    for number in range(1200):
        queue.enqueue(client, "noise", {"n": number}, notify=False, path=db_path)

    assert scheduler.run_daily(path=db_path) == []
    assert len(tasks_of(db_path, "rnp_daily")) == 1


def test_schedule_does_not_promise_anything_to_the_client(db_path, client):
    messages = []
    queue.set_notifier(lambda client_id, text: messages.append(text))
    scheduler.register_daily("rnp_daily", nothing)
    scheduler.register_weekly("finance_weekly", nothing)
    scheduler.set_report_probe(lambda client_id: 500)

    scheduler.run_daily(path=db_path)
    scheduler.check_weekly(path=db_path)

    assert messages == []


def test_weekly_task_waits_for_a_new_report(db_path, client):
    scheduler.register_weekly("finance_weekly", nothing)
    reports = {"id": None}
    scheduler.set_report_probe(lambda client_id: reports["id"])

    assert scheduler.check_weekly(path=db_path) == []
    assert tasks_of(db_path, "finance_weekly") == []

    reports["id"] = 500
    queued = scheduler.check_weekly(path=db_path)
    assert len(queued) == 1
    rows = tasks_of(db_path, "finance_weekly")
    assert len(rows) == 1
    assert rows[0]["client_id"] == client
    assert json.loads(rows[0]["payload"])["report_id"] == 500

    assert scheduler.check_weekly(path=db_path) == []
    assert len(tasks_of(db_path, "finance_weekly")) == 1

    reports["id"] = 501
    assert len(scheduler.check_weekly(path=db_path)) == 1
    assert len(tasks_of(db_path, "finance_weekly")) == 2


def test_weekly_dedup_survives_a_long_task_history(db_path, client):
    """Отбор идёт запросом по виду задачи, а не срезом последних строк."""
    scheduler.register_weekly("finance_weekly", nothing)
    scheduler.set_report_probe(lambda client_id: 500)

    assert len(scheduler.check_weekly(path=db_path)) == 1
    for number in range(300):
        queue.enqueue(client, "noise", {"n": number}, notify=False, path=db_path)

    assert scheduler.check_weekly(path=db_path) == []
    assert len(tasks_of(db_path, "finance_weekly")) == 1


def test_install_puts_jobs_into_job_queue(db_path, monkeypatch):
    scheduler.register_daily("rnp_daily", nothing)
    scheduler.register_weekly("finance_weekly", nothing)

    class Notebook:
        def __init__(self):
            self.daily = []
            self.repeating = []

        def run_daily(self, callback, time, name=None, **kwargs):
            self.daily.append((name, time))

        def run_repeating(self, callback, interval, first=None, name=None, **kwargs):
            self.repeating.append((name, interval))

    monkeypatch.setattr(
        config,
        "settings",
        lambda: {"schedule": {"daily_at": "09:00", "timezone": "UTC", "weekly_check_hours": 2}},
    )
    notebook = Notebook()
    names = scheduler.install(notebook, path=db_path)

    assert notebook.repeating[0][1] == timedelta(hours=2)

    assert notebook.daily, "ежедневная работа не поставлена"
    assert {moment.hour for _, moment in notebook.daily} == {9}
    assert all(moment.tzinfo is not None for _, moment in notebook.daily)
    assert notebook.repeating, "проверка нового финотчёта не поставлена"
    assert names
