"""Старт бота: база создаётся, отсутствие ключа и админов честно объявляется."""

import pytest

from bot import app as bot_app
from bot import texts
from core import audit, db


def test_startup_creates_database_in_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ADMIN_TELEGRAM_IDS", raising=False)
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    report = bot_app.startup()
    db.close_all()
    assert (tmp_path / "wbrentgen.db").exists()
    assert report["schema_version"] >= 1
    assert report["admins"] == 0
    assert report["tokens_enabled"] is False


def test_startup_writes_warning_when_no_admins(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ADMIN_TELEGRAM_IDS", raising=False)
    monkeypatch.setenv("ENCRYPTION_KEY", "")
    bot_app.startup()
    messages = [row["message"] for row in audit.recent()]
    db.close_all()
    assert any("ADMIN_TELEGRAM_IDS" in m for m in messages)
    assert any("ENCRYPTION_KEY" in m for m in messages)


def test_startup_is_repeatable(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    first = bot_app.startup()
    second = bot_app.startup()
    db.close_all()
    assert first["schema_version"] == second["schema_version"]


def test_startup_counts_admins(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "42,43")
    report = bot_app.startup()
    db.close_all()
    assert report["admins"] == 2


# --- тексты знакомства обещают только то, что работает ---

PROMISES = ("скоро", "готовится", "откроется", "появится позже", "в разработке")
LIVE_COMMANDS = (
    "/connect",
    "/disconnect",
    "/diagnostic",
    "/tariffs",
    "/trial",
    "/costs",
    "/finance",
    "/dynamics",
    "/profit",
    "/plan",
    "/rnp",
    "/settings",
    "/paysupport",
)


@pytest.mark.parametrize("text", [texts.INTRO, texts.HELP], ids=["intro", "help"])
def test_greeting_does_not_promise_the_future(text):
    lowered = text.lower()
    found = [word for word in PROMISES if word in lowered]
    assert not found, f"текст знакомства обещает будущее: {found}"


def test_greeting_names_what_is_free_and_what_is_paid():
    lowered = texts.INTRO.lower()
    assert "бесплатно" in lowered
    assert "/diagnostic" in texts.INTRO
    assert "/tariffs" in texts.INTRO
    assert "/connect" in texts.INTRO


def test_help_lists_every_live_command():
    missing = [command for command in LIVE_COMMANDS if command not in texts.HELP]
    assert not missing, f"в /help нет команд: {missing}"


# --- нерабочий токен объясняется так же понятно, как пустой ---


def test_empty_token_explains_itself_in_russian():
    with pytest.raises(SystemExit) as exit_info:
        bot_app.run("")
    message = str(exit_info.value)
    assert "TELEGRAM_BOT_TOKEN" in message
    assert ".env.example" in message
    assert "BotFather" in message


def test_token_rejected_while_polling_is_also_explained(monkeypatch, tmp_path):
    from telegram.error import InvalidToken

    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    class Rejecting:
        def run_polling(self, **kwargs):
            raise InvalidToken("The token was rejected by the server.")

    monkeypatch.setattr(bot_app, "build_app", lambda token: Rejecting())
    with pytest.raises(SystemExit) as exit_info:
        bot_app.run("123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    db.close_all()
    message = str(exit_info.value)
    assert message == bot_app.TOKEN_REJECTED
    # объяснение по-русски и без трассировки, как и на пустой токен
    assert "Traceback" not in message
    assert "InvalidToken" not in message
    assert "TELEGRAM_BOT_TOKEN" in message
    assert "BotFather" in message


def test_no_link_at_startup_is_a_pause_not_a_crash(monkeypatch, tmp_path):
    """Обрыв связи в момент запуска: бот ждёт и пробует сам, без человека.

    Это не bootstrap_retries: до него дело не доходит. Токен библиотека
    проверяет вызовом getMe внутри initialize(), повторов там нет никаких, и
    деплой во время помех у провайдера кончался трассировкой и выходом.
    """
    from telegram.error import NetworkError

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    waited: list[float] = []
    monkeypatch.setattr(bot_app.time, "sleep", waited.append)

    attempts: list[int] = []

    class Launch:
        def run_polling(self, **kwargs):
            attempts.append(len(attempts) + 1)
            if len(attempts) < 3:
                raise NetworkError("httpx.ConnectError: ")
            return None  # связь вернулась, бот работает и потом погашен

        def __init__(self):
            pass

    monkeypatch.setattr(bot_app, "build_app", lambda token: Launch())
    bot_app.run("123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    db.close_all()

    assert attempts == [1, 2, 3], "после обрыва запуск повторяется сам"
    assert len(waited) == 2
    assert waited == sorted(waited) and waited[0] > 0, "пауза растёт, а не долбит сеть"
    outage = [
        row["message"] for row in audit.recent(limit=20) if "Запуск не удался" in row["message"]
    ]
    assert len(outage) == 2, "каждая неудача запуска остаётся в журнале"


def test_polling_is_told_never_to_give_up_on_the_handshake(monkeypatch, tmp_path):
    """bootstrap_retries = -1 это «бесконечно»: ноль по умолчанию ронял процесс."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    seen: dict = {}

    class Launch:
        def run_polling(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(bot_app, "build_app", lambda token: Launch())
    bot_app.run("123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    db.close_all()

    assert seen["bootstrap_retries"] == bot_app.BOOTSTRAP_RETRIES_FOREVER < 0


def test_startup_does_not_run_without_a_token(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    with pytest.raises(SystemExit):
        bot_app.run(None)
    assert not (tmp_path / "wbrentgen.db").exists(), "база создана зря"
