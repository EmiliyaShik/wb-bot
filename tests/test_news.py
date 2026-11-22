"""Рассылка новостей: команда владельца и обещания политики.

Проверяется не «уходит ли сообщение», а то, из-за чего рассылка стоит бота
целиком. Без согласия ни одного письма. Пауза общая с напоминаниями, иначе
человек получит два письма подряд, и оба «по правилам». Ничего не уходит
до нажатия кнопки: отправленное письмо не отзывается. Заблокировавший бота
считается отказавшимся.

Шов один, обычный для проекта: путь к базе. Сети тут нет, Telegram подменён.
"""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from telegram.error import Forbidden

from bot.handlers import news
from core import access, db, leads

OWNER = 500100
GUEST = 500200
BUYER = 500300
TODAY = date(2026, 11, 23)


@pytest.fixture
def owner(monkeypatch):
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", str(OWNER))


@pytest.fixture
def people(db_path, owner):
    """Гость без оплат и клиент с оплатой, оба согласились на новости."""
    guest = db.admin_repo(db_path).ensure_client(GUEST)
    buyer = db.admin_repo(db_path).ensure_client(BUYER)
    access.grant_access(buyer, "finance", 30, "pay-1", path=db_path)
    leads.set_choice(guest, True, path=db_path)
    leads.set_choice(buyer, True, path=db_path)
    return SimpleNamespace(guest=guest, buyer=buyer)


class FakeMessage:
    def __init__(self, text: str = "", text_html: str | None = None):
        self.text = text
        self.text_html = text_html if text_html is not None else text
        self.sent: list[tuple[str, dict]] = []

    async def reply_text(self, text, **kwargs):
        self.sent.append((str(text), kwargs))
        return self


class FakeQuery:
    def __init__(self, data, message=None):
        self.data = data
        self.message = message
        self.edited: list[str] = []

    async def answer(self, *args, **kwargs):
        return None

    async def edit_message_text(self, text, **kwargs):
        self.edited.append(str(text))


class FakeUpdate:
    def __init__(self, telegram_id, message=None, query=None):
        self.effective_user = SimpleNamespace(id=telegram_id)
        self.effective_message = message
        self.callback_query = query


class FakeBot:
    def __init__(self, blocked: set[int] | None = None):
        self.sent: list[tuple[int, str]] = []
        self.blocked = blocked or set()

    async def send_message(self, chat_id, text, **kwargs):
        if int(chat_id) in self.blocked:
            raise Forbidden("бот заблокирован")
        self.sent.append((int(chat_id), str(text)))


class FakeApp:
    def __init__(self, blocked: set[int] | None = None):
        self.bot = FakeBot(blocked)


class Ctx:
    def __init__(self):
        self.user_data: dict = {}


def task(text: str):
    return SimpleNamespace(payload={"text": text})


# --- кому уходит -------------------------------------------------------------


def test_only_those_who_agreed_get_the_news(people, db_path):
    quiet = db.admin_repo(db_path).ensure_client(500400)
    leads.set_choice(quiet, False, path=db_path)
    never_asked = db.admin_repo(db_path).ensure_client(500500)

    ids = {person.client_id for person in leads.news_due(path=db_path)}

    assert ids == {people.guest, people.buyer}
    assert quiet not in ids and never_asked not in ids


def test_a_paying_client_is_a_recipient_too(people, db_path):
    """Новость про новый модуль нужна в первую очередь тому, кто уже платит."""
    ids = {person.client_id for person in leads.news_due(path=db_path)}

    assert people.buyer in ids


def test_the_pause_is_shared_with_the_reminders(people, db_path):
    """Иначе человек получит напоминание и новость в одну неделю.

    Политика обещает «не чаще одного раза в 30 дней», а не «по разу от
    каждой рассылки».
    """
    leads.note_sent(people.guest, today=TODAY, path=db_path)

    soon = TODAY + timedelta(days=leads.gap_days() - 1)
    later = TODAY + timedelta(days=leads.gap_days())

    assert people.guest not in {
        person.client_id for person in leads.news_due(today=soon, path=db_path)
    }
    assert people.guest in {
        person.client_id for person in leads.news_due(today=later, path=db_path)
    }


def test_a_news_letter_does_not_burn_the_reminder_limit(people, db_path):
    """Предел в три письма это предел напоминаний, а не переписки вообще."""
    leads.note_mailing(people.guest, today=TODAY, path=db_path)

    assert leads.sent_count(people.guest, path=db_path) == 0


# --- команда -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stranger_gets_no_answer_at_all(people, db_path):
    """Иначе перебор команд выдал бы список админки любому."""
    message = FakeMessage("/news")

    await news.news_command(FakeUpdate(GUEST, message=message), Ctx(), path=db_path)

    assert message.sent == []


@pytest.mark.asyncio
async def test_the_command_asks_for_the_text_first(people, db_path):
    message = FakeMessage("/news")
    context = Ctx()

    await news.news_command(FakeUpdate(OWNER, message=message), context, path=db_path)

    assert "Пришлите следующим сообщением текст" in message.sent[0][0]
    assert context.user_data["news_waiting"] is True


@pytest.mark.asyncio
async def test_the_preview_shows_the_letter_and_counts_the_people(people, db_path):
    context = Ctx()
    context.user_data["news_waiting"] = True
    message = FakeMessage("Вышел модуль «Воронка»")

    with pytest.raises(news.ApplicationHandlerStop):
        await news.text_step(FakeUpdate(OWNER, message=message), context, path=db_path)

    # Первым идёт само письмо, ровно так, как его увидят люди.
    assert message.sent[0][0] == "Вышел модуль «Воронка»"
    assert "Получателей: <b>2</b>" in message.sent[1][0]
    assert context.user_data[news.DRAFT] == "Вышел модуль «Воронка»"


@pytest.mark.asyncio
async def test_nothing_is_sent_before_the_button(people, db_path):
    """Отправленное письмо не отзывается, поэтому до кнопки не уходит ничего."""
    context = Ctx()
    context.user_data["news_waiting"] = True
    message = FakeMessage("Текст")

    with pytest.raises(news.ApplicationHandlerStop):
        await news.text_step(FakeUpdate(OWNER, message=message), context, path=db_path)

    # Ни одной отметки об отправке: часы паузы не тронуты.
    assert leads.news_due(path=db_path)


@pytest.mark.asyncio
async def test_cancel_sends_nothing(people, db_path):
    context = Ctx()
    context.user_data[news.DRAFT] = "Текст"
    query = FakeQuery(news.CANCEL)

    await news.cancel_callback(FakeUpdate(OWNER, query=query), context, path=db_path)

    assert "отменена" in query.edited[0]
    assert context.user_data[news.DRAFT] is None


@pytest.mark.asyncio
async def test_the_owner_formatting_survives(people, db_path):
    """Жирный и ссылки владелец набирает в Telegram, а не тегами руками."""
    context = Ctx()
    context.user_data["news_waiting"] = True
    message = FakeMessage("Вышел модуль", text_html="Вышел <b>модуль</b>")

    with pytest.raises(news.ApplicationHandlerStop):
        await news.text_step(FakeUpdate(OWNER, message=message), context, path=db_path)

    assert context.user_data[news.DRAFT] == "Вышел <b>модуль</b>"


@pytest.mark.asyncio
async def test_a_too_long_letter_is_refused_not_cut(people, db_path):
    """У отчёта остальное лежит в книге, у новости остального нет."""
    context = Ctx()
    context.user_data["news_waiting"] = True
    message = FakeMessage("а" * 5000)

    with pytest.raises(news.ApplicationHandlerStop):
        await news.text_step(FakeUpdate(OWNER, message=message), context, path=db_path)

    assert "длиннее" in message.sent[0][0]
    assert news.DRAFT not in context.user_data or not context.user_data[news.DRAFT]


# --- сама рассылка -----------------------------------------------------------


@pytest.mark.asyncio
async def test_the_broadcast_reaches_everyone_and_marks_the_pause(people, db_path, monkeypatch):
    monkeypatch.setattr(news, "PER_SECOND", 0)
    app = FakeApp()

    sent = await news.make_broadcast(app, db_path)(task("Новость"))

    assert sent == 2
    # Владельцу приходит итог тем же ботом, поэтому его из адресатов убираем.
    assert {chat for chat, _ in app.bot.sent if chat != OWNER} == {GUEST, BUYER}
    # Пауза пошла: второй раз подряд рассылка никого не найдёт.
    assert leads.news_due(path=db_path) == []


@pytest.mark.asyncio
async def test_a_blocked_bot_counts_as_a_refusal(people, db_path, monkeypatch):
    """Заблокировал бота это и есть «не пишите мне», спрашивать второй раз незачем."""
    monkeypatch.setattr(news, "PER_SECOND", 0)
    app = FakeApp(blocked={GUEST})

    await news.make_broadcast(app, db_path)(task("Новость"))

    assert leads.allowed(people.guest, path=db_path) is False
    assert leads.choice(people.guest, path=db_path) == leads.NO


@pytest.mark.asyncio
async def test_one_broken_recipient_does_not_stop_the_rest(people, db_path, monkeypatch):
    monkeypatch.setattr(news, "PER_SECOND", 0)
    app = FakeApp()

    async def fail_for_guest(chat_id, text, **kwargs):
        if int(chat_id) == GUEST:
            raise RuntimeError("Telegram молчит")
        app.bot.sent.append((int(chat_id), str(text)))

    monkeypatch.setattr(app.bot, "send_message", fail_for_guest)

    sent = await news.make_broadcast(app, db_path)(task("Новость"))

    assert sent == 1
    assert [chat for chat, _ in app.bot.sent if chat != OWNER] == [BUYER]


@pytest.mark.asyncio
async def test_an_empty_task_sends_nothing(people, db_path):
    app = FakeApp()

    assert await news.make_broadcast(app, db_path)(task("")) == 0
    assert app.bot.sent == []


@pytest.mark.asyncio
async def test_how_many_will_get_it(people, db_path):
    message = FakeMessage("/newswho")

    await news.who_command(FakeUpdate(OWNER, message=message), Ctx(), path=db_path)

    assert "<b>2</b>" in message.sent[0][0]


@pytest.mark.asyncio
async def test_the_owner_learns_how_it_went(people, db_path, monkeypatch):
    """Рассылка идёт в фоне, и без итога владелец не узнает, дошла ли она."""
    monkeypatch.setattr(news, "PER_SECOND", 0)
    app = FakeApp(blocked={GUEST})

    await news.make_broadcast(app, db_path)(task("Новость"))

    summary = [text for chat, text in app.bot.sent if chat == OWNER]
    assert summary and "Доставлено" in summary[0]
    assert "Заблокировали бота: 1" in summary[0]
