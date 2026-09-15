"""Старт бота: база создаётся, отсутствие ключа и админов честно объявляется."""

from bot import app as bot_app
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
