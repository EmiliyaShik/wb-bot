"""Приём уведомления об оплате по HTTP.

Сервер поднимается на петле (127.0.0.1) нарочно, а не подменяется двойником:
Prodamus шлёт уведомление `multipart/form-data`, и смысл этого файла ровно в
том, чтобы настоящее многочастное тело правда разобралось. Подделанный
разбор выглядел бы как неверная подпись, то есть как потерянная оплата.
Наружу тут никто не ходит, это тот же процесс и тот же компьютер.
"""

from __future__ import annotations

import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from bot import payments, webserver
from core import access, config, db
from core.billing import payform

SECRET = "kluch-platyozhnoy-stranicy"
OWNER_ID = 999002
CLIENT_TG = 880003
BOUNDARY = "granica-testa"


class FakeBot:
    def __init__(self):
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append((int(chat_id), text))


class FakeApp:
    def __init__(self):
        self.bot = FakeBot()


@pytest.fixture
def wired(db_path, monkeypatch):
    monkeypatch.setattr(config, "db_path", lambda: db_path)
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", str(OWNER_ID))
    monkeypatch.setenv(payform.SECRET_VAR, SECRET)
    return db_path


@pytest.fixture
def buyer(wired):
    return db.admin_repo(wired).ensure_client(CLIENT_TG)


def notice(client_id: int, total: str = "2673.00") -> dict:
    return {
        "order_num": f"RP-{client_id}-finance-3-202611100030",
        "sum": total,
        "products[0][name]": "Рентген Продаж, модуль Финансы, 3 месяца",
        "products[0][price]": total,
        "products[0][quantity]": "1",
        "payment_status": "success",
    }


def signed(fields: dict) -> str:
    return payform.signature(payform.php_dict(fields), SECRET)


def multipart(fields: dict) -> bytes:
    """Тело запроса так, как его шлёт Prodamus: многочастная форма."""
    parts = []
    for name, value in fields.items():
        parts.append(
            f"--{BOUNDARY}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
        )
    parts.append(f"--{BOUNDARY}--\r\n")
    return "".join(parts).encode("utf-8")


async def ask(app, fields, sign, path, *, form: bool = False):
    """Шлёт уведомление на поднятый сервер и возвращает код ответа."""
    client = TestClient(TestServer(webserver.make_app(app, path)))
    await client.start_server()
    try:
        if form:
            answer = await client.post(
                webserver.PAY_PATH, data=fields, headers={"Sign": sign}
            )
        else:
            answer = await client.post(
                webserver.PAY_PATH,
                data=multipart(fields),
                headers={
                    "Sign": sign,
                    "Content-Type": f"multipart/form-data; boundary={BOUNDARY}",
                },
            )
        code = answer.status
    finally:
        await client.close()
    await asyncio.gather(*payments.pending())
    return code


@pytest.mark.asyncio
async def test_a_multipart_notification_opens_the_module(wired, buyer):
    app = FakeApp()
    fields = notice(buyer)

    assert await ask(app, fields, signed(fields), wired) == 200
    assert access.access_of(buyer, "finance", path=wired).works is True
    assert app.bot.messages


@pytest.mark.asyncio
async def test_an_ordinary_form_is_understood_too(wired, buyer):
    """Тип тела у платёжного сервиса однажды поменяется, и это не повод падать."""
    app = FakeApp()
    fields = notice(buyer)

    assert await ask(app, fields, signed(fields), wired, form=True) == 200
    assert access.access_of(buyer, "finance", path=wired).works is True


@pytest.mark.asyncio
async def test_a_request_without_a_signature_is_refused(wired, buyer):
    app = FakeApp()

    assert await ask(app, notice(buyer), "", wired) == 403
    assert access.access_of(buyer, "finance", path=wired).works is False


@pytest.mark.asyncio
async def test_a_changed_amount_is_refused(wired, buyer):
    """Подпись считается по телу, и правка суммы ломает её."""
    app = FakeApp()
    fields = notice(buyer)
    sign = signed(fields)
    fields["sum"] = "1.00"

    assert await ask(app, fields, sign, wired) == 403
    assert access.access_of(buyer, "finance", path=wired).works is False


@pytest.mark.asyncio
async def test_a_curious_browser_gets_nothing(wired):
    """Уведомление приходит только POST'ом, отвечать на заход глазами нечего."""
    client = TestClient(TestServer(webserver.make_app(FakeApp(), wired)))
    await client.start_server()
    try:
        assert (await client.get(webserver.PAY_PATH)).status != 200
        assert (await client.get("/")).status == 404
    finally:
        await client.close()


def test_the_port_decides_whether_there_is_a_server(monkeypatch):
    """Нет порта - нет сервера: локально лишний слушающий порт не нужен."""
    monkeypatch.setenv("WEB_PORT", "")
    monkeypatch.setenv("PORT", "")
    assert webserver.web_port() == 0
    monkeypatch.setenv("PORT", "ne-chislo")
    assert webserver.web_port() == 0
    monkeypatch.setenv("PORT", "70000")
    assert webserver.web_port() == 0
    monkeypatch.setenv("PORT", "3000")
    assert webserver.web_port() == 3000


def test_our_own_variable_wins_over_the_hosting_one(monkeypatch):
    """Платформа передаёт PORT сама, но приём денег на этом не строится.

    Не приехала чужая переменная - сервер молча не поднимется, а узнаем мы
    об этом от клиента, который заплатил и не получил доступ.
    """
    monkeypatch.setenv("WEB_PORT", "3000")
    monkeypatch.setenv("PORT", "")
    assert webserver.web_port() == 3000

    monkeypatch.setenv("PORT", "8080")
    assert webserver.web_port() == 3000

    # Своя переменная с мусором не отменяет хостинговую: сервер важнее
    # аккуратности, и опечатка владельца не должна гасить приём оплаты.
    monkeypatch.setenv("WEB_PORT", "ne-chislo")
    assert webserver.web_port() == 8080


@pytest.mark.asyncio
async def test_without_a_port_nothing_is_started(monkeypatch):
    monkeypatch.setenv("WEB_PORT", "")
    monkeypatch.setenv("PORT", "")

    assert await webserver.start(FakeApp()) is None
