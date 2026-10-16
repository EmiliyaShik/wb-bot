"""Сторож вывода: секрет не должен попасть в логи ни сообщением, ни стеком."""

from __future__ import annotations

import io
import logging

import pytest

from bot import app
from core import audit

# Нарочно короче и непохожее на настоящий токен: репозиторий публичный, и
# строка вида «цифры:35 символов» в тесте сработала бы как тревога у
# сторожей GitHub. Чистке это безразлично, она ищет значение переменной.
FAKE_TOKEN = "0000000000:TEST-NOT-A-REAL-TOKEN"


@pytest.fixture
def captured(monkeypatch):
    """Корневой журнал с нашим сторожем и выводом в строку."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_filters = list(root.filters)
    saved_level = root.level
    root.handlers = [handler]
    root.filters = []
    root.setLevel(logging.INFO)
    app.hide_secrets_in_logs()
    try:
        yield stream
    finally:
        root.handlers = saved_handlers
        root.filters = saved_filters
        root.setLevel(saved_level)


def test_token_hidden_in_message(captured):
    logging.getLogger("telegram.request").error(
        "сбой запроса https://api.telegram.org/bot%s/getUpdates", FAKE_TOKEN
    )
    out = captured.getvalue()
    assert FAKE_TOKEN not in out
    assert audit.HIDDEN in out


def test_token_hidden_in_traceback(captured):
    try:
        raise RuntimeError(f"401 for url https://api.telegram.org/bot{FAKE_TOKEN}/getMe")
    except RuntimeError:
        logging.getLogger("bot.test").exception("сбой в хендлере")
    out = captured.getvalue()
    assert FAKE_TOKEN not in out
    assert "RuntimeError" in out


def test_plain_message_survives(captured):
    logging.getLogger("bot.test").info("Бот запущен, артикул %s", 123456789)
    out = captured.getvalue()
    assert "Бот запущен" in out
    # Артикул остаётся: прятать его незачем, и это правило чистки журнала.
    assert "123456789" in out


def test_twice_installed_once():
    root = logging.getLogger()
    saved = list(root.filters)
    root.filters = []
    try:
        app.hide_secrets_in_logs()
        app.hide_secrets_in_logs()
        guards = [one for one in root.filters if isinstance(one, app.SecretsHidden)]
        assert len(guards) == 1
    finally:
        root.filters = saved
