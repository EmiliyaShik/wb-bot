"""Журнал: пишет события и вырезает секреты, но не идентификаторы.

Все образцы реквизитов синтетические: повторяющиеся цифры, ни одного
настоящего ИНН, ОГРНИП или счёта. Репозиторий публичный.
"""

from core import audit, config, db

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


# --- место ошибки: где упало, без воспроизведения ---


def _fallen(depth: int = 0, secret: str = "") -> BaseException:
    """Настоящий сбой с настоящим стеком. Выдуманный стек ничего не доказывает."""

    def inner(level: int) -> None:
        if level:
            inner(level - 1)
            return
        payload = f"период dates, токен {secret}" if secret else "неизвестный период: dates"
        raise ValueError(payload)

    try:
        inner(depth)
    except ValueError as error:
        return error
    raise AssertionError("сбой обязан был случиться")


def test_place_of_error_names_the_function_and_the_line():
    place = audit.place_of(_fallen())

    assert "ValueError" in place
    assert "неизвестный период: dates" in place
    assert "inner" in place, "видно, в какой функции упало"
    assert "test_audit.py" in place, "видно, в каком файле упало"


def test_place_of_error_is_empty_without_an_error():
    assert audit.place_of(None) == ""


def test_place_of_error_hides_secrets():
    """В кадрах стека лежат значения переменных, и токен встречается именно там."""
    place = audit.place_of(_fallen(secret=FAKE_JWT))

    assert FAKE_JWT not in place
    assert "eyJ" not in place
    assert audit.HIDDEN in place
    assert "период dates" in place, "сам сбой по-прежнему виден"


def test_place_of_error_does_not_grow_past_the_limit():
    """Журнал лежит в той же базе, что данные кабинетов: стек обязан быть с краю."""
    place = audit.place_of(_fallen(depth=200), limit=400)

    assert 0 < len(place) <= 400
    assert audit.TRACE_CUT in place, "обрезок обязан быть помечен"
    assert "ValueError" in place, "конец стека оставлен: упало там"


def test_place_of_error_limit_comes_from_the_config():
    """В config.toml секция [audit], ключ trace_max_chars."""
    assert audit.trace_max_chars() == int(
        config.settings()["audit"]["trace_max_chars"]
    )
    place = audit.place_of(_fallen(depth=500))
    assert len(place) <= audit.trace_max_chars()


def test_place_of_error_is_off_at_zero():
    assert audit.place_of(_fallen(), limit=0) == ""


def test_log_with_an_error_keeps_the_place_as_its_own_kind(db_path):
    audit.log("queue", None, "задача упала", level="error", path=db_path, error=_fallen())

    rows = audit.recent(limit=10, path=db_path)
    kinds = [row["kind"] for row in rows]
    assert audit.TRACE_KIND in kinds
    assert "queue" in kinds
    place = next(row for row in rows if row["kind"] == audit.TRACE_KIND)
    assert place["level"] == "error", "стек ищут там же, где сам сбой"
    assert "inner" in place["message"]
    assert "задача упала" in place["message"], "по стеку видно, к чему он"


def test_log_without_an_error_writes_no_place(db_path):
    audit.log("queue", None, "задача упала", level="error", path=db_path)

    kinds = [row["kind"] for row in audit.recent(limit=10, path=db_path)]
    assert audit.TRACE_KIND not in kinds


def test_recent_can_leave_the_place_out(db_path):
    audit.log("queue", None, "задача упала", level="error", path=db_path, error=_fallen())

    rows = audit.recent(limit=10, path=db_path, exclude_kind=audit.TRACE_KIND)
    assert [row["kind"] for row in rows] == ["queue"]


def test_place_of_client_error_stays_with_the_client(db_path):
    client_id = db.admin_repo(db_path).ensure_client(telegram_id=79)
    audit.log("queue", client_id, "задача упала", level="error", path=db_path, error=_fallen())

    own = db.repo(client_id, db_path).rows("events")
    assert audit.TRACE_KIND in [row["kind"] for row in own]
    assert db.repo(client_id, db_path).rows("events", kind=audit.TRACE_KIND)
