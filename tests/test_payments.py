"""Приём уведомления об оплате: что отвечаем Prodamus и кому что говорим.

Сети тут нет: функция получает готовые поля формы и подпись, как их отдал бы
любой сервер. Телеграм заменён двойником. Проверяется то, что дороже всего
стоит ошибки: доступ не открывается без подписи, повтор ничего не ломает, а
сбой телеграма не превращает прошедшую оплату в неудачную.
"""

from __future__ import annotations

import pytest

from bot import payments
from core import access, config, db
from core.billing import payform

SECRET = "kluch-platyozhnoy-stranicy"
OWNER_ID = 999001
CLIENT_TG = 880002


class FakeBot:
    def __init__(self, broken: bool = False):
        self.messages: list[tuple[int, str]] = []
        self.broken = broken

    async def send_message(self, chat_id, text, **kwargs):
        if self.broken:
            raise RuntimeError("Telegram молчит")
        self.messages.append((int(chat_id), text))

    def to(self, chat_id: int) -> list[str]:
        return [text for cid, text in self.messages if cid == chat_id]


class FakeApp:
    def __init__(self, broken: bool = False):
        self.bot = FakeBot(broken)


@pytest.fixture
def wired(db_path, monkeypatch):
    monkeypatch.setattr(config, "db_path", lambda: db_path)
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", str(OWNER_ID))
    monkeypatch.setenv(payform.SECRET_VAR, SECRET)
    return db_path


@pytest.fixture
def buyer(wired):
    return db.admin_repo(wired).ensure_client(CLIENT_TG)


def notice(client_id: int, total: str = "2673.00", status: str = "success") -> dict:
    return {
        "date": "2026-11-10T00:40:00+03:00",
        "order_id": "300155",
        "order_num": f"RP-{client_id}-finance-3-202611100030",
        "sum": total,
        "products[0][name]": "Рентген Продаж, модуль Финансы, 3 месяца",
        "products[0][price]": total,
        "products[0][quantity]": "1",
        "payment_status": status,
    }


def signed(fields: dict) -> str:
    return payform.signature(payform.php_dict(fields), SECRET)


async def receive(app, fields, sign, path):
    return await payments.make_receiver(app, path)(fields, sign)


@pytest.mark.asyncio
async def test_a_paid_order_opens_the_module_and_tells_both(wired, buyer):
    app = FakeApp()
    fields = notice(buyer)

    code, _ = await receive(app, fields, signed(fields), wired)

    assert code == 200
    assert access.access_of(buyer, "finance", path=wired).works is True
    assert "включён" in app.bot.to(CLIENT_TG)[0]
    assert "Оплата прошла" in app.bot.to(OWNER_ID)[0]


@pytest.mark.asyncio
async def test_the_repeat_is_quiet_and_still_answers_two_hundred(wired, buyer):
    """Prodamus повторяет, пока не получит 200. Второе сообщение было бы о том же."""
    app = FakeApp()
    fields = notice(buyer)
    sign = signed(fields)
    await receive(app, fields, sign, wired)
    before = len(app.bot.messages)

    code, _ = await receive(app, fields, sign, wired)

    assert code == 200
    assert len(app.bot.messages) == before


@pytest.mark.asyncio
async def test_a_bad_signature_is_refused_and_the_owner_hears_about_it(wired, buyer):
    app = FakeApp()

    code, _ = await receive(app, notice(buyer), "0" * 64, wired)

    # Не 200 нарочно: так велит документация Prodamus, и повтор тут ничего
    # не исправит, это не обрыв связи.
    assert code == 403
    assert access.access_of(buyer, "finance", path=wired).works is False
    assert app.bot.to(CLIENT_TG) == []
    assert "подпис" in app.bot.to(OWNER_ID)[0]


@pytest.mark.asyncio
async def test_without_the_secret_key_we_ask_to_come_again(wired, buyer, monkeypatch):
    """Ключ зададут - повтор дойдёт. Поэтому код такой, после которого повторяют."""
    monkeypatch.setenv(payform.SECRET_VAR, "")
    app = FakeApp()

    code, _ = await receive(app, notice(buyer), "whatever", wired)

    assert code == 503
    assert "PRODAMUS_SECRET" in app.bot.to(OWNER_ID)[0]


@pytest.mark.asyncio
async def test_an_underpayment_goes_to_the_owner_with_a_ready_command(wired, buyer):
    app = FakeApp()
    fields = notice(buyer, total="1.00")

    code, _ = await receive(app, fields, signed(fields), wired)

    assert code == 200
    assert access.access_of(buyer, "finance", path=wired).works is False
    assert app.bot.to(CLIENT_TG) == []
    owner = app.bot.to(OWNER_ID)[0]
    assert f"/grant {buyer} finance 3 карта" in owner


@pytest.mark.asyncio
async def test_a_cancelled_order_is_nobody_business(wired, buyer):
    """Клиент передумал. Письмо об этом Prodamus владельцу и так пришлёт."""
    app = FakeApp()
    fields = notice(buyer, status="order_canceled")

    code, _ = await receive(app, fields, signed(fields), wired)

    assert code == 200
    assert app.bot.messages == []


@pytest.mark.asyncio
async def test_a_silent_telegram_does_not_undo_a_payment(wired, buyer):
    """Доступ открыт, деньги пришли. Ответ Prodamus от молчания не меняется."""
    app = FakeApp(broken=True)
    fields = notice(buyer)

    code, _ = await receive(app, fields, signed(fields), wired)

    assert code == 200
    assert access.access_of(buyer, "finance", path=wired).works is True


@pytest.mark.asyncio
async def test_a_foreign_order_number_opens_nothing(wired, buyer):
    app = FakeApp()
    fields = notice(buyer)
    fields["order_num"] = "chuzhoy-1"

    code, _ = await receive(app, fields, signed(fields), wired)

    assert code == 200
    assert app.bot.to(CLIENT_TG) == []
    assert "не наш" in app.bot.to(OWNER_ID)[0]
