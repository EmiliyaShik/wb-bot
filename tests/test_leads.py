"""Напоминания тем, кто ничего не оформил, и команда `/stop`.

Проверяется не «работает ли рассылка», а обещания, которые дала политика
конфиденциальности: без согласия ни одного сообщения, не больше трёх, не
чаще раза в месяц, отказ действует сразу, данные молчащего удаляются по
сроку, а данные подключённого кабинета не удаляются по сроку никогда.

Шов один, обычный для проекта: путь к базе. Сети тут нет вовсе, Telegram
подменён.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from telegram.error import Forbidden

from agents import lifecycle
from bot.handlers import connect, reminders
from core import access, clients, db, leads

TODAY = date(2026, 11, 9)
OFFER = "https://example.org/offer"
PRIVACY = "https://example.org/privacy"


@pytest.fixture
def guest(db_path):
    """Человек, который просто пришёл: ни кабинета, ни оплат."""
    return db.admin_repo(db_path).ensure_client(900100)


@pytest.fixture
def buyer(db_path):
    """Клиент: доступ выдавался, пусть и давно."""
    client_id = db.admin_repo(db_path).ensure_client(900200)
    access.grant_access(client_id, "finance", 30, "pay-1", path=db_path)
    return client_id


class FakeMessage:
    def __init__(self, text: str = ""):
        self.text = text
        self.sent: list[tuple[str, dict]] = []

    async def reply_text(self, text, **kwargs):
        self.sent.append((text, kwargs))
        return self


class FakeQuery:
    def __init__(self, data, message):
        self.data = data
        self.message = message
        self.answered = False

    async def answer(self, *args, **kwargs):
        self.answered = True


class FakeUpdate:
    def __init__(self, telegram_id, message=None, query=None):
        self.effective_user = SimpleNamespace(id=telegram_id)
        self.effective_message = message
        self.callback_query = query


class FakeBot:
    def __init__(self, fail: Exception | None = None):
        self.sent: list[tuple[int, str]] = []
        self.fail = fail

    async def send_message(self, chat_id, text, **kwargs):
        if self.fail is not None:
            raise self.fail
        self.sent.append((int(chat_id), text))


class FakeApp:
    def __init__(self, fail: Exception | None = None):
        self.bot = FakeBot(fail)
        self.handlers: list = []

    def add_handler(self, handler, group=0):
        self.handlers.append((handler, group))


def task(day: date = TODAY):
    return SimpleNamespace(
        id=1, client_id=None, kind="leads", payload={"date": day.isoformat()}
    )


# --- согласие -----------------------------------------------------------------


def test_nobody_is_written_to_without_an_answer(db_path, guest):
    """Молчание это не согласие: ни одного напоминания тому, кто не нажал."""
    leads.seen(guest, today=TODAY - timedelta(days=365), path=db_path)

    assert leads.asked(guest, path=db_path) is False
    assert leads.due(today=TODAY, path=db_path) == []


def test_consent_keeps_the_day_it_was_given(db_path, guest):
    leads.set_choice(guest, True, today=TODAY, path=db_path)

    assert leads.allowed(guest, path=db_path) is True
    assert leads.answered_at(guest, path=db_path) == TODAY


def test_refusal_stops_reminders_at_once_and_cannot_be_waited_out(db_path, guest):
    """Отказ гасит и уже начатую цепочку: счётчик обнуляется, а не ждёт."""
    leads.set_choice(guest, True, today=TODAY - timedelta(days=90), path=db_path)
    leads.note_sent(guest, today=TODAY - timedelta(days=60), path=db_path)
    leads.set_choice(guest, False, today=TODAY - timedelta(days=59), path=db_path)

    assert leads.sent_count(guest, path=db_path) == 0
    assert leads.due(today=TODAY, path=db_path) == []


def test_settings_of_the_neighbour_survive_the_choice(db_path, guest):
    """Соседи по словарю настроек чужие: пишется весь словарь, а не ключ."""
    data = clients.settings_of(guest, path=db_path)
    data["drr_target"] = 15
    clients.save_settings(guest, data, path=db_path)

    leads.set_choice(guest, True, today=TODAY, path=db_path)

    assert clients.settings_of(guest, path=db_path)["drr_target"] == 15


# --- кто считается не оформившим ----------------------------------------------


def test_the_one_who_ever_had_access_is_a_client_not_a_lead(db_path, buyer):
    """Доступ кончился - человек остался клиентом, и реклама ему не пишется."""
    assert leads.unpaid(buyer, path=db_path) is False
    leads.seen(buyer, today=TODAY - timedelta(days=90), path=db_path)
    leads.set_choice(buyer, True, today=TODAY - timedelta(days=90), path=db_path)
    assert leads.due(today=TODAY, path=db_path) == []


def test_the_connected_cabinet_without_payment_is_still_a_lead(db_path, guest):
    """Подключил кабинет, ничего не купил: напоминать можно, удалять нельзя."""
    db.repo(guest, db_path).insert("wb_tokens", ciphertext=b"x", exp=None)

    assert leads.unpaid(guest, path=db_path) is True
    found = [item for item in leads.leads(path=db_path) if item.client_id == guest]
    assert found and found[0].bare is False


# --- отметка последнего обращения ---------------------------------------------


def test_the_day_of_the_visit_is_written_once(db_path, guest):
    leads.seen(guest, today=TODAY, path=db_path)
    before = clients.settings_of(guest, path=db_path)[leads.SETTINGS_KEY]
    leads.seen(guest, today=TODAY, path=db_path)

    assert clients.settings_of(guest, path=db_path)[leads.SETTINGS_KEY] == before
    assert leads.last_seen(guest, path=db_path) == TODAY


# --- очередь напоминаний ------------------------------------------------------


def test_the_pause_between_reminders_is_kept(db_path, guest):
    leads.seen(guest, today=TODAY, path=db_path)
    leads.set_choice(guest, True, today=TODAY, path=db_path)

    assert leads.due(today=TODAY, path=db_path) == []
    due = leads.due(today=TODAY + timedelta(days=leads.gap_days()), path=db_path)
    assert [item.client_id for item in due] == [guest]


def test_reminders_stop_at_the_promised_count(db_path, guest):
    leads.seen(guest, today=TODAY, path=db_path)
    leads.set_choice(guest, True, today=TODAY, path=db_path)
    day = TODAY
    for _ in range(leads.max_reminders()):
        day += timedelta(days=leads.gap_days())
        assert [item.client_id for item in leads.due(today=day, path=db_path)] == [guest]
        leads.note_sent(guest, today=day, path=db_path)

    day += timedelta(days=leads.gap_days())
    assert leads.due(today=day, path=db_path) == []


@pytest.mark.asyncio
async def test_the_morning_job_writes_only_to_those_who_allowed_it(db_path, guest, buyer):
    long_ago = TODAY - timedelta(days=60)
    quiet = db.admin_repo(db_path).ensure_client(900300)
    refused = db.admin_repo(db_path).ensure_client(900400)
    for client_id in (guest, buyer, quiet, refused):
        leads.seen(client_id, today=long_ago, path=db_path)
    leads.set_choice(refused, False, today=long_ago, path=db_path)
    leads.set_choice(guest, True, today=long_ago, path=db_path)
    leads.set_choice(buyer, True, today=long_ago, path=db_path)

    app = FakeApp()
    sent = await reminders.make_remind_job(app, db_path)(task())

    assert sent == 1
    assert [chat for chat, _ in app.bot.sent] == [900100]
    assert leads.sent_count(guest, path=db_path) == 1
    assert leads.sent_count(quiet, path=db_path) == 0
    assert leads.sent_count(refused, path=db_path) == 0


@pytest.mark.asyncio
async def test_the_reminder_says_which_one_it_is_and_how_to_stop(db_path, guest):
    leads.seen(guest, today=TODAY - timedelta(days=60), path=db_path)
    leads.set_choice(guest, True, today=TODAY - timedelta(days=60), path=db_path)

    app = FakeApp()
    await reminders.make_remind_job(app, db_path)(task())

    text = app.bot.sent[0][1]
    assert "1 из 3" in text
    assert "/stop" in text


@pytest.mark.asyncio
async def test_a_blocked_bot_is_the_same_as_a_refusal(db_path, guest):
    """Заблокировал бота - значит, не хочет: стучать месяцами незачем."""
    leads.seen(guest, today=TODAY - timedelta(days=60), path=db_path)
    leads.set_choice(guest, True, today=TODAY - timedelta(days=60), path=db_path)

    app = FakeApp(fail=Forbidden("bot was blocked by the user"))
    sent = await reminders.make_remind_job(app, db_path)(task())

    assert sent == 0
    assert leads.choice(guest, path=db_path) == leads.NO


@pytest.mark.asyncio
async def test_a_failed_send_does_not_turn_into_a_daily_knock(db_path, guest):
    """Пауза держится отметкой, и отметка ставится до отправки."""
    leads.seen(guest, today=TODAY - timedelta(days=60), path=db_path)
    leads.set_choice(guest, True, today=TODAY - timedelta(days=60), path=db_path)

    app = FakeApp(fail=RuntimeError("Telegram не ответил"))
    assert await reminders.make_remind_job(app, db_path)(task()) == 0

    assert leads.due(today=TODAY + timedelta(days=1), path=db_path) == []


# --- удаление по сроку --------------------------------------------------------


def test_data_of_the_silent_one_is_deleted_by_the_promised_term(db_path, guest):
    leads.seen(guest, today=TODAY - timedelta(days=leads.quiet_days()), path=db_path)

    reminders.make_cleanup_job(db_path)(task())

    assert db.admin_repo(db_path).client(guest) is None


def test_the_silent_one_is_kept_until_the_term_is_over(db_path, guest):
    leads.seen(guest, today=TODAY - timedelta(days=leads.quiet_days() - 1), path=db_path)

    reminders.make_cleanup_job(db_path)(task())

    assert db.admin_repo(db_path).client(guest) is not None


def test_the_refusal_outlives_the_rest_of_the_data(db_path, guest):
    """Список «не писать» это сами идентификаторы: удалить их значит написать."""
    long_ago = TODAY - timedelta(days=leads.quiet_days() + 10)
    leads.seen(guest, today=long_ago, path=db_path)
    leads.set_choice(guest, False, today=long_ago, path=db_path)

    reminders.make_cleanup_job(db_path)(task())
    assert db.admin_repo(db_path).client(guest) is not None

    later = TODAY + timedelta(days=leads.refusal_days())
    reminders.make_cleanup_job(db_path)(task(later))
    assert db.admin_repo(db_path).client(guest) is None


def test_the_connected_cabinet_is_never_deleted_by_silence(db_path, guest):
    """Политика обещает хранить данные, пока кабинет подключён. Обещание держим."""
    db.repo(guest, db_path).insert("wb_tokens", ciphertext=b"x", exp=None)
    leads.seen(guest, today=TODAY - timedelta(days=leads.quiet_days() * 3), path=db_path)

    reminders.make_cleanup_job(db_path)(task())

    assert db.admin_repo(db_path).client(guest) is not None


def test_the_client_is_not_touched_by_this_cleanup(db_path, buyer):
    """У клиента свой срок хранения ([access] retention_days) и своё удаление."""
    leads.seen(buyer, today=TODAY - timedelta(days=leads.quiet_days() * 2), path=db_path)

    reminders.make_cleanup_job(db_path)(task())

    assert db.admin_repo(db_path).client(buyer) is not None


def test_the_deletion_leaves_a_faceless_line_in_the_journal(db_path, guest):
    leads.seen(guest, today=TODAY - timedelta(days=leads.quiet_days()), path=db_path)

    reminders.make_cleanup_job(db_path)(task())

    rows = db.admin_repo(db_path).events(limit=20, kind=lifecycle.DELETED)
    assert rows
    # Обезличенно: чьи это были данные, в журнале не остаётся.
    assert rows[0]["client_id"] is None


# --- команда /stop ------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_records_the_refusal_and_says_the_bot_will_be_quiet(db_path, guest):
    message = FakeMessage("/stop")
    await reminders.stop_command(FakeUpdate(900100, message), None, path=db_path)

    assert leads.choice(guest, path=db_path) == leads.NO
    assert "не напоминать" in message.sent[-1][0].lower()


@pytest.mark.asyncio
async def test_stop_tells_the_client_what_it_does_not_switch_off(db_path, buyer):
    """У клиента отчёты идут по договору, и выключаются они в другом месте."""
    message = FakeMessage("/stop")
    await reminders.stop_command(FakeUpdate(900200, message), None, path=db_path)

    text = message.sent[-1][0]
    assert "/settings" in text and "/disconnect" in text


# --- кнопки -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_button_decides_not_its_caption(db_path, guest):
    message = FakeMessage()
    query = FakeQuery(reminders.PERMIT, message)
    await reminders.choice_callback(
        FakeUpdate(900100, None, query), None, permit=True, path=db_path
    )

    assert query.answered is True
    assert leads.choice(guest, path=db_path) == leads.YES


@pytest.mark.asyncio
async def test_the_refuse_button_writes_the_refusal(db_path, guest):
    message = FakeMessage()
    query = FakeQuery(reminders.REFUSE, message)
    await reminders.choice_callback(
        FakeUpdate(900100, None, query), None, permit=False, path=db_path
    )

    assert leads.choice(guest, path=db_path) == leads.NO


def test_the_question_is_asked_once_and_not_to_a_client(db_path, guest, buyer):
    assert reminders.worth_asking(guest, path=db_path) is True
    leads.set_choice(guest, False, today=TODAY, path=db_path)
    assert reminders.worth_asking(guest, path=db_path) is False
    assert reminders.worth_asking(buyer, path=db_path) is False


# --- согласие при подключении -------------------------------------------------


def test_the_consent_block_names_both_documents(monkeypatch):
    monkeypatch.setenv("OFFER_URL", OFFER)
    monkeypatch.setenv("PRIVACY_URL", PRIVACY)

    block = connect.consent_block()

    assert OFFER in block
    assert PRIVACY in block
    assert "только на чтение" in block
    assert "/disconnect" in block


def test_the_consent_block_is_honest_while_a_document_is_being_prepared(monkeypatch):
    monkeypatch.setenv("OFFER_URL", OFFER)
    monkeypatch.setenv("PRIVACY_URL", "")

    block = connect.consent_block()

    assert OFFER in block
    assert "готовится" in block


def test_the_seen_mark_does_not_break_the_conversation(db_path, guest, monkeypatch):
    """Сбой отметки не важнее ответа на команду: она гасится и пишется в лог."""

    def boom(*args, **kwargs):
        raise RuntimeError("база недоступна")

    monkeypatch.setattr(leads, "seen", boom)
    asyncio.run(
        reminders.seen_guard(FakeUpdate(900100, FakeMessage()), None, path=db_path)
    )


def test_the_seen_mark_marks_the_day(db_path, guest):
    asyncio.run(
        reminders.seen_guard(FakeUpdate(900100, FakeMessage()), None, path=db_path)
    )

    assert leads.last_seen(guest, path=db_path) is not None


def test_a_stranger_does_not_get_a_row_just_for_writing(db_path):
    """Отметка строк не создаёт: собирать данные ради их удаления незачем."""
    asyncio.run(
        reminders.seen_guard(FakeUpdate(777777, FakeMessage()), None, path=db_path)
    )

    assert db.admin_repo(db_path).client_by_telegram(777777) is None
