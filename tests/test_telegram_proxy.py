"""Прокси до Telegram: нужен не для анонимности, а чтобы бот вообще работал.

С российского адреса api.telegram.org недоступен, и сервер в России без
прокси не увидит ни одного сообщения. Проверяется то, что дороже ошибки:
прокси достаётся обоим транспортам (опрос и отправка), пустая переменная
ничего не меняет, а логин с паролем из адреса не всплывают в журнале.
"""

from __future__ import annotations

import bot.app as bot_app
from bot import netwatch
from core import audit

PROXY = "http://polzovatel:tajna-proksi-dlinnaya@proxy.example:3128"


def test_without_the_variable_nothing_changes(monkeypatch):
    monkeypatch.setenv(bot_app.TELEGRAM_PROXY_VAR, "")

    assert bot_app.telegram_proxy() == ""
    # Транспорт опроса собирается и без прокси, как собирался всегда.
    assert netwatch.updates_request("") is not None


def test_the_address_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv(bot_app.TELEGRAM_PROXY_VAR, PROXY)

    assert bot_app.telegram_proxy() == PROXY


def test_both_transports_get_the_proxy(monkeypatch):
    """Опрос и отправка идут на один и тот же недоступный адрес.

    Прокси только на опрос значил бы бота, который слышит, но не отвечает.
    """
    monkeypatch.setenv(bot_app.TELEGRAM_PROXY_VAR, PROXY)
    given: list[object] = []

    class FakeBuilder:
        def __getattr__(self, name):
            def step(*args, **kwargs):
                if name in ("request", "get_updates_request"):
                    given.append(args[0] if args else None)
                return self
            return step

        def build(self):
            return FakeApp()

    class FakeApp:
        updater = None
        bot_data: dict = {}
        handlers: list = []

        def add_handler(self, handler, group=0):
            self.handlers.append((handler, group))

        def add_error_handler(self, handler):
            pass

    monkeypatch.setattr(bot_app.Application, "builder", staticmethod(FakeBuilder))
    bot_app.build_app("123:token")

    assert len(given) == 2
    assert all(item is not None for item in given)


def test_the_proxy_password_does_not_reach_the_journal(monkeypatch):
    """В адресе прокси бывает логин с паролем, и в журнал он попасть не должен."""
    monkeypatch.setenv("TELEGRAM_PROXY", PROXY)

    cleaned = audit.redact(f"не удалось сходить через {PROXY}")

    assert "tajna-proksi-dlinnaya" not in cleaned
