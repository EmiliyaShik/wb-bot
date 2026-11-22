"""Три согласия до работы с ботом: оферта, персональные данные, новости.

Проверяется не «рисуется ли экран», а обещания, которые нельзя нарушить.
Два обязательных согласия закрывают бота, третье необязательное и ничему не
мешает: реклама по сетям связи разрешена только с отдельно выраженного
согласия, и одна кнопка «со всем согласен» обесценила бы его целиком.

Отдельно закреплено, что остаётся открытым без согласий: карточка товара по
артикулу (решение владельца: это бесплатный крючок, им пробуют бота до
всяких отношений) и `/stop` (сказать «не пишите мне» человек должен мочь
всегда).

Шов один, обычный для проекта: путь к базе. Сети тут нет, Telegram подменён.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from telegram.ext import ApplicationHandlerStop

from bot.handlers import consent as handler
from core import clients, consent, db, leads

TODAY = date(2026, 11, 23)
OFFER = "https://example.org/offer"
PRIVACY = "https://example.org/privacy"


@pytest.fixture
def documents(monkeypatch):
    monkeypatch.setenv("OFFER_URL", OFFER)
    monkeypatch.setenv("PRIVACY_URL", PRIVACY)


@pytest.fixture
def newcomer(db_path, documents):
    """Человек, который только что написал боту."""
    return db.admin_repo(db_path).ensure_client(700100)


class FakeMessage:
    def __init__(self, text: str = "", document=None):
        self.text = text
        self.document = document
        self.sent: list[tuple[str, dict]] = []

    async def reply_text(self, text, **kwargs):
        self.sent.append((text, kwargs))
        return self


class FakeQuery:
    def __init__(self, data, message):
        self.data = data
        self.message = message
        self.edited: list[str] = []

    async def answer(self, *args, **kwargs):
        return None

    async def edit_message_text(self, text, **kwargs):
        self.edited.append(text)


class FakeUpdate:
    def __init__(self, telegram_id, message=None, query=None):
        self.effective_user = SimpleNamespace(id=telegram_id)
        self.effective_message = message
        self.callback_query = query


async def pass_guard(update, path) -> bool:
    """True, если сторож пропустил дальше."""
    try:
        await handler.guard(update, None, path=path)
    except ApplicationHandlerStop:
        return False
    return True


# --- чего не хватает ---------------------------------------------------------


def test_a_newcomer_lacks_both_required_consents(newcomer, db_path):
    assert consent.missing(newcomer, path=db_path) == (
        consent.OFFER,
        consent.PERSONAL_DATA,
    )
    assert consent.ready(newcomer, path=db_path) is False


def test_news_are_not_required(newcomer, db_path):
    """Отказ от рекламы не должен мешать пользоваться ботом."""
    consent.accept(newcomer, consent.OFFER, path=db_path)
    consent.accept(newcomer, consent.PERSONAL_DATA, path=db_path)
    consent.refuse_news(newcomer, path=db_path)

    assert consent.ready(newcomer, path=db_path) is True
    assert consent.has(newcomer, consent.NEWS, path=db_path) is False


def test_each_consent_is_recorded_with_its_date_and_document(newcomer, db_path):
    """Дату спросят при споре, адрес отвечает, с чем именно согласились."""
    consent.accept(newcomer, consent.OFFER, today=TODAY, path=db_path)
    consent.accept(newcomer, consent.PERSONAL_DATA, today=TODAY, path=db_path)

    offer = consent.given(newcomer, consent.OFFER, path=db_path)
    personal = consent.given(newcomer, consent.PERSONAL_DATA, path=db_path)

    assert offer.document == OFFER
    assert personal.document == PRIVACY
    assert personal.at == TODAY
    # Обе записи об одном нажатии обязаны показывать одну дату.
    assert offer.at == personal.at


def test_the_offer_consent_lives_where_it_always_did(newcomer, db_path):
    """Вторая запись о том же согласии однажды дала бы две разные даты."""
    consent.accept(newcomer, consent.OFFER, path=db_path)

    # Та же дверь, которой пользуется подключение кабинета: значит подключение
    # не станет спрашивать оферту второй раз.
    assert clients.has_consent(newcomer, path=db_path) is True


def test_an_old_client_is_not_asked_for_the_offer_again(db_path, documents):
    """Кто согласился при подключении кабинета, тот уже согласился.

    Экран согласий посреди работы читается как «бота сломали», поэтому
    старое согласие засчитывается, а спрашивается только недостающее.
    """
    client_id = db.admin_repo(db_path).ensure_client(700200)
    clients.record_consent(client_id, path=db_path)

    assert consent.missing(client_id, path=db_path) == (consent.PERSONAL_DATA,)


def test_the_news_consent_is_the_same_one_the_mailing_obeys(newcomer, db_path):
    """Две копии одного согласия разошлись бы в первый же день."""
    consent.accept(newcomer, consent.NEWS, today=TODAY, path=db_path)

    assert leads.allowed(newcomer, path=db_path) is True
    assert leads.answered_at(newcomer, path=db_path) == TODAY

    consent.refuse_news(newcomer, today=TODAY, path=db_path)
    assert leads.allowed(newcomer, path=db_path) is False
    # Отказ это тоже ответ: второй раз спрашивать значит навязываться.
    assert consent.answered_news(newcomer, path=db_path) is True


def test_an_unknown_consent_is_an_error_not_a_silent_write(newcomer, db_path):
    with pytest.raises(ValueError):
        consent.accept(newcomer, "что-нибудь", path=db_path)


# --- сторож ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_command_waits_for_the_consents(newcomer, db_path):
    message = FakeMessage("/finance")
    update = FakeUpdate(700100, message=message)

    assert await pass_guard(update, db_path) is False
    assert message.sent, "экран согласий не показан"
    assert "Прежде чем начать" in message.sent[0][0]


@pytest.mark.asyncio
async def test_the_card_by_article_stays_open(newcomer, db_path):
    """Решение владельца: бесплатный крючок работает до всяких отношений."""
    message = FakeMessage("179323396")
    update = FakeUpdate(700100, message=message)

    assert await pass_guard(update, db_path) is True
    assert message.sent == []


@pytest.mark.asyncio
async def test_stop_works_without_any_consent(newcomer, db_path):
    """«Не пишите мне» не может зависеть ни от чего."""
    update = FakeUpdate(700100, message=FakeMessage("/stop"))

    assert await pass_guard(update, db_path) is True


@pytest.mark.asyncio
async def test_a_document_waits_too(newcomer, db_path):
    """Файл себестоимости это уже обработка данных, а не просмотр."""
    message = FakeMessage("", document=SimpleNamespace(file_name="costs.xlsx"))
    update = FakeUpdate(700100, message=message)

    assert await pass_guard(update, db_path) is False


@pytest.mark.asyncio
async def test_other_screens_buttons_wait_too(newcomer, db_path):
    update = FakeUpdate(700100, message=FakeMessage(), query=FakeQuery("tar:buy", None))

    assert await pass_guard(update, db_path) is False


@pytest.mark.asyncio
async def test_the_consent_buttons_are_not_blocked_by_the_guard(newcomer, db_path):
    """Иначе экран согласий запер бы сам себя."""
    update = FakeUpdate(
        700100, message=FakeMessage(), query=FakeQuery(handler.ACCEPT_OFFER, None)
    )

    assert await pass_guard(update, db_path) is True


@pytest.mark.asyncio
async def test_after_both_consents_everything_opens(newcomer, db_path):
    consent.accept(newcomer, consent.OFFER, path=db_path)
    consent.accept(newcomer, consent.PERSONAL_DATA, path=db_path)
    message = FakeMessage("/finance")

    assert await pass_guard(FakeUpdate(700100, message=message), db_path) is True
    assert message.sent == []


@pytest.mark.asyncio
async def test_a_broken_guard_does_not_lock_the_bot(newcomer, db_path, monkeypatch):
    """Запертый бот хуже пропущенного экрана: согласие спросится в следующий раз."""
    monkeypatch.setattr(
        consent, "ready", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("база"))
    )
    update = FakeUpdate(700100, message=FakeMessage("/finance"))

    assert await pass_guard(update, db_path) is True


# --- кнопки ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_buttons_record_each_consent_separately(newcomer, db_path):
    """Три нажатия, три записи. Одной кнопки «со всем согласен» тут нет."""
    query = FakeQuery(handler.ACCEPT_OFFER, FakeMessage())
    await handler.accept_callback(
        FakeUpdate(700100, query=query), None, kind=consent.OFFER, path=db_path
    )
    assert consent.missing(newcomer, path=db_path) == (consent.PERSONAL_DATA,)

    await handler.accept_callback(
        FakeUpdate(700100, query=query), None, kind=consent.PERSONAL_DATA, path=db_path
    )
    assert consent.ready(newcomer, path=db_path) is True
    assert "Готово" in query.edited[-1]


@pytest.mark.asyncio
async def test_the_news_refusal_is_recorded_as_an_answer(newcomer, db_path):
    query = FakeQuery(handler.NEWS_NO, FakeMessage())

    await handler.news_callback(
        FakeUpdate(700100, query=query), None, permit=False, path=db_path
    )

    assert consent.answered_news(newcomer, path=db_path) is True
    assert consent.has(newcomer, consent.NEWS, path=db_path) is False


def test_the_screen_offers_only_what_is_still_undecided(newcomer, db_path):
    consent.accept(newcomer, consent.OFFER, path=db_path)

    text, markup = handler.screen(newcomer, path=db_path)

    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert "Принимаю оферту" not in labels
    assert "Согласен на обработку данных" in labels
    assert "Оферта принята" in text


def test_the_screen_says_the_document_is_pending_without_a_link(newcomer, db_path, monkeypatch):
    """Пустая переменная это не повод молчать или выдумать адрес."""
    monkeypatch.setenv("OFFER_URL", "")

    text, _ = handler.screen(newcomer, path=db_path)

    assert "готовится" in text
