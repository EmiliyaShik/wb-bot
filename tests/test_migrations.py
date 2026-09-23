"""Сторож схемы: файл migrations/*.sql применяется при каждом старте.

Схему в этом проекте правят на месте, одним файлом, поэтому migrate() гонит
его заново на каждом запуске. Всё держится на одном свойстве: каждый оператор
файла можно выполнить дважды. Появится там INSERT начальных данных или
ALTER TABLE, и повторный прогон либо задвоит строки, либо упадёт, а заметить
это будет некому. Отсюда третий сторож проекта, рядом с длинными тире
(tests/test_texts_no_emdash.py) и отключёнными методами WB (tests/test_wbapi.py).

Разбор настоящий, а не поиск подстроки: границы операторов ставит сам SQLite
(sqlite3.complete_statement, он знает про CREATE TRIGGER с вложенными
операторами), комментарии и строковые литералы снимает разбор из core.db.
Слово INSERT в комментарии или внутри кавычек сторожа не поднимает.

Чего сторож не умеет: доказать идемпотентность произвольного оператора. Вместо
этого он разрешает только те формы, которые заведомо переживают повтор
(CREATE ... IF NOT EXISTS, DROP ... IF EXISTS, PRAGMA), а всё остальное
объявляет нарушением. Дыра у такого правила одна и она в другую сторону:
честный оператор непривычной формы придётся либо переписать, либо осознанно
добавить в список разрешённых - молча пройти он не сможет.
"""

import re
import sqlite3
from pathlib import Path

import pytest

from core import audit, db

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = ROOT / "migrations"

# Формы, которые можно выполнить второй раз без последствий.
REPEATABLE = (
    re.compile(r"^CREATE\s+(?:UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\b", re.I),
    re.compile(r"^CREATE\s+(?:VIRTUAL\s+)?TABLE\s+IF\s+NOT\s+EXISTS\b", re.I),
    re.compile(r"^CREATE\s+(?:VIEW|TRIGGER)\s+IF\s+NOT\s+EXISTS\b", re.I),
    re.compile(r"^DROP\s+(?:TABLE|INDEX|VIEW|TRIGGER)\s+IF\s+EXISTS\b", re.I),
    re.compile(r"^PRAGMA\b", re.I),
)


def offenders(sql: str) -> list[str]:
    """Операторы, которые нельзя выполнить дважды. Пусто - файл в порядке."""
    found = []
    for number, statement in enumerate(db.split_statements(sql), start=1):
        clean = " ".join(db.strip_sql_comments(statement).split())
        if not clean:
            continue
        if not any(pattern.match(clean) for pattern in REPEATABLE):
            found.append(f"оператор {number}: {clean[:80]}")
    return found


def migration_files() -> list[Path]:
    return sorted(MIGRATIONS.glob("*.sql"))


def test_there_is_something_to_check():
    names = {path.name for path in migration_files()}
    assert "001_initial.sql" in names, "сторож не нашёл файл схемы"


@pytest.mark.parametrize("path", migration_files(), ids=lambda p: p.name)
def test_every_statement_survives_a_second_run(path: Path):
    """Каждый оператор файла схемы можно выполнить повторно."""
    bad = offenders(path.read_text(encoding="utf-8"))
    assert not bad, (
        f"в {path.name} оператор, который нельзя выполнить дважды. Схема "
        "применяется при каждом старте бота, поэтому допустимы только "
        "CREATE ... IF NOT EXISTS, DROP ... IF EXISTS и PRAGMA: "
        + "; ".join(bad)
    )


@pytest.mark.parametrize("path", migration_files(), ids=lambda p: p.name)
def test_file_applies_twice_on_real_sqlite(path: Path, tmp_path):
    """Проверка не только по форме: файл дважды прогоняется через саму базу."""
    conn = sqlite3.connect(tmp_path / f"{path.stem}.db", isolation_level=None)
    try:
        sql = path.read_text(encoding="utf-8")
        for _ in range(2):
            for statement in db.split_statements(sql):
                conn.execute(statement)
    finally:
        conn.close()


def test_the_guard_reads_sql_and_not_substrings():
    """Комментарий, литерал и тело триггера сторожа не обманывают и не будят."""
    safe = (
        "-- INSERT INTO clients VALUES (1);\n"
        "CREATE TABLE IF NOT EXISTS t (name TEXT DEFAULT 'INSERT INTO x; y');\n"
        "/* ALTER TABLE t ADD COLUMN z; */\n"
        "CREATE TRIGGER IF NOT EXISTS tr AFTER INSERT ON t BEGIN\n"
        "  UPDATE t SET name = 'x';\n"
        "  DELETE FROM t;\n"
        "END;\n"
    )
    assert offenders(safe) == []

    assert offenders(safe + "INSERT INTO t(name) VALUES ('первый запуск');")
    assert offenders(safe + "ALTER TABLE t ADD COLUMN extra TEXT;")
    assert offenders(safe + "CREATE INDEX idx_t_name ON t(name);")


# --- правка схемы доезжает до готовой базы ---


def test_change_in_schema_file_reaches_a_database_that_knows_version(tmp_path):
    """Версия уже записана, а новый индекс всё равно появляется."""
    path = tmp_path / "old.db"
    version = db.migrate(path)
    conn = db.connect(path)

    # База, где правки файла ещё нет: индекс убран, версия осталась.
    conn.execute("DROP INDEX idx_tasks_no_duplicates")
    assert _indexes(conn) and "idx_tasks_no_duplicates" not in _indexes(conn)

    assert db.migrate(path) == version
    assert "idx_tasks_no_duplicates" in _indexes(conn)
    db.close_all()


def test_statement_rejected_by_data_leaves_a_record_and_lets_the_bot_start(tmp_path):
    """Дубли в таблице: индекс не создан, остальная схема на месте, запись есть."""
    path = tmp_path / "dirty.db"
    db.migrate(path)
    conn = db.connect(path)
    client_id = db.admin_repo(path).ensure_client(424242)

    # Данные, которым новое правило противоречит: две одинаковые задачи.
    conn.execute("DROP INDEX idx_tasks_no_duplicates")
    for _ in range(2):
        conn.execute(
            "INSERT INTO tasks (client_id, kind, payload) VALUES (?, 'finance_report', '{}')",
            (client_id,),
        )

    version = db.migrate(path)

    assert version >= 1, "миграция не должна ронять запуск из-за одного индекса"
    assert "idx_tasks_no_duplicates" not in _indexes(conn)
    # Остальные операторы файла применились: сторож не бросил файл на первой ошибке.
    assert "idx_events_at" in _indexes(conn)

    records = [row["message"] for row in audit.recent(limit=20, level="error", path=path)]
    assert any("idx_tasks_no_duplicates" in text for text in records), (
        "владелец не увидит, почему правило не создано: " + "; ".join(records)
    )
    db.close_all()


def _indexes(conn) -> set[str]:
    return {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    }
