"""Фон бота: очередь и расписание поднимаются при старте и гаснут при выходе."""

import asyncio
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from bot import app as bot_app
from core import audit, config, db, queue, scheduler, wbapi

ROOT = Path(__file__).resolve().parent.parent


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


class FakeJobQueue:
    def __init__(self):
        self.jobs = []

    def run_daily(self, callback, time, name=None, **kwargs):
        self.jobs.append(name)

    def run_repeating(self, callback, interval, first=None, name=None, **kwargs):
        self.jobs.append(name)


class FakeApp:
    def __init__(self, job_queue=None):
        self.bot = FakeBot()
        self.job_queue = job_queue
        self.bot_data = {}


@pytest.fixture
def prepared(monkeypatch, tmp_path):
    """Временная база как папка данных бота плюс быстрый опрос очереди."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(bot_app, "WORKER_POLL_SEC", 0.05)
    db.migrate()
    yield tmp_path
    queue.set_notifier(None)
    scheduler.reset()
    db.close_all()


# --- D01: часовые пояса ---


def test_moscow_timezone_is_available_everywhere():
    # без tzdata это падает ZoneInfoNotFoundError на Windows
    moscow = ZoneInfo("Europe/Moscow")
    assert str(moscow) == "Europe/Moscow"


def test_tzdata_is_declared_as_dependency():
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "tzdata" in requirements


def test_reason_for_tzdata_is_written_down():
    claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    start = claude.index("<!-- autopilot:start -->")
    end = claude.index("<!-- autopilot:end -->")
    assert "tzdata" in claude[start:end], "объяснение должно лежать между маркерами"


def test_scheduler_gets_a_real_timezone():
    assert str(scheduler.tz()) == config.settings()["schedule"]["timezone"]


# --- секция очереди в конфиге ---


def test_queue_section_is_in_config():
    section = config.settings()["queue"]
    assert section["attempts"] == 3
    assert section["retry_base_sec"] == 60
    assert section["retry_factor"] == 3
    assert section["retry_max_sec"] == 3600


# --- запуск и остановка фона ---


@pytest.mark.asyncio
async def test_worker_starts_and_stops_cleanly(prepared):
    app = FakeApp(job_queue=FakeJobQueue())
    await bot_app.start_background(app)
    worker = app.bot_data["queue_worker"]
    assert not worker.done()

    await bot_app.stop_background(app)
    assert worker.done()
    assert app.bot_data.get("queue_worker") is None


@pytest.mark.asyncio
async def test_stuck_tasks_return_to_queue_before_worker_starts(prepared):
    admin = db.admin_repo()
    task_id = admin.add_task("housekeeping")
    admin.update_task(task_id, state="running")

    app = FakeApp(job_queue=FakeJobQueue())
    await bot_app.start_background(app)
    try:
        assert admin.task(task_id)["state"] == "queued"
    finally:
        await bot_app.stop_background(app)


@pytest.mark.asyncio
async def test_schedule_jobs_are_installed(prepared):
    async def nothing(*args, **kwargs):
        return None

    scheduler.register_daily("test_daily", nothing)
    app = FakeApp(job_queue=FakeJobQueue())
    await bot_app.start_background(app)
    try:
        assert "wbrentgen_daily" in app.job_queue.jobs
        assert app.bot_data["scheduled_jobs"]
    finally:
        await bot_app.stop_background(app)


@pytest.mark.asyncio
async def test_bot_lives_without_job_queue(prepared):
    app = FakeApp(job_queue=None)
    await bot_app.start_background(app)
    try:
        assert app.bot_data["scheduled_jobs"] == []
        messages = [row["message"] for row in audit.recent()]
        assert any("расписание" in message.lower() for message in messages)
        # очередь при этом работает
        assert not app.bot_data["queue_worker"].done()
    finally:
        await bot_app.stop_background(app)


@pytest.mark.asyncio
async def test_notifier_writes_to_the_client(prepared):
    client_id = db.admin_repo().ensure_client(telegram_id=555000)
    app = FakeApp(job_queue=FakeJobQueue())
    await bot_app.start_background(app)
    try:
        notify = bot_app.make_notifier(app)
        await notify(client_id, "отчёт готов")
        assert app.bot.sent == [(555000, "отчёт готов")]
    finally:
        await bot_app.stop_background(app)


@pytest.mark.asyncio
async def test_notifier_is_handed_to_the_queue(monkeypatch, prepared):
    given = []
    monkeypatch.setattr(queue, "set_notifier", lambda fn: given.append(fn))
    app = FakeApp(job_queue=FakeJobQueue())
    await bot_app.start_background(app)
    try:
        assert given and callable(given[0])
    finally:
        await bot_app.stop_background(app)


@pytest.mark.asyncio
async def test_notifier_without_client_goes_to_the_owner(monkeypatch, prepared):
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "777001")
    app = FakeApp(job_queue=FakeJobQueue())
    notify = bot_app.make_notifier(app)
    await notify(None, "служебная задача упала")
    assert app.bot.sent == [(777001, "служебная задача упала")]


@pytest.mark.asyncio
async def test_stop_is_safe_when_nothing_started(prepared):
    app = FakeApp(job_queue=FakeJobQueue())
    await bot_app.stop_background(app)  # не должно падать


@pytest.mark.asyncio
async def test_sending_failure_does_not_break_the_queue(prepared):
    app = FakeApp(job_queue=FakeJobQueue())
    client_id = db.admin_repo().ensure_client(telegram_id=555001)

    async def boom(*args, **kwargs):
        raise RuntimeError("телеграм молчит")

    app.bot.send_message = boom
    notify = bot_app.make_notifier(app)
    await notify(client_id, "отчёт готов")  # ошибка гасится, очередь живёт
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_wb_session_is_closed_on_shutdown(prepared):
    app = FakeApp(job_queue=FakeJobQueue())
    await bot_app.start_background(app)
    session = wbapi.shared_session()
    assert not session.is_closed

    await bot_app.stop_background(app)
    assert session.is_closed
    # второй раз останавливать нечего, и это не должно падать
    await bot_app.stop_background(app)


@pytest.mark.asyncio
async def test_shutdown_is_safe_without_any_session(prepared):
    await wbapi.close_session()
    app = FakeApp(job_queue=FakeJobQueue())
    await bot_app.stop_background(app)  # сессии не было, падать не из-за чего


@pytest.mark.asyncio
async def test_broken_session_does_not_block_shutdown(monkeypatch, prepared):
    async def boom():
        raise RuntimeError("сессия не закрывается")

    monkeypatch.setattr(wbapi, "close_session", boom)
    app = FakeApp(job_queue=FakeJobQueue())
    await bot_app.start_background(app)
    await bot_app.stop_background(app)
    assert app.bot_data.get("queue_worker") is None


# --- сбой в хендлере: клиент получает ответ, владелец запись ---


class FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


class FakeUpdate:
    def __init__(self, telegram_id=None, message=None):
        self.effective_message = message
        self.effective_user = type("U", (), {"id": telegram_id})() if telegram_id else None


class FakeContext:
    def __init__(self, error):
        self.error = error


@pytest.mark.asyncio
async def test_handler_failure_answers_the_client_and_lands_in_the_journal(prepared):
    from bot import texts

    client_id = db.admin_repo().ensure_client(telegram_id=910001)
    message = FakeMessage()
    await bot_app.on_error(FakeUpdate(910001, message), FakeContext(ValueError("сломалось")))

    assert message.replies == [texts.SOMETHING_WENT_WRONG]
    row = audit.recent(level="error")[0]
    assert row["client_id"] == client_id
    assert "ValueError" in row["message"]


@pytest.mark.asyncio
async def test_failure_without_message_is_still_recorded(prepared):
    await bot_app.on_error(FakeUpdate(), FakeContext(RuntimeError("фон упал")))
    row = audit.recent(level="error")[0]
    assert "RuntimeError" in row["message"]
    assert row["client_id"] is None


@pytest.mark.asyncio
async def test_silent_telegram_does_not_add_a_second_failure(prepared):
    class Mute(FakeMessage):
        async def reply_text(self, text, **kwargs):
            raise RuntimeError("телеграм молчит")

    await bot_app.on_error(FakeUpdate(910002, Mute()), FakeContext(ValueError("сломалось")))
    assert audit.recent(level="error")  # запись всё равно есть


def test_bot_does_not_serve_clients_one_by_one():
    # одно сообщение за раз означало бы, что медленный хендлер держит всех
    assert bot_app.CONCURRENT_UPDATES > 1
