"""Журнал: пишет события и вырезает секреты, но не идентификаторы.

Все образцы реквизитов синтетические: повторяющиеся цифры, ни одного
настоящего ИНН, ОГРНИП или счёта. Репозиторий публичный.
"""

from core import audit, db

FAKE_INN = "1111111111"
FAKE_INN_12 = "111111111111"
FAKE_OGRNIP = "222222222222222"
FAKE_ACCOUNT = "33333333333333333333"
FAKE_JWT = "eyJhbGciOiJFUzI1NiJ9.eyJzaWQiOiJ0ZXN0In0.cG9kcGlzcG9kcGlz"


def test_token_is_never_written(db_path):
    audit.log("token", None, f"клиент прислал токен {FAKE_JWT} для проверки", path=db_path)
    rows = audit.recent(path=db_path)
    assert len(rows) == 1
    message = rows[0]["message"]
    assert FAKE_JWT not in message
    assert "eyJ" not in message
    assert audit.HIDDEN in message
    assert "клиент прислал токен" in message


def test_long_opaque_secret_is_cut():
    secret = "A" * 48 + "b7"
    assert secret not in audit.redact(f"ключ {secret} готов")


def test_bank_details_are_cut():
    text = f"счёт {FAKE_ACCOUNT} ИНН {FAKE_INN} ОГРНИП {FAKE_OGRNIP}"
    cleaned = audit.redact(text)
    assert FAKE_ACCOUNT not in cleaned
    assert FAKE_INN not in cleaned
    assert FAKE_OGRNIP not in cleaned
    assert "счёт" in cleaned and "ИНН" in cleaned  # понятно, чего именно нет


def test_inn_of_twelve_digits_is_cut():
    assert FAKE_INN_12 not in audit.redact(f"ИНН: {FAKE_INN_12}")


def test_article_and_telegram_id_survive():
    # ради этих чисел журнал и читают, вырезать их нельзя
    cleaned = audit.redact(
        "клиент 123456789 спросил артикул 179323396, сумма 990 руб, отчёт 2026-09"
    )
    assert "123456789" in cleaned
    assert "179323396" in cleaned
    assert "990" in cleaned


def test_seller_details_from_env_are_cut(monkeypatch):
    monkeypatch.setenv("SELLER_ACCOUNT", "тестовый-реквизит-из-окружения")
    cleaned = audit.redact("платёж на тестовый-реквизит-из-окружения принят")
    assert "тестовый-реквизит-из-окружения" not in cleaned


def test_client_event_is_written_through_client_layer(db_path):
    client_id = db.admin_repo(db_path).ensure_client(telegram_id=77)
    audit.log("access", client_id, "выдан доступ finance", level="error", path=db_path)

    # событие лежит в данных клиента и видно через repo(client_id)
    own = db.repo(client_id, db_path).rows("events")
    assert [row["kind"] for row in own] == ["access"]
    assert own[0]["level"] == "error"

    other = db.admin_repo(db_path).ensure_client(telegram_id=78)
    assert db.repo(other, db_path).rows("events") == []


def test_event_without_client_goes_to_common_layer(db_path):
    audit.log("system", None, "бот запущен", path=db_path)
    row = audit.recent(path=db_path)[0]
    assert row["kind"] == "system"
    assert row["client_id"] is None


def test_logging_failure_does_not_break_the_bot(monkeypatch, db_path):
    def boom(*args, **kwargs):
        raise RuntimeError("базы нет")

    monkeypatch.setattr(db, "admin_repo", boom)
    audit.log("system", None, "старт бота", path=db_path)  # не должно падать
