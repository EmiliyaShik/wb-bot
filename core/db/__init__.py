"""База: соединение, миграции, репозитории.

Наружу выставлены ровно три вещи: migrate(path), repo(client_id) и admin_repo().
SQL, схема и режим WAL остаются здесь.

Изоляция клиентов сделана конструкцией, а не дисциплиной: у репозитория клиента
нет метода, который выполняет произвольный SQL, и нет метода без client_id.
Каждый запрос получает "client_id = ?" в условие автоматически.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from core import config

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent / "migrations"

# Таблицы с данными клиента. Всё, что не здесь, репозиторию клиента недоступно.
CLIENT_TABLES: frozenset[str] = frozenset(
    {
        "consents",
        "wb_tokens",
        "module_access",
        "access_log",
        "invoices",
        "costs",
        "fin_weeks",
        "fin_rows",
        "nm_daily",
        "plans",
        "tasks",
        "api_calls",
        "ai_calls",
        "events",
    }
)


def to_kop(amount: Decimal | int | str) -> int:
    """Рубли в целые копейки. Деньги в базе хранятся только так."""
    value = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    return int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_kop(kopecks: int | None) -> Decimal:
    """Копейки из базы обратно в рубли как Decimal, без потери точности."""
    return Decimal(int(kopecks or 0)) / Decimal(100)

_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")

_connections: dict[str, sqlite3.Connection] = {}
_lock = threading.Lock()


def _resolve(path: str | Path | None) -> Path:
    return Path(path) if path is not None else config.db_path()


def _check_column(name: str) -> str:
    if not _IDENT_RE.match(name):
        raise ValueError(f"недопустимое имя поля: {name}")
    return name


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Соединение с базой. Одно на файл, WAL и внешние ключи включены."""
    target = _resolve(path)
    key = str(target.resolve() if target.parent.exists() else target)
    with _lock:
        conn = _connections.get(key)
        if conn is None:
            target.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(target), check_same_thread=False, isolation_level=None
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            _connections[key] = conn
        return conn


def close_all() -> None:
    """Закрывает все открытые соединения. Нужно тестам и остановке бота."""
    with _lock:
        for conn in _connections.values():
            try:
                conn.close()
            except sqlite3.Error:
                pass
        _connections.clear()


def migrate(path: str | Path | None = None) -> int:
    """Применяет непринятые миграции по возрастанию. Возвращает версию схемы."""
    conn = connect(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_version")}
    for file in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = int(file.name.split("_", 1)[0])
        if version in applied:
            continue
        # executescript сам завершает открытую транзакцию, поэтому оборачивать
        # его в BEGIN нельзя. Миграции пишутся идемпотентными (IF NOT EXISTS),
        # и прерванный посередине запуск просто повторяется при следующем старте.
        conn.executescript(file.read_text(encoding="utf-8"))
        conn.execute("INSERT INTO schema_version(version) VALUES (?)", (version,))
    row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()
    return int(row[0])


class ClientRepo:
    """Данные одного клиента. Другого клиента отсюда не видно."""

    def __init__(self, conn: sqlite3.Connection, client_id: int) -> None:
        self._conn = conn
        self._client_id = int(client_id)

    @property
    def client_id(self) -> int:
        return self._client_id

    def _table(self, table: str) -> str:
        if table not in CLIENT_TABLES:
            raise ValueError(
                f"таблица {table} не принадлежит клиенту; "
                "общие таблицы доступны только через admin_repo()"
            )
        return table

    def _where(self, conditions: dict[str, Any]) -> tuple[str, list[Any]]:
        clauses = ["client_id = ?"]
        params: list[Any] = [self._client_id]
        for column, value in conditions.items():
            clauses.append(f"{_check_column(column)} = ?")
            params.append(value)
        return " AND ".join(clauses), params

    def insert(self, table: str, **values: Any) -> int:
        """Добавляет строку, подставляя client_id. Возвращает rowid."""
        table = self._table(table)
        values = {"client_id": self._client_id, **values}
        columns = [_check_column(name) for name in values]
        placeholders = ", ".join("?" for _ in columns)
        cursor = self._conn.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
            list(values.values()),
        )
        return int(cursor.lastrowid or 0)

    def upsert(self, table: str, keys: dict[str, Any], **values: Any) -> None:
        """Вставляет или обновляет строку по ключу (client_id всегда в ключе)."""
        table = self._table(table)
        keys = {"client_id": self._client_id, **keys}
        row = {**keys, **values}
        columns = [_check_column(name) for name in row]
        placeholders = ", ".join("?" for _ in columns)
        updates = ", ".join(f"{name}=excluded.{name}" for name in values)
        conflict = ", ".join(_check_column(name) for name in keys)
        sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
        sql += f" ON CONFLICT({conflict}) DO UPDATE SET {updates}" if updates else ""
        self._conn.execute(sql, list(row.values()))

    def rows(
        self,
        table: str,
        order_by: str | None = None,
        limit: int | None = None,
        **conditions: Any,
    ) -> list[sqlite3.Row]:
        """Строки клиента с необязательным условием, сортировкой и лимитом."""
        table = self._table(table)
        where, params = self._where(conditions)
        sql = f"SELECT * FROM {table} WHERE {where}"
        if order_by:
            column, _, direction = order_by.partition(" ")
            direction = "DESC" if direction.strip().upper() == "DESC" else "ASC"
            sql += f" ORDER BY {_check_column(column)} {direction}"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return list(self._conn.execute(sql, params))

    def one(self, table: str, **conditions: Any) -> sqlite3.Row | None:
        """Первая подходящая строка клиента или None."""
        found = self.rows(table, limit=1, **conditions)
        return found[0] if found else None

    def count(self, table: str, **conditions: Any) -> int:
        """Сколько строк клиента подходит под условие."""
        table = self._table(table)
        where, params = self._where(conditions)
        row = self._conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {where}", params
        ).fetchone()
        return int(row[0])

    def update(self, table: str, conditions: dict[str, Any], **values: Any) -> int:
        """Обновляет строки клиента. Чужие строки не видны и не меняются."""
        table = self._table(table)
        if not values:
            return 0
        where, params = self._where(conditions or {})
        assignments = ", ".join(f"{_check_column(name)} = ?" for name in values)
        cursor = self._conn.execute(
            f"UPDATE {table} SET {assignments} WHERE {where}",
            list(values.values()) + params,
        )
        return int(cursor.rowcount)

    def delete(self, table: str, **conditions: Any) -> int:
        """Удаляет строки клиента."""
        table = self._table(table)
        where, params = self._where(conditions)
        cursor = self._conn.execute(f"DELETE FROM {table} WHERE {where}", params)
        return int(cursor.rowcount)


class AdminRepo:
    """Общий слой: клиенты, счётчик счетов, очередь, журнал, учёт вызовов.

    Произвольный SQL наружу не выставлен намеренно: иначе рядом с
    конструктивной изоляцией появился бы открытый обход, и правило «к данным
    клиента только через repo(client_id)» держалось бы на дисциплине.

    Что здесь есть и чего нет, дословно:
    - деловые данные клиента (токены, отчёты, себестоимость, счета, доступы)
      отсюда недостижимы ни на чтение, ни на запись; сводка по всем клиентам
      собирается обходом all_clients() и repo(client_id);
    - запись в служебные таблицы (events, tasks, api_calls, ai_calls) идёт
      только без клиента: методы add_* не принимают client_id, строка клиента
      пишется через repo(client_id).insert(...);
    - чтение служебных таблиц возвращает и строки клиентов. Это сознательно:
      владелец обязан видеть упавшие задачи, ошибки и расход по всем сразу
      (истории H1, H1a, H4). Деловых данных в них нет, только служебные поля.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def ensure_client(self, telegram_id: int) -> int:
        """Внутренний id клиента по его Telegram ID, создавая запись при первом визите."""
        self._conn.execute(
            "INSERT OR IGNORE INTO clients (telegram_id) VALUES (?)", (int(telegram_id),)
        )
        row = self._conn.execute(
            "SELECT id FROM clients WHERE telegram_id = ?", (int(telegram_id),)
        ).fetchone()
        return int(row["id"])

    def client(self, client_id: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM clients WHERE id = ?", (int(client_id),)
        ).fetchone()

    def client_by_telegram(self, telegram_id: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM clients WHERE telegram_id = ?", (int(telegram_id),)
        ).fetchone()

    def all_clients(self) -> list[sqlite3.Row]:
        return list(self._conn.execute("SELECT * FROM clients ORDER BY id"))

    def set_client_fields(self, client_id: int, **values: Any) -> None:
        if not values:
            return
        assignments = ", ".join(f"{_check_column(name)} = ?" for name in values)
        self._conn.execute(
            f"UPDATE clients SET {assignments} WHERE id = ?",
            list(values.values()) + [int(client_id)],
        )

    def delete_client(self, client_id: int) -> None:
        """Физически удаляет клиента и все его строки (внешние ключи с CASCADE)."""
        self._conn.execute("DELETE FROM clients WHERE id = ?", (int(client_id),))

    def next_invoice_number(self, year: int) -> str:
        """Следующий номер счёта, сквозной в пределах года и без повторов."""
        year = int(year)
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                "INSERT OR IGNORE INTO invoice_seq (year, last_number) VALUES (?, 0)",
                (year,),
            )
            self._conn.execute(
                "UPDATE invoice_seq SET last_number = last_number + 1 WHERE year = ?",
                (year,),
            )
            row = self._conn.execute(
                "SELECT last_number FROM invoice_seq WHERE year = ?", (year,)
            ).fetchone()
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return f"WBR-{year}-{int(row[0]):04d}"

    # --- журнал ---

    def add_event(self, kind: str, message: str, level: str = "info") -> int:
        """Событие без клиента: старт, расписание, общие ошибки.

        client_id тут не принимается намеренно. Событие клиента пишется через
        repo(client_id).insert("events", ...), иначе запись мимо client_id
        вернулась бы в слой данных этажом ниже.
        """
        cursor = self._conn.execute(
            "INSERT INTO events (level, client_id, kind, message) VALUES (?, NULL, ?, ?)",
            (level, kind, message),
        )
        return int(cursor.lastrowid or 0)

    def events(
        self, limit: int = 50, level: str | None = None, kind: str | None = None
    ) -> list[sqlite3.Row]:
        """Последние записи журнала, свежие сверху. Для владельца."""
        sql = "SELECT * FROM events"
        where: list[str] = []
        params: list[Any] = []
        if level:
            where.append("level = ?")
            params.append(level)
        if kind:
            where.append("kind = ?")
            params.append(kind)
        if where:
            sql += " WHERE " + " AND ".join(where)
        params.append(int(limit))
        return list(self._conn.execute(sql + " ORDER BY id DESC LIMIT ?", params))

    # --- очередь ---

    def add_task(
        self, kind: str, payload: str = "{}", next_run_at: str | None = None
    ) -> int:
        """Задача без клиента: обслуживание, рассылки, проверки расписания.

        Задача клиента ставится через repo(client_id).insert("tasks", ...).
        """
        cursor = self._conn.execute(
            "INSERT INTO tasks (client_id, kind, payload, next_run_at)"
            " VALUES (NULL, ?, ?, ?)",
            (kind, payload, next_run_at),
        )
        return int(cursor.lastrowid or 0)

    def task(self, task_id: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (int(task_id),)
        ).fetchone()

    def tasks(self, state: str | None = None, limit: int = 100) -> list[sqlite3.Row]:
        if state:
            return list(
                self._conn.execute(
                    "SELECT * FROM tasks WHERE state = ? ORDER BY id DESC LIMIT ?",
                    (state, int(limit)),
                )
            )
        return list(
            self._conn.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (int(limit),))
        )

    def due_tasks(self, now: str, limit: int = 10) -> list[sqlite3.Row]:
        """Задачи, которым пора выполняться."""
        return list(
            self._conn.execute(
                "SELECT * FROM tasks WHERE state = 'queued'"
                " AND (next_run_at IS NULL OR next_run_at <= ?)"
                " ORDER BY id LIMIT ?",
                (now, int(limit)),
            )
        )

    def update_task(self, task_id: int, **values: Any) -> int:
        if not values:
            return 0
        assignments = ", ".join(f"{_check_column(name)} = ?" for name in values)
        cursor = self._conn.execute(
            f"UPDATE tasks SET {assignments} WHERE id = ?",
            list(values.values()) + [int(task_id)],
        )
        return int(cursor.rowcount)

    # --- учёт вызовов ---

    def add_api_call(
        self,
        host: str,
        method: str,
        status: int | None = None,
        duration_ms: int | None = None,
    ) -> int:
        """Вызов WB не от имени клиента: /diag владельца, проверки хостов.

        Вызов за клиента пишется через repo(client_id).insert("api_calls", ...).
        """
        cursor = self._conn.execute(
            "INSERT INTO api_calls (client_id, host, method, status, duration_ms)"
            " VALUES (NULL, ?, ?, ?, ?)",
            (host, method, status, duration_ms),
        )
        return int(cursor.lastrowid or 0)

    def api_calls(self, since: str | None = None, limit: int = 500) -> list[sqlite3.Row]:
        if since:
            return list(
                self._conn.execute(
                    "SELECT * FROM api_calls WHERE at >= ? ORDER BY id DESC LIMIT ?",
                    (since, int(limit)),
                )
            )
        return list(
            self._conn.execute(
                "SELECT * FROM api_calls ORDER BY id DESC LIMIT ?", (int(limit),)
            )
        )

    def add_ai_call(
        self,
        provider: str,
        model: str,
        kind: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost_kop: int = 0,
    ) -> int:
        """Расход нейросети не за клиента: токены и стоимость вызова в копейках.

        Расход за клиента пишется через repo(client_id).insert("ai_calls", ...).
        """
        cursor = self._conn.execute(
            "INSERT INTO ai_calls (client_id, provider, model, kind,"
            " prompt_tokens, completion_tokens, cost_kop)"
            " VALUES (NULL, ?, ?, ?, ?, ?, ?)",
            (
                provider,
                model,
                kind,
                int(prompt_tokens),
                int(completion_tokens),
                int(cost_kop),
            ),
        )
        return int(cursor.lastrowid or 0)

    def ai_calls(self, since: str | None = None, limit: int = 500) -> list[sqlite3.Row]:
        if since:
            return list(
                self._conn.execute(
                    "SELECT * FROM ai_calls WHERE at >= ? ORDER BY id DESC LIMIT ?",
                    (since, int(limit)),
                )
            )
        return list(
            self._conn.execute(
                "SELECT * FROM ai_calls ORDER BY id DESC LIMIT ?", (int(limit),)
            )
        )

    # --- кабинет продавца: диагностика и пробный период ---

    def diagnostic(self, seller_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM diagnostics WHERE seller_id = ?", (str(seller_id),)
        ).fetchone()

    def mark_diagnostic(self, seller_id: str, summary: str | None = None) -> None:
        self._conn.execute(
            "INSERT INTO diagnostics (seller_id, summary) VALUES (?, ?)"
            " ON CONFLICT(seller_id) DO UPDATE SET summary=excluded.summary",
            (str(seller_id), summary),
        )

    def trial(self, seller_id: str, module: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM trials WHERE seller_id = ? AND module = ?",
            (str(seller_id), module),
        ).fetchone()

    def trials(self, seller_id: str) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM trials WHERE seller_id = ? ORDER BY started_at",
                (str(seller_id),),
            )
        )

    def start_trial(self, seller_id: str, module: str) -> bool:
        """Отмечает начало пробного периода. False - он уже был."""
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO trials (seller_id, module) VALUES (?, ?)",
            (str(seller_id), module),
        )
        return bool(cursor.rowcount)


def repo(client_id: int, path: str | Path | None = None) -> ClientRepo:
    """Единственный способ добраться до данных клиента."""
    if client_id is None:
        raise ValueError("client_id обязателен: доступа к данным без клиента нет")
    return ClientRepo(connect(path), int(client_id))


def admin_repo(path: str | Path | None = None) -> AdminRepo:
    """Доступ к общим таблицам: клиенты, счётчики, журнал, очередь."""
    return AdminRepo(connect(path))
