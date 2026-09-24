"""База: соединение, миграции, репозитории.

Наружу выставлены ровно три вещи: migrate(path), repo(client_id) и admin_repo().
SQL, схема и режим WAL остаются здесь.

Изоляция клиентов сделана конструкцией, а не дисциплиной: у репозитория клиента
нет метода, который выполняет произвольный SQL, и нет метода без client_id.
Каждый запрос получает "client_id = ?" в условие автоматически.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from core import config

logger = logging.getLogger(__name__)

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
        "card_names",
        "fin_weeks",
        "fin_rows",
        "nm_daily",
        "ad_campaigns",
        "ad_daily",
        "ad_nm_daily",
        "ad_upd",
        "funnel_visibility",
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
# Выдача номера счёта сериализуется здесь, а не у вызывающего: номер обязан
# быть сквозным и без дыр, кто бы его ни попросил.
_number_lock = threading.Lock()


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


def strip_sql_comments(sql: str) -> str:
    """Убирает комментарии, не трогая их подобие внутри строк и кавычек.

    Нужно двоим: миграции, чтобы назвать в журнале оператор, который база
    отвергла, и сторожу идемпотентности, чтобы слово из комментария не сошло
    за оператор. Поиск подстроки тут не годится, поэтому разбор посимвольный.
    """
    out: list[str] = []
    quotes = {"'": "'", '"': '"', "`": "`", "[": "]"}
    total = len(sql)
    i = 0
    while i < total:
        char = sql[i]
        closing = quotes.get(char)
        if closing is not None:
            out.append(char)
            i += 1
            while i < total:
                out.append(sql[i])
                if sql[i] == closing:
                    # Удвоенная кавычка внутри литерала это она сама, не конец.
                    if closing != "]" and sql[i + 1 : i + 2] == closing:
                        out.append(closing)
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if sql.startswith("--", i):
            end = sql.find("\n", i)
            i = total if end < 0 else end
            continue
        if sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = total if end < 0 else end + 2
            out.append(" ")
            continue
        out.append(char)
        i += 1
    return "".join(out)


def split_statements(sql: str) -> list[str]:
    """Режет файл схемы на отдельные операторы.

    Границу оператора определяет сам SQLite (sqlite3.complete_statement): он
    знает и про комментарии, и про строковые литералы, и про CREATE TRIGGER,
    внутри которого точка с запятой оператор не заканчивает. Свой разрез по
    ';' ошибся бы на первом же триггере.
    """
    statements: list[str] = []
    current = ""
    for line in sql.splitlines(keepends=True):
        current += line
        if sqlite3.complete_statement(current):
            statements.append(current.strip())
            current = ""
    tail = current.strip()
    if tail and strip_sql_comments(tail).strip():
        statements.append(tail)
    return statements


def _headline(statement: str) -> str:
    """Оператор одной строкой: так его видно в журнале без всего файла."""
    return " ".join(strip_sql_comments(statement).split())[:120]


SCHEMA_FAILED = (
    "Схема базы применена не полностью. Файл {file}, оператор «{headline}»: "
    "{error}. Чаще всего это значит, что в таблице уже лежат строки, которые "
    "новому правилу противоречат: правило не создано, остальная схема на "
    "месте. Уберите лишние строки и перезапустите бота."
)


def migrate(path: str | Path | None = None) -> int:
    """Приводит базу к схеме из migrations/*.sql. Возвращает версию схемы.

    Файлы применяются при каждом старте, а не один раз. Схема здесь правится
    на месте, одним файлом, и «применили, версию записали, больше не смотрим»
    означало бы, что новый индекс не появится ни на одной базе, где версия уже
    стоит, а кто-то обязан помнить про ручной оператор при деплое. Ручной шаг
    забудут ровно один раз, и это будет важный раз.

    Это безопасно ровно потому, что каждый оператор файла идемпотентен
    (IF NOT EXISTS); за этим следит tests/test_migrations.py. Мерили: 32
    оператора на готовой базе это около 1 мс вместе с чтением файла, на пустой
    первый прогон около 40 мс. На фоне запуска бота этого не видно.

    Оператор, который база отвергла, не останавливает остальные и не роняет
    бота: владелец получает запись в журнале с текстом ошибки. Бот без одного
    индекса лучше, чем молча не стартовавший бот.
    """
    conn = connect(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    for file in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = int(file.name.split("_", 1)[0])
        for statement in split_statements(file.read_text(encoding="utf-8")):
            try:
                conn.execute(statement)
            except sqlite3.Error as error:
                _report_schema_failure(file.name, statement, error, path)
        conn.execute(
            "INSERT OR IGNORE INTO schema_version(version) VALUES (?)", (version,)
        )
    row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()
    return int(row[0])


def _report_schema_failure(
    file_name: str, statement: str, error: Exception, path: str | Path | None
) -> None:
    """Запись владельцу о непринятом операторе. Журнал пишет и в лог тоже."""
    message = SCHEMA_FAILED.format(
        file=file_name, headline=_headline(statement), error=error
    )
    # Импорт здесь, а не наверху: audit стоит на db, наверху вышло бы кольцо.
    try:
        from core import audit

        audit.log("schema", None, message, level="error", path=path)
    except Exception:  # журнал не имеет права остановить запуск
        logger.error(message)


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

    def insert_once(self, table: str, **values: Any) -> int | None:
        """Добавляет строку, если уникальный индекс этого не запрещает.

        Возвращает rowid или None, если такая строка уже есть. Проверка и
        вставка это один оператор: два одновременных вызова не могут оба
        решить, что строки нет, и оба вставить. Сравните с next_invoice_number:
        ручной BEGIN тут невозможен, соединение одно на всех.
        """
        table = self._table(table)
        values = {"client_id": self._client_id, **values}
        columns = [_check_column(name) for name in values]
        placeholders = ", ".join("?" for _ in columns)
        cursor = self._conn.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
            " ON CONFLICT DO NOTHING",
            list(values.values()),
        )
        return int(cursor.lastrowid or 0) if cursor.rowcount else None

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

    def rows_between(
        self,
        table: str,
        column: str,
        first: Any,
        last: Any,
        **conditions: Any,
    ) -> list[sqlite3.Row]:
        """Строки клиента, у которых column лежит между first и last включительно.

        Отдельный метод, а не условие в rows(): равенство здесь не годится, а
        произвольного SQL наружу в этом проекте нет. Отчёт обязан читать только
        свой период: индексы по дате в схеме стоят, но без границ в запросе они
        не работают вовсе, а sqlite3 живёт в одном процессе с ботом, и лишний
        скан это пауза у всех клиентов сразу.
        """
        table = self._table(table)
        where, params = self._where(conditions)
        sql = f"SELECT * FROM {table} WHERE {where} AND {_check_column(column)} BETWEEN ? AND ?"
        params.extend([first, last])
        return list(self._conn.execute(sql, params))

    def first_value(
        self, table: str, column: str, *, not_null: str | None = None, **conditions: Any
    ) -> Any:
        """Наименьшее значение column среди строк клиента. Нет строк - None.

        Сделано через ORDER BY и LIMIT 1, а не через MIN(): с условием
        «в колонке not_null что-то есть» база проходит по индексу и
        останавливается на первой подходящей строке, а MIN() при том же
        условии дочитывает всё до конца.
        """
        table = self._table(table)
        where, params = self._where(conditions)
        column = _check_column(column)
        sql = f"SELECT {column} FROM {table} WHERE {where} AND {column} IS NOT NULL"
        if not_null is not None:
            sql += f" AND {_check_column(not_null)} IS NOT NULL"
        row = self._conn.execute(f"{sql} ORDER BY {column} ASC LIMIT 1", params).fetchone()
        return None if row is None else row[0]

    def delete_before(self, table: str, column: str, value: Any, **conditions: Any) -> int:
        """Удаляет строки клиента, у которых column строго меньше value.

        Нужна сроку хранения суточной истории: без неё таблицы растут
        бесконечно, а глубже самого длинного отчётного периода они никому не
        нужны. Удаление идёт по тем же индексам, что и чтение периода.
        """
        table = self._table(table)
        where, params = self._where(conditions)
        cursor = self._conn.execute(
            f"DELETE FROM {table} WHERE {where} AND {_check_column(column)} < ?",
            params + [value],
        )
        return int(cursor.rowcount)

    def delete_between(self, table: str, column: str, first: Any, last: Any, **conditions: Any) -> int:
        """Удаляет строки клиента, у которых column лежит в границах включительно.

        Нужна сборщику, который переписывает окно целиком: дополнять окно
        строками нельзя там, где ключ строки бот придумывает сам.
        """
        table = self._table(table)
        where, params = self._where(conditions)
        cursor = self._conn.execute(
            f"DELETE FROM {table} WHERE {where} AND {_check_column(column)} BETWEEN ? AND ?",
            params + [first, last],
        )
        return int(cursor.rowcount)

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
    - единственное исключение из этого правила - tokens_with_exp(): она отдаёт
      пары (client_id, exp) сразу по всем клиентам, потому что напоминание о
      протухающем токене иначе делало бы запрос на каждого. Границы исключения
      жёсткие: только внутренний id и срок, шифротокен не отдаётся и добавлять
      его в эту выборку нельзя; нужен сам токен - только через repo(client_id);
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

    def tokens_with_exp(self) -> list[tuple[int, str | None]]:
        """Сроки действия токенов по всем клиентам: пары (client_id, exp).

        Напоминаниям о протухающем токене нужен один проход, а не запрос на
        каждого клиента. Это служебный срез по всем сразу, как /tasks и /stats
        у владельца.

        Сам токен отсюда не выходит: в выборке нет шифротекста, и добавлять его
        сюда нельзя. Кому нужен токен, берёт его через своего клиента.
        """
        rows = self._conn.execute(
            "SELECT client_id, exp FROM wb_tokens ORDER BY client_id"
        )
        return [(int(row["client_id"]), row["exp"]) for row in rows]

    def next_invoice_number(self, year: int) -> str:
        """Следующий номер счёта, сквозной в пределах года и без повторов.

        Соединение одно на файл и общее для потоков (sqlite3 собран с
        threadsafety=3, так что сам по себе это разрешённый режим). Ручной
        BEGIN IMMEDIATE на таком соединении ронял вторую параллельную выдачу
        с «cannot start a transaction within a transaction»: транзакция
        принадлежит соединению, а не вызывающему.

        Поэтому номер выдаётся одним оператором, который увеличивает счётчик
        и сразу возвращает результат, а вызовы сериализуются замком здесь же,
        в слое данных. Вызывающему заводить свой замок не нужно.
        """
        year = int(year)
        with _number_lock:
            row = self._conn.execute(
                "INSERT INTO invoice_seq (year, last_number) VALUES (?, 1)"
                " ON CONFLICT(year) DO UPDATE SET last_number = last_number + 1"
                " RETURNING last_number",
                (year,),
            ).fetchone()
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

    def add_task_once(
        self, kind: str, payload: str = "{}", next_run_at: str | None = None
    ) -> int | None:
        """То же самое, но молча пропускает повтор уже стоящей задачи.

        None означает «такая задача уже в очереди или прямо сейчас идёт».
        Отбор делает уникальный индекс по (клиент, вид, payload) среди
        незавершённых задач, а не запрос перед вставкой: запрос и вставка
        двумя шагами это гонка, хендлеры выполняются одновременно.
        """
        cursor = self._conn.execute(
            "INSERT INTO tasks (client_id, kind, payload, next_run_at)"
            " VALUES (NULL, ?, ?, ?) ON CONFLICT DO NOTHING",
            (kind, payload, next_run_at),
        )
        return int(cursor.lastrowid or 0) if cursor.rowcount else None

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

    def tasks_by_kind(self, kind: str, limit: int | None = None) -> list[sqlite3.Row]:
        """Все задачи одного вида, без среза по последним N.

        Нужно расписанию, чтобы не поставить вторую утреннюю задачу за тот же
        день: срез окна пропускал бы повтор, как только очередь подрастёт.
        Чтение, и только чтение: писать клиентскую строку общий слой не умеет.
        """
        sql = "SELECT * FROM tasks WHERE kind = ? ORDER BY id DESC"
        params: list[Any] = [kind]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return list(self._conn.execute(sql, params))

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

    def start_trial(self, seller_id: str, module: str, limit: int = 1) -> bool:
        """Отмечает начало пробного периода. False - лимит выбран или он уже был.

        Квота «столько модулей на кабинет» проверяется тем же оператором,
        который вставляет строку, а не отдельным чтением перед ним. Правило,
        проверенное отдельно от записи, правилом не является: два
        одновременных нажатия на разные модули оба прочитали бы ноль и оба
        вставили бы строку, и кабинет получил бы два модуля бесплатно.

        Одного оператора здесь достаточно, замок не нужен: SQLite выполняет
        его целиком, и rowcount сам отвечает, дали или нет. Первичный ключ
        (seller_id, module) закрывает только повтор того же модуля, поэтому
        условие по количеству стоит внутри запроса.
        """
        cursor = self._conn.execute(
            "INSERT INTO trials (seller_id, module)"
            " SELECT ?, ? WHERE (SELECT COUNT(*) FROM trials WHERE seller_id = ?) < ?"
            " ON CONFLICT DO NOTHING",
            (str(seller_id), module, str(seller_id), int(limit)),
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
