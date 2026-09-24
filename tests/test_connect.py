"""Подключение кабинета: оферта, токен, проверка, отключение.

Швы ровно два, как и договорено в спецификации: транспорт WB подменяется
httpx.MockTransport (сети в тестах нет ни байта) и путь к базе, временный файл.

Отдельная забота этого файла: доказать, что токен нигде не всплывает.
Поэтому строка токена ищется в шифротексте, в журнале и в тексте ответа.
"""

from __future__ import annotations

import base64
import json
import re
from decimal import Decimal
from datetime import datetime, timedelta, timezone

from types import SimpleNamespace

import httpx
import pytest

from bot.handlers import connect
from core import access, audit, clients, crypto, db, queue, wbapi

# Маска посчитана руками, а не кодом под тестом: Контент 1, Аналитика 2,
# Статистика 5, Продвижение 6, Финансы 13 -> 2 + 4 + 32 + 64 + 8192 = 8294.
MASK_FIVE = 8294
READ_ONLY = 1 << 30

SID = "7c1f0a22-9b3d-4e55-8a17-6d2c4b8e0f91"
NOW = datetime(2026, 9, 16, 9, 0, tzinfo=timezone.utc)
OFFER = "https://example.org/offer"


def make_token(**payload) -> str:
    """JWT с нужным payload. Подпись не проверяется, ключа от неё нет ни у кого."""

    def part(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    body = {
        "id": "cd" * 8,
        "sid": SID,
        "exp": int((NOW + timedelta(days=180)).timestamp()),
        "s": MASK_FIVE | READ_ONLY,
        "acc": 3,
    }
    body.update(payload)
    return f"{part({'alg': 'HS256', 'typ': 'JWT'})}.{part(body)}.c2lnbmF0dXJl"


def http(status: int = 200, record: list | None = None) -> httpx.AsyncClient:
    """Транспорт с записанным ответом WB на пробный запрос."""

    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(request)
        return httpx.Response(status, json={"TS": "2026-09-16", "Status": "OK"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture
def ready(db_path, monkeypatch):
    """Клиент в базе, ключ шифрования на месте, оферта опубликована."""
    monkeypatch.setenv("ENCRYPTION_KEY", crypto.generate_key())
    monkeypatch.setenv("OFFER_URL", OFFER)
    # Бюджет запросов к WB общий на процесс, а id клиента в каждом тесте один
    # и тот же: без сброса соседний тест ждал бы чужую паузу.
    wbapi.reset_limits()
    return db.admin_repo(db_path).ensure_client(500500)


@pytest.mark.asyncio
async def test_token_is_not_accepted_until_the_offer_button_is_pressed(db_path, ready):
    with pytest.raises(clients.ConsentRequired):
        await clients.connect(ready, make_token(), http=http(), path=db_path)
    assert db.repo(ready, db_path).count("wb_tokens") == 0


def test_consent_keeps_the_time_and_the_link_to_the_offer(db_path, ready):
    clients.record_consent(ready, now=NOW, path=db_path)

    row = db.repo(ready, db_path).one("consents")
    assert row["offer_url"] == OFFER
    assert row["agreed_at"].startswith("2026-09-16")
    assert clients.has_consent(ready, path=db_path) is True


def test_empty_offer_url_still_lets_agree_and_warns_the_owner(db_path, ready, monkeypatch):
    monkeypatch.setenv("OFFER_URL", "")

    clients.record_consent(ready, now=NOW, path=db_path)

    assert db.repo(ready, db_path).count("consents") == 1
    assert clients.offer_ready() is False
    warnings = audit.recent(level="warning", path=db_path)
    assert any("оферт" in row["message"].lower() for row in warnings)


@pytest.mark.asyncio
async def test_token_is_kept_only_encrypted_and_never_reaches_the_journal(db_path, ready):
    token = make_token()
    clients.record_consent(ready, now=NOW, path=db_path)
    seen: list[httpx.Request] = []

    result = await clients.connect(
        ready, token, http=http(record=seen), path=db_path, now=NOW
    )

    row = db.repo(ready, db_path).one("wb_tokens")
    assert token.encode() not in bytes(row["ciphertext"])
    assert crypto.decrypt(row["ciphertext"]) == token
    assert row["read_only"] == 1
    assert row["exp"].startswith("2027-03-15")
    # Пробный запрос ровно один, и он ушёл на тот домен, где живёт отчёт.
    assert len(seen) == 1 and seen[0].url.host.startswith("finance-api")
    assert db.admin_repo(db_path).client(ready)["seller_id"] == SID
    assert result.missing == ()
    journal = " ".join(
        str(row["message"]) for row in audit.recent(client_id=ready, path=db_path)
    )
    assert token not in journal


@pytest.mark.asyncio
async def test_revoked_token_is_refused_and_nothing_is_stored(db_path, ready):
    clients.record_consent(ready, now=NOW, path=db_path)

    with pytest.raises(wbapi.WBAuthError):
        await clients.connect(ready, make_token(), http=http(401), path=db_path, now=NOW)

    assert db.repo(ready, db_path).count("wb_tokens") == 0


@pytest.mark.asyncio
async def test_second_connect_replaces_the_token_and_returns_paused_days(db_path, ready):
    clients.record_consent(ready, now=NOW, path=db_path)
    await clients.connect(ready, make_token(), http=http(), path=db_path, now=NOW)
    access.grant_access(
        ready, "finance", 30, "WBR-2026-0001", "invoice", "owner", now=NOW, path=db_path
    )
    access.pause(ready, now=NOW, path=db_path)
    other = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"

    result = await clients.connect(
        ready, make_token(sid=other), http=http(), path=db_path, now=NOW
    )

    assert result.replaced is True
    assert result.resumed == 1
    assert db.repo(ready, db_path).count("wb_tokens") == 1
    assert db.admin_repo(db_path).client(ready)["seller_id"] == other
    finance = next(
        item for item in access.status(ready, now=NOW, path=db_path) if item.module == "finance"
    )
    assert finance.state == access.ACTIVE


@pytest.mark.asyncio
async def test_401_pauses_the_modules_and_403_leaves_them_running(db_path, ready):
    clients.record_consent(ready, now=NOW, path=db_path)
    await clients.connect(ready, make_token(), http=http(), path=db_path, now=NOW)
    access.grant_access(
        ready, "finance", 30, "WBR-2026-0002", "invoice", "owner", now=NOW, path=db_path
    )

    # 403 это «токен рабочий, но категории не хватает»: платить за паузу нечем.
    forbidden = clients.on_wb_error(
        ready,
        wbapi.WBForbiddenError("нет категории", status=403, category="promotion"),
        now=NOW,
        path=db_path,
    )
    assert forbidden.kind == "forbidden"
    assert forbidden.category == "promotion"
    assert forbidden.paused == 0
    assert _state(ready, db_path) == access.ACTIVE

    trouble = clients.on_wb_error(
        ready, wbapi.WBAuthError("токен отозван", status=401), now=NOW, path=db_path
    )
    assert trouble.kind == "auth"
    assert trouble.paused == 1
    assert _state(ready, db_path) == access.PAUSED
    assert db.repo(ready, db_path).one("wb_tokens")["last_401_at"].startswith("2026-09-16")


def _state(client_id: int, path) -> str:
    finance = next(
        item
        for item in access.status(client_id, now=NOW, path=path)
        if item.module == "finance"
    )
    return finance.state


@pytest.mark.asyncio
async def test_disconnect_leaves_no_row_of_the_client_in_any_table(db_path, ready):
    clients.record_consent(ready, now=NOW, path=db_path)
    await clients.connect(ready, make_token(), http=http(), path=db_path, now=NOW)
    access.grant_access(
        ready, "finance", 30, "WBR-2026-0003", "invoice", "owner", now=NOW, path=db_path
    )
    repo = db.repo(ready, db_path)
    repo.insert("costs", nm_id=101, cost_per_unit_kop=25000)
    repo.insert("events", level="info", kind="test", message="след клиента")
    repo.insert("api_calls", host="finance-api", method="POST /x", status=200, duration_ms=5)
    repo.insert(
        "invoices",
        number="WBR-2026-0003",
        module="finance",
        period_months=1,
        amount_kop=99000,
    )
    # Видимость из поискового отчёта: новая клиентская таблица тоже обязана
    # уходить вместе с клиентом, а не переживать его.
    repo.insert(
        "funnel_visibility",
        date_from="2026-09-18",
        date_to="2026-09-24",
        nm_id=101,
        visibility=7.0,
    )
    filled = [table for table in db.CLIENT_TABLES if repo.count(table)]
    assert len(filled) >= 7, filled
    assert "funnel_visibility" in filled

    clients.disconnect(ready, path=db_path)

    # Перебираем все таблицы клиента, а не одну: «удалить всё» значит всё.
    for table in sorted(db.CLIENT_TABLES):
        assert repo.count(table) == 0, f"в {table} осталась строка клиента"
    assert db.admin_repo(db_path).client(ready) is None


@pytest.mark.asyncio
async def test_reminders_land_fourteen_and_three_days_before_the_token_dies(db_path, ready):
    clients.record_consent(ready, now=NOW, path=db_path)
    dies = int((NOW + timedelta(days=14)).timestamp())
    await clients.connect(
        ready, make_token(exp=dies), http=http(), path=db_path, now=NOW
    )

    assert clients.REMINDER_DAYS == (14, 3)
    assert clients.due_reminders(now=NOW, path=db_path) == [(ready, 14, 14)]
    clients.mark_reminded(ready, 14, path=db_path)
    # Порог 14 уже отработан, до следующего ещё далеко.
    assert clients.due_reminders(now=NOW + timedelta(days=1), path=db_path) == []
    assert clients.due_reminders(now=NOW + timedelta(days=11), path=db_path) == [
        (ready, 3, 3)
    ]
    # Новый токен обнуляет отметки: у него свой срок и свои напоминания.
    await clients.connect(ready, make_token(), http=http(), path=db_path, now=NOW)
    assert clients.due_reminders(now=NOW, path=db_path) == []


# --- поверхность бота ---


class FakeMessage:
    def __init__(self, text: str = ""):
        self.text = text
        self.sent: list[tuple[str, dict]] = []
        self.deleted = False

    async def reply_text(self, text, **kwargs):
        self.sent.append((text, kwargs))
        return self

    async def delete(self):
        self.deleted = True


class FakeQuery:
    def __init__(self, data, message):
        self.data = data
        self.message = message
        self.answered = False

    async def answer(self, *args, **kwargs):
        self.answered = True


class FakeUpdate:
    def __init__(self, telegram_id, message, query=None):
        self.effective_user = SimpleNamespace(id=telegram_id)
        self.effective_message = message
        self.callback_query = query


class FakeContext:
    """То, что даёт PTB. Путь к базе сюда не кладётся: он приходит параметром."""

    def __init__(self):
        self.bot_data = {}
        self.user_data = {}


@pytest.fixture
def wb(monkeypatch):
    """Транспорт WB для хендлера: ядро берёт общую сессию, её и подменяем.

    Хендлер про транспорт не знает по соглашению о швах, поэтому шов
    подставляется там, где он и живёт, в core.wbapi.
    """
    session = http()
    monkeypatch.setattr("core.wbapi.diag.shared_session", lambda: session)
    return session


def test_instruction_names_five_categories_and_asks_for_a_separate_read_only_token():
    text = connect.instruction_text()
    for title in ("Статистика", "Финансы", "Аналитика", "Продвижение", "Контент"):
        assert title in text, title
    assert "Только на чтение" in text
    assert "отдельн" in text.lower()


def test_offer_line_is_honest_while_the_text_of_the_offer_is_being_prepared(monkeypatch):
    monkeypatch.setenv("OFFER_URL", "")
    assert "готов" in connect.offer_line().lower()
    monkeypatch.setenv("OFFER_URL", OFFER)
    assert OFFER in connect.offer_line()


@pytest.mark.asyncio
async def test_token_sent_before_the_button_is_explained_and_not_stored(db_path, ready):
    token = make_token()
    message = FakeMessage(token)

    await connect.token_message(FakeUpdate(500500, message), FakeContext(), path=db_path)

    answer = message.sent[0][0]
    assert "оферт" in answer.lower()
    assert token not in answer
    assert db.repo(ready, db_path).count("wb_tokens") == 0
    # Кнопка согласия идёт вместе с объяснением, чтобы не искать её заново.
    assert message.sent[0][1]["reply_markup"].inline_keyboard[0][0].callback_data == (
        "connect:agree"
    )


def test_missing_category_names_it_and_what_stops_working():
    text = connect.missing_text(("finance", "promotion"))
    assert "Финансы" in text and "Продвижение" in text
    assert "диагностик" in text.lower()
    assert "реклам" in text.lower()
    assert "Аналитика" not in text


class FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((int(chat_id), text))


class FakeApp:
    def __init__(self):
        self.bot = FakeBot()
        self.handlers: list = []

    def add_handler(self, handler, group=0):
        self.handlers.append((handler, group))


@pytest.mark.asyncio
async def test_answer_names_the_deadline_the_gap_and_the_modules(db_path, ready, wb):
    clients.record_consent(ready, now=NOW, path=db_path)
    access.grant_access(
        ready, "finance", 30, "WBR-2026-0004", "invoice", "owner", now=NOW, path=db_path
    )
    # Токен без категории Финансы и без галочки «Только на чтение».
    token = make_token(s=MASK_FIVE ^ (1 << 13))
    message = FakeMessage(token)

    await connect.token_message(FakeUpdate(500500, message), FakeContext(), path=db_path)

    answer = message.sent[0][0]
    assert token not in answer
    assert "подключён" in answer.lower()
    assert "Финансы" in answer
    assert "диагностик" in answer.lower()
    assert "Только на чтение" in answer
    assert "Персональный" in answer
    assert "активен до" in answer
    assert message.deleted is True
    assert db.repo(ready, db_path).count("wb_tokens") == 1


@pytest.mark.asyncio
async def test_disconnect_asks_first_and_only_then_removes_everything(db_path, ready):
    clients.record_consent(ready, now=NOW, path=db_path)
    await clients.connect(ready, make_token(), http=http(), path=db_path, now=NOW)
    message = FakeMessage()

    await connect.disconnect_command(FakeUpdate(500500, message), FakeContext(), path=db_path)

    text, kwargs = message.sent[0]
    assert "удал" in text.lower()
    assert kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "connect:wipe"
    assert db.repo(ready, db_path).count("wb_tokens") == 1

    answer = FakeMessage()
    query = FakeQuery("connect:wipe", answer)
    await connect.wipe_callback(FakeUpdate(500500, answer, query), FakeContext(), path=db_path)

    for table in sorted(db.CLIENT_TABLES):
        assert db.repo(ready, db_path).count(table) == 0, f"осталось в {table}"


@pytest.mark.asyncio
async def test_daily_job_reminds_about_the_deadline_of_the_token(db_path, ready):
    clients.record_consent(ready, now=NOW, path=db_path)
    dies = int((NOW + timedelta(days=14)).timestamp())
    await clients.connect(ready, make_token(exp=dies), http=http(), path=db_path, now=NOW)
    app = FakeApp()
    job = connect.make_reminder(app, path=db_path, now=NOW)

    await job(SimpleNamespace(id=1, client_id=None, kind="x", payload={}, attempts=0))

    chat_id, text = app.bot.sent[0]
    assert chat_id == 500500
    assert "14" in text
    assert "/connect" in text


@pytest.mark.asyncio
async def test_401_from_a_background_task_pauses_and_explains_about_the_token(db_path, ready):
    token = make_token()
    clients.record_consent(ready, now=NOW, path=db_path)
    await clients.connect(ready, token, http=http(), path=db_path, now=NOW)
    access.grant_access(
        ready, "finance", 30, "WBR-2026-0005", "invoice", "owner", now=NOW, path=db_path
    )
    app = FakeApp()
    notice = connect.make_auth_notice(app, path=db_path)
    task = SimpleNamespace(id=2, client_id=ready, kind="finance_report", payload={}, attempts=1)

    await notice(task, wbapi.WBAuthError("токен отозван", status=401))

    chat_id, text = app.bot.sent[0]
    assert chat_id == 500500
    assert token not in text
    assert "паузу" in text.lower()
    assert _state(ready, db_path) == access.PAUSED


@pytest.mark.asyncio
async def test_second_connect_offers_to_replace_the_token_not_a_second_cabinet(db_path, ready):
    clients.record_consent(ready, now=NOW, path=db_path)
    await clients.connect(ready, make_token(), http=http(), path=db_path, now=NOW)
    message = FakeMessage()

    await connect.connect_command(FakeUpdate(500500, message), FakeContext(), path=db_path)

    text, kwargs = message.sent[0]
    assert "замен" in text.lower()
    assert "втор" in text.lower()
    assert kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "connect:replace"


@pytest.mark.asyncio
async def test_token_inside_a_sentence_is_taken_and_the_message_is_removed(db_path, ready, wb):
    token = make_token()
    written = f"вот мой токен {token} , принимай"
    clients.record_consent(ready, now=NOW, path=db_path)
    message = FakeMessage(written)

    await connect.token_message(FakeUpdate(500500, message), FakeContext(), path=db_path)

    # Обещание «удалю из переписки» должно работать и тогда, когда человек
    # пишет по-человечески, а не голым токеном.
    assert re.search(connect.TOKEN_PATTERN, written) is not None
    assert "подключён" in message.sent[0][0].lower()
    assert message.deleted is True
    assert db.repo(ready, db_path).count("wb_tokens") == 1


@pytest.mark.asyncio
async def test_an_article_number_is_not_mistaken_for_a_token(db_path, ready):
    for ordinary in ("123456789", "https://www.wildberries.ru/catalog/123456789/detail.aspx"):
        assert re.search(connect.TOKEN_PATTERN, ordinary) is None


@pytest.mark.asyncio
async def test_a_service_test_token_is_named_and_its_risk_explained(db_path, ready, wb):
    clients.record_consent(ready, now=NOW, path=db_path)
    message = FakeMessage(make_token(acc=4, t=True))

    await connect.token_message(FakeUpdate(500500, message), FakeContext(), path=db_path)

    answer = message.sent[0][0]
    assert "Сервисный" in answer
    assert "Персональный" in answer
    assert "песочниц" in answer.lower()
    assert db.repo(ready, db_path).count("wb_tokens") == 1


@pytest.mark.asyncio
async def test_connection_survives_a_message_that_cannot_be_deleted(db_path, ready, wb):
    class Stubborn(FakeMessage):
        async def delete(self):
            raise RuntimeError("удалить чужое сообщение не дали")

    clients.record_consent(ready, now=NOW, path=db_path)
    message = Stubborn(make_token())

    await connect.token_message(FakeUpdate(500500, message), FakeContext(), path=db_path)

    assert "подключён" in message.sent[0][0].lower()
    assert db.repo(ready, db_path).count("wb_tokens") == 1


@pytest.mark.asyncio
async def test_a_missed_day_keeps_the_reminder_and_two_runs_do_not_send_it_twice(db_path, ready):
    clients.record_consent(ready, now=NOW, path=db_path)
    dies = int((NOW + timedelta(days=14)).timestamp())
    await clients.connect(ready, make_token(exp=dies), http=http(), path=db_path, now=NOW)
    app = FakeApp()
    task = SimpleNamespace(id=1, client_id=None, kind="x", payload={}, attempts=0)
    # Бот молчал двенадцать дней: порог 14 пропущен, до конца срока 2 дня.
    late = connect.make_reminder(app, path=db_path, now=NOW + timedelta(days=12))

    await late(task)
    assert len(app.bot.sent) == 1
    assert "2 дн" in app.bot.sent[0][1]

    await late(task)
    assert len(app.bot.sent) == 1, "второй запуск подряд не должен повторять напоминание"


@pytest.mark.asyncio
async def test_deadlines_of_all_clients_are_read_in_one_selection(db_path, ready, monkeypatch):
    clients.record_consent(ready, now=NOW, path=db_path)
    await clients.connect(
        ready,
        make_token(exp=int((NOW + timedelta(days=14)).timestamp())),
        http=http(),
        path=db_path,
        now=NOW,
    )
    other = db.admin_repo(db_path).ensure_client(600600)
    clients.record_consent(other, now=NOW, path=db_path)
    await clients.connect(
        other,
        make_token(
            sid="9f8e7d6c-5b4a-4321-9876-0a1b2c3d4e5f",
            exp=int((NOW + timedelta(days=3)).timestamp()),
        ),
        http=http(),
        path=db_path,
        now=NOW,
    )

    calls: list[int] = []
    original = db.AdminRepo.tokens_with_exp

    def spy(self):
        calls.append(1)
        return original(self)

    monkeypatch.setattr(db.AdminRepo, "tokens_with_exp", spy)

    due = clients.due_reminders(now=NOW, path=db_path)

    # Сроки читаются один раз на всех, а не по разу на клиента.
    assert calls == [1]
    assert due == [(ready, 14, 14), (other, 3, 3)]


@pytest.mark.asyncio
async def test_journal_keeps_the_sent_reminder_after_the_token_is_replaced(db_path, ready):
    clients.record_consent(ready, now=NOW, path=db_path)
    dies = int((NOW + timedelta(days=14)).timestamp())
    await clients.connect(ready, make_token(exp=dies), http=http(), path=db_path, now=NOW)
    clients.mark_reminded(ready, 14, path=db_path)

    def sent_notes():
        return [
            row
            for row in audit.recent(client_id=ready, path=db_path)
            if "напоминание о сроке токена" in str(row["message"]).lower()
        ]

    assert clients.reminded(ready, path=db_path) == (14,)
    assert len(sent_notes()) == 1

    await clients.connect(
        ready,
        make_token(sid="0b1c2d3e-4f50-4617-8829-a0b1c2d3e4f5", exp=dies),
        http=http(),
        path=db_path,
        now=NOW,
    )

    # Журнал это летопись: запись об отправке никуда не делась.
    assert len(sent_notes()) == 1
    # А состояние обнулилось, и новое напоминание по новому токену уйдёт.
    assert clients.reminded(ready, path=db_path) == ()
    assert clients.due_reminders(now=NOW, path=db_path) == [(ready, 14, 14)]


@pytest.mark.asyncio
async def test_a_message_without_a_readable_token_is_also_removed(db_path, ready):
    # Похоже на токен для глаза, но не JWT: человек ошибся при копировании.
    message = FakeMessage("вот держи eyJhbGciOi.короткий")

    await connect.token_message(FakeUpdate(500500, message), FakeContext(), path=db_path)

    assert connect.BAD_TOKEN_TEXT in message.sent[0][0]
    # Удаляем во всех ветках: висеть в истории не должно даже неудачное.
    assert message.deleted is True


@pytest.mark.asyncio
async def test_a_naive_now_is_read_as_utc_and_does_not_break_the_reminders(db_path, ready):
    clients.record_consent(ready, now=NOW, path=db_path)
    dies = int((NOW + timedelta(days=14)).timestamp())
    await clients.connect(ready, make_token(exp=dies), http=http(), path=db_path, now=NOW)
    naive = NOW.replace(tzinfo=None)

    assert clients.days_left(ready, now=naive, path=db_path) == 14
    assert clients.due_reminders(now=naive, path=db_path) == [(ready, 14, 14)]


@pytest.mark.asyncio
async def test_a_disconnect_that_left_rows_behind_is_not_called_done(db_path, ready, monkeypatch):
    clients.record_consent(ready, now=NOW, path=db_path)
    await clients.connect(ready, make_token(), http=http(), path=db_path, now=NOW)
    # Удаление вдруг перестало удалять: так выглядел бы неверный внешний ключ.
    monkeypatch.setattr(db.AdminRepo, "delete_client", lambda self, client_id: None)

    with pytest.raises(clients.DisconnectIncomplete):
        clients.disconnect(ready, path=db_path)

    errors = audit.recent(level="error", path=db_path)
    assert any("не до конца" in str(row["message"]) for row in errors)

    answer = FakeMessage()
    query = FakeQuery("connect:wipe", answer)
    await connect.wipe_callback(
        FakeUpdate(500500, answer, query), FakeContext(), path=db_path
    )

    assert connect.DISCONNECT_DONE not in answer.sent[0][0]
    assert db.repo(ready, db_path).count("wb_tokens") == 1


# --- обещание проверить связь ---


@pytest.fixture
def worker():
    """Чистый реестр очереди: задачи регистрируют их владельцы."""
    queue.reset()
    yield
    queue.reset()


@pytest.mark.asyncio
async def test_a_token_taken_without_a_probe_is_not_called_checked(db_path, ready, wb, worker):
    clients.record_consent(ready, now=NOW, path=db_path)
    # Бюджет пробных запросов маленький и общий: три штуки за полминуты.
    # Четвёртое подключение подряд идёт уже без проверки связи.
    for _ in range(3):
        await connect.token_message(
            FakeUpdate(500500, FakeMessage(make_token())), FakeContext(), path=db_path
        )
    message = FakeMessage(make_token())

    await connect.token_message(FakeUpdate(500500, message), FakeContext(), path=db_path)

    answer = message.sent[0][0]
    assert "подключён" in answer.lower()
    # Про связь сказано честно: разобрали, а проверим позже.
    assert "проверю" in answer.lower() and "фоне" in answer.lower()
    # И обещание подкреплено задачей, а не только словами.
    planned = [
        row
        for row in db.admin_repo(db_path).tasks(limit=50)
        if row["kind"] == connect.TOKEN_CHECK
    ]
    assert len(planned) == 1
    assert planned[0]["client_id"] == ready
    assert planned[0]["state"] == queue.PENDING


@pytest.mark.asyncio
async def test_a_probed_token_says_nothing_about_a_later_check(db_path, ready, wb, worker):
    clients.record_consent(ready, now=NOW, path=db_path)
    message = FakeMessage(make_token())

    await connect.token_message(FakeUpdate(500500, message), FakeContext(), path=db_path)

    assert "фоне" not in message.sent[0][0].lower()
    assert db.admin_repo(db_path).tasks(limit=50) == []


@pytest.mark.asyncio
async def test_the_promised_check_happens_and_a_dead_token_pauses_the_modules(
    db_path, ready, wb, worker, monkeypatch
):
    clients.record_consent(ready, now=NOW, path=db_path)
    await clients.connect(ready, make_token(), http=http(), path=db_path, now=NOW)
    access.grant_access(
        ready, "finance", 30, "WBR-2026-0006", "invoice", "owner", now=NOW, path=db_path
    )
    app = FakeApp()
    queue.register(connect.TOKEN_CHECK, connect.make_token_check(path=db_path))
    queue.set_auth_handler(connect.make_auth_notice(app, path=db_path))
    # Фоновая проверка идёт через общего клиента WB, там же и шов транспорта.
    monkeypatch.setattr("core.wbapi.client.shared_session", lambda: http(401))
    task_id = queue.enqueue(ready, connect.TOKEN_CHECK, notify=False, path=db_path)

    assert await queue.run_once(path=db_path) is True

    chat_id, text = app.bot.sent[0]
    assert chat_id == 500500
    assert "паузу" in text.lower()
    assert _state(ready, db_path) == access.PAUSED
    # Повторять бессмысленно: дело в ключе, а не в доступности WB.
    assert db.admin_repo(db_path).task(task_id)["state"] == queue.CANCELLED


@pytest.mark.asyncio
async def test_a_living_token_is_checked_quietly(db_path, ready, wb, worker, monkeypatch):
    clients.record_consent(ready, now=NOW, path=db_path)
    await clients.connect(ready, make_token(), http=http(), path=db_path, now=NOW)
    app = FakeApp()
    queue.register(connect.TOKEN_CHECK, connect.make_token_check(path=db_path))
    queue.set_auth_handler(connect.make_auth_notice(app, path=db_path))
    monkeypatch.setattr("core.wbapi.client.shared_session", lambda: http(200))
    task_id = queue.enqueue(ready, connect.TOKEN_CHECK, notify=False, path=db_path)

    await queue.run_once(path=db_path)

    # Хорошая новость это не повод писать клиенту: он ничего не спрашивал.
    assert app.bot.sent == []
    assert db.admin_repo(db_path).task(task_id)["state"] == queue.DONE


@pytest.mark.asyncio
async def test_a_repeated_token_does_not_plan_a_second_check(db_path, ready, wb, worker):
    clients.record_consent(ready, now=NOW, path=db_path)
    for _ in range(5):
        await connect.token_message(
            FakeUpdate(500500, FakeMessage(make_token())), FakeContext(), path=db_path
        )

    planned = [
        row
        for row in db.admin_repo(db_path).tasks(limit=50)
        if row["kind"] == connect.TOKEN_CHECK
    ]
    assert len(planned) == 1


# --- чужой текст в подтверждении подключения ---
#
# ID продавца бот берёт из самого токена, а токен приносит человек. Ответ
# уходит с ParseMode.HTML: осмысленная угловая скобка стала бы разметкой от
# имени бота, случайная - ошибкой Telegram, и тогда клиент не узнает даже
# того, что кабинет подключился.

TRAP = '<a href="http://zlo.example">нажми</a>'


@pytest.mark.asyncio
async def test_a_seller_id_from_the_token_does_not_become_markup(db_path, ready, wb):
    clients.record_consent(ready, now=NOW, path=db_path)
    message = FakeMessage(make_token(sid=TRAP))

    await connect.token_message(FakeUpdate(500500, message), FakeContext(), path=db_path)

    answer = message.sent[0][0]
    assert "<a href" not in answer
    assert "&lt;a href=&quot;" in answer
    assert "<b>" in answer  # разметка самого бота при этом на месте


def test_the_offer_link_from_the_environment_does_not_become_markup(monkeypatch):
    monkeypatch.setenv("OFFER_URL", TRAP)

    text = connect.offer_line()

    assert "<a href" not in text
    assert "&lt;a href=&quot;" in text


# --- себестоимость: про неё говорят сразу, а не после пустого отчёта -------
#
# Живая проверка владельцем: кабинет подключён, прибыльность запрошена, а
# прибыль не посчитана ни по одному артикулу, потому что себестоимости нет и
# бот отказывается её выдумывать. Отказ правильный, молчание нет. Подключение
# это первое место, где человек уже настроен что-то делать, поэтому дорога к
# себестоимости показывается здесь.


@pytest.mark.asyncio
async def test_a_fresh_cabinet_is_told_about_the_cost_price_and_gets_the_road(
    db_path, ready, wb
):
    clients.record_consent(ready, now=NOW, path=db_path)
    message = FakeMessage(make_token())

    await connect.token_message(FakeUpdate(500500, message), FakeContext(), path=db_path)

    answer, kwargs = message.sent[0]
    assert "подключён" in answer.lower()
    assert "себестоимость" in answer.lower()
    # Правда про то, что без неё работает: пугать «не работает ничего» нельзя.
    assert "/finance" in answer and "/profit" in answer
    assert "/costs" in answer
    # Дорога под пальцем, а не в памяти: кнопка ведёт в ту же команду.
    buttons = [
        button.callback_data
        for row in kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert buttons == ["menu:costs"]


@pytest.mark.asyncio
async def test_a_seller_who_already_gave_the_cost_price_is_not_told_about_it(
    db_path, ready, wb
):
    """Напоминание останавливает сама себестоимость, и ничего больше не нужно."""
    from core import costs as costs_core

    clients.record_consent(ready, now=NOW, path=db_path)
    costs_core.save_costs(ready, {101: Decimal("10")}, path=db_path)
    message = FakeMessage(make_token())

    await connect.token_message(FakeUpdate(500500, message), FakeContext(), path=db_path)

    answer, kwargs = message.sent[0]
    assert "подключён" in answer.lower()
    assert "Остался один шаг" not in answer
    assert kwargs["reply_markup"] is None
