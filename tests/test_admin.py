"""Таск 08: владелец видит бизнес, готовит акты, выдаёт доступ и чинит задачи.

Шов один - путь к базе. Всё остальное проверяется через публичные функции.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from types import SimpleNamespace

from telegram.ext import ApplicationHandlerStop

from bot.handlers import admin
from core import access, audit, billing, db, metering, queue, ratelimit, scheduler, xlsx


# --- H5: ограничение частоты команд ---


def test_rate_limit_lets_through_up_to_the_limit_and_then_asks_to_wait():
    """Лимит 3 в минуту: четвёртая команда подряд не проходит."""
    ratelimit.reset()
    start = datetime(2026, 9, 16, 10, 0, 0, tzinfo=timezone.utc)

    passed = [ratelimit.check(777, limit=3, now=start).allowed for _ in range(3)]
    fourth = ratelimit.check(777, limit=3, now=start)

    assert passed == [True, True, True]
    assert fourth.allowed is False
    assert fourth.retry_after > 0


def test_rate_limit_forgets_the_minute_that_has_passed():
    ratelimit.reset()
    start = datetime(2026, 9, 16, 10, 0, 0, tzinfo=timezone.utc)
    for _ in range(3):
        ratelimit.check(777, limit=3, now=start)

    later = ratelimit.check(777, limit=3, now=start + timedelta(seconds=61))

    assert later.allowed is True


def test_rate_limit_counts_each_person_separately():
    ratelimit.reset()
    start = datetime(2026, 9, 16, 10, 0, 0, tzinfo=timezone.utc)
    for _ in range(3):
        ratelimit.check(777, limit=3, now=start)

    other = ratelimit.check(888, limit=3, now=start)

    assert other.allowed is True


def test_rate_limit_takes_its_number_from_the_config():
    """В config.toml секция [limits], ключ client_messages_per_minute = 20."""
    assert ratelimit.per_minute() == 20


# --- H1: владелец видит бизнес одной командой ---

TODAY = date(2026, 9, 16)


def put_invoice(client_id, path, number, *, amount_kop, status, issued, paid=None, inn="", org=""):
    db.repo(client_id, path).insert(
        "invoices",
        number=number,
        module="finance",
        period_months=1,
        amount_kop=amount_kop,
        status=status,
        inn=inn,
        org_name=org,
        issued_at=issued,
        paid_at=paid,
    )


@pytest.fixture
def business(db_path):
    """Двое клиентов: у первого оплаченный счёт и доступ, у второго только счёт."""
    admin = db.admin_repo(db_path)
    first = admin.ensure_client(4040)
    second = admin.ensure_client(5050)

    access.grant_access(
        first, "finance", 31, "WBR-2026-0001", method="invoice", actor="owner",
        now=datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc), path=db_path,
    )
    # Оплачен в этом месяце - попадает в выручку.
    put_invoice(first, db_path, "WBR-2026-0001", amount_kop=300000, status=billing.PAID,
                issued="2026-09-01 09:00:00", paid="2026-09-01 10:00:00",
                inn="7707083893", org="ООО Ромашка")
    # Оплачен в августе - в сентябрьскую выручку не попадает.
    put_invoice(first, db_path, "WBR-2026-0000", amount_kop=999900, status=billing.PAID,
                issued="2026-08-01 09:00:00", paid="2026-08-02 10:00:00")
    # Выставлен и не оплачен.
    put_invoice(second, db_path, "WBR-2026-0002", amount_kop=150000, status=billing.ISSUED,
                issued="2026-09-10 09:00:00")

    db.repo(first, db_path).insert(
        "api_calls", host="statistics-api", method="/api/v5/supplier/reportDetailByPeriod",
        status=200, at="2026-09-10 08:00:00",
    )
    db.repo(first, db_path).insert(
        "api_calls", host="statistics-api", method="/api/v5/supplier/reportDetailByPeriod",
        status=429, at="2026-09-10 08:01:00",
    )
    db.repo(first, db_path).insert(
        "api_calls", host="advert-api", method="/adv/v1/promotion/count",
        status=200, at="2026-08-10 08:00:00",
    )
    db.repo(first, db_path).insert(
        "ai_calls", provider="test", model="test", cost_kop=1000, at="2026-09-11 08:00:00",
    )
    return first, second


def test_stats_counts_revenue_only_from_invoices_paid_in_that_month(db_path, business):
    result = metering.stats("month", today=TODAY, path=db_path)

    assert result.revenue == Decimal("3000")


def test_stats_shows_active_clients_by_module(db_path, business):
    first, _ = business

    result = metering.stats("month", today=TODAY, path=db_path)

    assert result.modules["finance"] == 1
    assert result.active_clients == 1


def test_stats_shows_issued_but_unpaid_invoices(db_path, business):
    result = metering.stats("month", today=TODAY, path=db_path)

    assert result.unpaid_count == 1
    assert result.unpaid_amount == Decimal("1500")


def test_stats_counts_wb_calls_by_method_within_the_month(db_path, business):
    result = metering.stats("month", today=TODAY, path=db_path)

    by_method = {use.method: use.count for use in result.calls}
    assert by_method == {"/api/v5/supplier/reportDetailByPeriod": 2}
    assert result.calls[0].errors == 1


def test_stats_gives_margin_per_client(db_path, business):
    first, second = business

    result = metering.stats("month", today=TODAY, path=db_path)
    by_client = {row.client_id: row for row in result.clients}

    assert by_client[first].revenue == Decimal("3000")
    assert by_client[first].cost == Decimal("10")  # расход нейросети, 1000 копеек
    assert by_client[first].margin == Decimal("2990")
    assert second not in by_client or by_client[second].revenue == Decimal("0")


# --- D8: реестр оплаченных счетов для актов ---


def test_acts_take_only_invoices_paid_in_the_asked_month(db_path, business):
    start, end = billing.previous_month_bounds(TODAY)  # август

    paid = billing.paid_invoices(start, end, path=db_path)

    assert [item.number for item in paid] == ["WBR-2026-0000"]


def test_acts_book_carries_all_seven_columns_of_the_registry(db_path, business):
    data = billing.acts_xlsx("2026-09", path=db_path)
    sheet = xlsx.read_sheet(data)

    assert tuple(sheet.headers) == billing.ACTS_HEADERS
    row = sheet.rows[0]
    assert row.get("Номер счёта") == "WBR-2026-0001"
    assert row.get("ИНН") == "7707083893"
    assert row.get("Название клиента") == "ООО Ромашка"
    assert row.get("Сумма, ₽") == 3000


# --- хендлер владельца ---

OWNER = 9001
STRANGER = 4040


@pytest.fixture
def owner(monkeypatch):
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", str(OWNER))
    ratelimit.reset()
    return OWNER


class FakeMessage:
    def __init__(self):
        self.sent: list[str] = []
        self.documents: list[tuple[str, bytes]] = []
        self.keyboards: list = []

    async def reply_text(self, text, **kwargs):
        self.sent.append(text)
        self.keyboards.append(kwargs.get("reply_markup"))
        return self

    async def reply_document(self, document, filename=None, **kwargs):
        self.documents.append((filename, bytes(document)))
        return self

    @property
    def last(self) -> str:
        return self.sent[-1] if self.sent else ""


class FakeQuery:
    def __init__(self, data, message):
        self.data = data
        self.message = message
        self.answers: list[str] = []
        self.edits: list[str] = []

    async def answer(self, text="", **kwargs):
        self.answers.append(text)

    async def edit_message_text(self, text, **kwargs):
        self.edits.append(text)


class FakeUpdate:
    def __init__(self, telegram_id, message=None, query=None):
        self.effective_user = SimpleNamespace(id=telegram_id)
        self.effective_message = message
        self.message = message
        self.callback_query = query


class FakeBot:
    def __init__(self):
        self.documents: list[tuple[int, str]] = []

    async def send_document(self, chat_id, document, filename=None, **kwargs):
        self.documents.append((chat_id, filename))


class FakeApp:
    def __init__(self):
        self.bot = FakeBot()
        self.added: list = []

    def add_handler(self, handler_obj, group=0):
        self.added.append((handler_obj, group))


def context(*args):
    return SimpleNamespace(args=list(args), bot=FakeBot())


@pytest.mark.asyncio
async def test_a_stranger_gets_exactly_what_a_nonexistent_command_gives(db_path, business, owner):
    """H2: чужой не должен узнать, что команда вообще есть. Значит - ни звука."""
    message = FakeMessage()
    update = FakeUpdate(STRANGER, message)
    was = access.access_of(1, "finance", path=db_path)
    log_rows = db.repo(1, db_path).count("access_log")

    await admin.stats_command(update, context(), path=db_path, today=TODAY)
    await admin.acts_command(update, context(), path=db_path, today=TODAY)
    await admin.tasks_command(update, context(), path=db_path)
    await admin.grant_command(update, context("1", "finance", "1", "счёт", "П-1"), path=db_path)
    await admin.revoke_command(update, context("1", "finance", "возврат"), path=db_path)

    # Несуществующая команда в этом боте не отвечает ничем: нет хендлера,
    # нет ответа. Админ-команда у чужого выглядит ровно так же.
    assert message.sent == []
    assert message.documents == []
    # И молчать мало: молчаливая выдача доступа выглядела бы точно так же.
    assert access.access_of(1, "finance", path=db_path).until == was.until
    assert db.repo(1, db_path).count("access_log") == log_rows


@pytest.mark.asyncio
async def test_the_owner_does_get_an_answer_to_the_same_command(db_path, business, owner):
    message = FakeMessage()

    await admin.stats_command(FakeUpdate(OWNER, message), context(), path=db_path, today=TODAY)

    assert message.sent != []


@pytest.mark.asyncio
async def test_stats_shows_money_modules_and_calls_without_naming_people(db_path, business, owner):
    first, _ = business
    message = FakeMessage()

    await admin.stats_command(FakeUpdate(OWNER, message), context(), path=db_path, today=TODAY)
    text = message.last

    assert "3 000" in text  # выручка месяца, разряды неразрывным пробелом
    assert "1 500" in text  # неоплаченный счёт
    assert "finance" in text.lower()
    assert "reportDetailByPeriod" in text  # расходы: вызовы WB по методам
    assert f"{first}" in text  # клиент назван внутренним id
    assert "4040" not in text  # и никогда Telegram-аккаунтом
    assert "7707083893" not in text  # и никогда реквизитами


@pytest.mark.asyncio
async def test_acts_command_sends_the_registry_as_a_file(db_path, business, owner):
    message = FakeMessage()

    await admin.acts_command(FakeUpdate(OWNER, message), context(), path=db_path, today=TODAY)

    filename, data = message.documents[0]
    assert filename.endswith(".xlsx")
    assert xlsx.read_sheet(data).rows[0].get("Номер счёта") == "WBR-2026-0001"


@pytest.mark.asyncio
async def test_grant_goes_through_the_only_door_and_lands_in_the_log(db_path, business, owner):
    _, second = business
    message = FakeMessage()

    await admin.grant_command(
        FakeUpdate(OWNER, message),
        context(str(second), "finance", "1", "карта", "П-777"),
        path=db_path,
    )

    assert access.has_access(second, "finance", path=db_path)
    row = db.repo(second, db_path).one("access_log", action="grant")
    assert row["payment_ref"] == "П-777"
    assert row["method"] == "card"
    assert row["actor"] == "owner"
    assert "finance" in message.last


@pytest.mark.asyncio
async def test_grant_with_the_same_payment_number_does_not_extend_anything(db_path, business, owner):
    first, _ = business
    message = FakeMessage()

    await admin.grant_command(
        FakeUpdate(OWNER, message),
        context(str(first), "finance", "1", "счёт", "WBR-2026-0001"),
        path=db_path,
    )

    assert "дубл" in message.last.lower()
    assert db.repo(first, db_path).one("access_log", action="duplicate") is not None


@pytest.mark.asyncio
async def test_revoke_turns_access_off_and_writes_the_reason(db_path, business, owner):
    """Гасит core.access.revoke_access: причина и сгоревшие сутки в журнале."""
    first, _ = business
    message = FakeMessage()
    now = datetime(2026, 9, 16, tzinfo=timezone.utc)

    await admin.revoke_command(
        FakeUpdate(OWNER, message),
        context(str(first), "finance", "возврат", "денег"),
        path=db_path,
        now=now,
    )

    assert access.has_access(first, "finance", now=now, path=db_path) is False
    row = db.repo(first, db_path).one("access_log", action="revoke")
    assert row["actor"] == "owner"
    assert row["method"] == "возврат денег"
    # Доступ был выдан 1 сентября на 31 день, отменён 16-го: сгорело 16 суток.
    assert row["days"] == 16
    events = [e["message"] for e in db.repo(first, db_path).rows("events", kind="access.revoke")]
    assert any("возврат денег" in text for text in events)


@pytest.mark.asyncio
async def test_revoke_all_walks_the_modules_one_by_one(db_path, business, owner):
    """«Все» это обход status(), а не особый случай внутри ядра."""
    first, _ = business
    now = datetime(2026, 9, 16, tzinfo=timezone.utc)
    access.grant_access(first, "rnp", 31, "П-2", method="card", actor="owner", now=now, path=db_path)
    message = FakeMessage()

    await admin.revoke_command(
        FakeUpdate(OWNER, message), context(str(first), "все", "возврат"), path=db_path, now=now
    )

    assert access.has_access(first, "finance", now=now, path=db_path) is False
    assert access.has_access(first, "rnp", now=now, path=db_path) is False
    revoked = {r["module"] for r in db.repo(first, db_path).rows("access_log", action="revoke")}
    assert revoked == {"finance", "rnp"}


@pytest.fixture
def broken(db_path, business):
    """Одна упавшая задача и одна в работе плюс запись журнала об ошибке."""
    first, _ = business
    repo = db.repo(first, db_path)
    failed = repo.insert(
        "tasks", kind="finance_report", payload="{}", state=queue.FAILED,
        attempts=3, last_error="Wildberries ответил 500",
    )
    repo.insert("tasks", kind="rnp_collect_client", payload="{}", state=queue.RUNNING)
    audit.log("task.failed", first, "задача finance_report упала", level="error", path=db_path)
    return first, failed


@pytest.mark.asyncio
async def test_tasks_shows_what_fell_with_the_error_text_and_a_button(db_path, broken, owner):
    first, failed = broken
    message = FakeMessage()

    await admin.tasks_command(FakeUpdate(OWNER, message), context(), path=db_path)
    text = message.last

    assert "finance_report" in text
    assert "Wildberries ответил 500" in text
    assert f"{first}" in text
    assert "3" in text  # число попыток
    assert "rnp_collect_client" in text  # задачи в работе тоже видны
    buttons = message.keyboards[-1].inline_keyboard
    assert any(f"{failed}" in button.callback_data for row in buttons for button in row)


@pytest.mark.asyncio
async def test_restart_button_returns_the_task_to_the_queue(db_path, broken, owner):
    first, failed = broken
    query = FakeQuery(f"{admin.PREFIX}retry:{failed}", FakeMessage())

    await admin.retry_callback(FakeUpdate(OWNER, query=query), context(), path=db_path)

    row = db.repo(first, db_path).one("tasks", id=failed)
    assert row["state"] == queue.PENDING
    assert row["attempts"] == 0
    kinds = [e["kind"] for e in db.repo(first, db_path).rows("events")]
    assert "admin.task.retry" in kinds


@pytest.mark.asyncio
async def test_restart_button_does_nothing_for_a_stranger(db_path, broken, owner):
    first, failed = broken
    query = FakeQuery(f"{admin.PREFIX}retry:{failed}", FakeMessage())

    await admin.retry_callback(FakeUpdate(STRANGER, query=query), context(), path=db_path)

    assert db.repo(first, db_path).one("tasks", id=failed)["state"] == queue.FAILED
    assert query.answers == []
    assert query.edits == []


@pytest.mark.asyncio
async def test_the_error_text_reaches_the_owner_cleaned_of_tokens(db_path, business, owner):
    """last_error пишет очередь, и туда может попасть токен из ответа WB."""
    first, _ = business
    token = "eyJhbGciOiJFUzI1NiJ9.eyJzaWQiOiJhYmMifQ.ZmFrZXNpZ25hdHVyZXZhbHVl"
    db.repo(first, db_path).insert(
        "tasks", kind="finance_report", payload="{}", state=queue.FAILED,
        attempts=2, last_error=f"401 от WB, заголовок был {token}",
    )
    message = FakeMessage()

    await admin.tasks_command(FakeUpdate(OWNER, message), context(), path=db_path)

    assert token not in message.last
    assert audit.HIDDEN in message.last
    assert "401 от WB" in message.last  # сама ошибка при этом видна


@pytest.mark.asyncio
async def test_the_owner_is_not_rate_limited(db_path, owner):
    """Владелец гоняет /tasks и /stats как раз тогда, когда что-то упало."""
    message = FakeMessage()
    update = FakeUpdate(OWNER, message)

    for _ in range(ratelimit.per_minute() + 5):
        await admin.rate_guard(update, context())

    assert message.sent == []


@pytest.mark.asyncio
async def test_too_many_commands_get_a_polite_wait_instead_of_silence(db_path, owner):
    message = FakeMessage()
    update = FakeUpdate(STRANGER, message)
    for _ in range(ratelimit.per_minute()):
        await admin.rate_guard(update, context())

    with pytest.raises(ApplicationHandlerStop):
        await admin.rate_guard(update, context())

    assert "подожд" in message.last.lower()


@pytest.mark.asyncio
async def test_registry_of_acts_goes_out_by_itself_on_the_first_day(db_path, business, owner):
    scheduler.reset()
    app = FakeApp()
    admin.register(app, path=db_path)
    job = scheduler.daily_names()

    assert admin.ACTS_JOB in job
    await queue.handlers()[admin.ACTS_JOB](
        queue.Task(id=1, client_id=None, kind=admin.ACTS_JOB, payload={"date": "2026-09-16"}, attempts=0)
    )
    assert app.bot.documents == []  # не первое число - ничего не уходит

    await queue.handlers()[admin.ACTS_JOB](
        queue.Task(id=2, client_id=None, kind=admin.ACTS_JOB, payload={"date": "2026-09-01"}, attempts=0)
    )
    chat_id, filename = app.bot.documents[0]
    assert chat_id == OWNER
    assert filename.endswith(".xlsx")
