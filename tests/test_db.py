"""База: схема, миграции, деньги в копейках и изоляция клиентов."""

import sqlite3
from decimal import Decimal

import pytest

from core import db

# Перечень из раздела «Хранилище» спецификации плюс ai_calls: расход нейросети
# требует журнал (R144), а новых миграций другим таскам писать нельзя.
SPEC_TABLES = [
    "clients",
    "consents",
    "wb_tokens",
    "module_access",
    "access_log",
    "invoices",
    "invoice_seq",
    "costs",
    "fin_weeks",
    "fin_rows",
    "nm_daily",
    "plans",
    "diagnostics",
    "trials",
    "tasks",
    "api_calls",
    "ai_calls",
    "events",
]

# Деловые данные клиента: их не должен возвращать ни общий слой, ни чужой клиент.
CLIENT_DATA_TABLES = [
    "wb_tokens",
    "fin_rows",
    "nm_daily",
    "costs",
    "invoices",
    "module_access",
]


def _tables(path):
    conn = sqlite3.connect(path)
    try:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def test_migrate_creates_every_table_from_spec(tmp_path):
    path = tmp_path / "wbrentgen.db"
    db.migrate(path)
    db.close_all()
    tables = _tables(path)
    for name in SPEC_TABLES + ["schema_version"]:
        assert name in tables, f"миграция не создала таблицу {name}"


def test_migrate_is_repeatable_and_keeps_data(tmp_path):
    path = tmp_path / "wbrentgen.db"
    first = db.migrate(path)
    client_id = db.admin_repo(path).ensure_client(telegram_id=555)
    db.repo(client_id, path).insert("costs", nm_id=1, cost_per_unit_kop=1000)
    second = db.migrate(path)
    assert first == second >= 1
    assert len(db.repo(client_id, path).rows("costs")) == 1
    db.close_all()


def test_wal_and_foreign_keys_are_on(db_path):
    conn = db.connect(db_path)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


# --- деньги ---


# REAL в схеме разрешён только процентам, долям и коэффициентам: это не деньги.
ALLOWED_REAL_COLUMNS = {
    "sale_percent",
    "commission_percent",
    "acquiring_percent",
    "ppvz_spp_prc",
    "ppvz_kvw_prc_base",
    "ppvz_kvw_prc",
    "cart_to_order_pct",
    "buyout_pct",
    # Пятый этап воронки. Видимость это вероятность в процентах, её динамика
    # тоже процент, а средняя позиция в поиске это порядковый номер с долями.
    # Денег среди них нет ни одного.
    "visibility",
    "dynamics",
    "avg_position",
}


def test_no_money_column_is_real(db_path):
    """Любая REAL-колонка обязана быть процентом или долей, остальное целое."""
    conn = db.connect(db_path)
    tables = [row["name"] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )]
    offenders = []
    for table in tables:
        for row in conn.execute(f"PRAGMA table_info({table})"):
            name, kind = row["name"], row["type"].upper()
            if kind == "REAL" and name not in ALLOWED_REAL_COLUMNS:
                offenders.append(f"{table}.{name}")
            if name.endswith("_kop"):
                assert kind == "INTEGER", f"{table}.{name} должно быть целым, а не {kind}"
    assert not offenders, "деньги нельзя хранить в REAL: " + ", ".join(offenders)
    # белый список не должен зарасти: каждая запись в нём реально существует
    existing = {
        row["name"]
        for table in tables
        for row in conn.execute(f"PRAGMA table_info({table})")
    }
    assert ALLOWED_REAL_COLUMNS <= existing


def test_kopecks_survive_the_roundtrip(db_path):
    client_id = db.admin_repo(db_path).ensure_client(telegram_id=9)
    repo = db.repo(client_id, db_path)
    repo.insert("costs", nm_id=1, cost_per_unit_kop=db.to_kop(Decimal("123.45")))
    row = repo.one("costs", nm_id=1)
    assert row["cost_per_unit_kop"] == 12345
    assert db.from_kop(row["cost_per_unit_kop"]) == Decimal("123.45")


def test_sum_of_kopecks_is_exact(db_path):
    client_id = db.admin_repo(db_path).ensure_client(telegram_id=10)
    repo = db.repo(client_id, db_path)
    for nm_id, rub in ((1, "0.10"), (2, "0.20"), (3, "0.30")):
        repo.insert("costs", nm_id=nm_id, cost_per_unit_kop=db.to_kop(Decimal(rub)))
    total = sum(row["cost_per_unit_kop"] for row in repo.rows("costs"))
    # у float здесь получилось бы 0.6000000000000001
    assert db.from_kop(total) == Decimal("0.60")


def test_to_kop_rounds_half_up():
    assert db.to_kop(Decimal("0.005")) == 1
    assert db.to_kop(Decimal("10")) == 1000
    assert db.to_kop("2.999") == 300


# --- изоляция ---


def test_client_never_sees_rows_of_another(db_path):
    admin = db.admin_repo(db_path)
    first = admin.ensure_client(telegram_id=1001)
    second = admin.ensure_client(telegram_id=1002)
    assert first != second

    db.repo(first, db_path).insert("costs", nm_id=111, cost_per_unit_kop=10000)
    db.repo(first, db_path).insert("nm_daily", date="2026-09-01", nm_id=111, orders=3)
    db.repo(second, db_path).insert("costs", nm_id=222, cost_per_unit_kop=20000)

    mine = db.repo(first, db_path).rows("costs")
    assert [row["nm_id"] for row in mine] == [111]
    assert db.repo(second, db_path).rows("nm_daily") == []
    # прямое обращение к чужой строке по её ключу тоже ничего не даёт
    assert db.repo(second, db_path).one("costs", nm_id=111) is None
    assert db.repo(second, db_path).count("costs") == 1


def test_client_cannot_update_or_delete_foreign_rows(db_path):
    admin = db.admin_repo(db_path)
    first = admin.ensure_client(telegram_id=2001)
    second = admin.ensure_client(telegram_id=2002)
    db.repo(first, db_path).insert("costs", nm_id=777, cost_per_unit_kop=5000)

    assert db.repo(second, db_path).update("costs", {"nm_id": 777}, cost_per_unit_kop=100) == 0
    assert db.repo(second, db_path).delete("costs", nm_id=777) == 0
    assert db.repo(first, db_path).one("costs", nm_id=777)["cost_per_unit_kop"] == 5000


def test_repo_refuses_tables_without_client_id(db_path):
    repo = db.repo(1, db_path)
    with pytest.raises(ValueError):
        repo.rows("invoice_seq")
    with pytest.raises(ValueError):
        repo.rows("sqlite_master")
    # Чтение и чистка по диапазону живут по тому же правилу.
    with pytest.raises(ValueError):
        repo.rows_between("invoice_seq", "year", 2020, 2030)
    with pytest.raises(ValueError):
        repo.delete_before("invoice_seq", "year", 2030)
    with pytest.raises(ValueError):
        repo.first_value("sqlite_master", "name")
    # Имя колонки тоже не подставляется как попало.
    with pytest.raises(ValueError):
        repo.rows_between("nm_daily", "date; drop table nm_daily", "a", "b")


def test_a_range_reads_and_cleans_only_its_own_client(db_path):
    """Границы периода не отменяют изоляцию: чужие сутки не видны и не стёрты."""
    admin = db.admin_repo(db_path)
    first = admin.ensure_client(telegram_id=2101)
    second = admin.ensure_client(telegram_id=2102)
    for client_id, stamp in ((first, "2026-01-10"), (first, "2026-02-10"), (second, "2026-01-10")):
        db.repo(client_id, db_path).upsert(
            "nm_daily", {"date": stamp, "nm_id": 111}, orders=1, raw="{}"
        )

    mine = db.repo(first, db_path)
    found = mine.rows_between("nm_daily", "date", "2026-01-01", "2026-01-31")
    assert [str(row["date"]) for row in found] == ["2026-01-10"]
    assert mine.first_value("nm_daily", "date", not_null="raw") == "2026-01-10"

    assert mine.delete_before("nm_daily", "date", "2026-02-01") == 1
    assert mine.count("nm_daily") == 1
    # У соседа всё на месте: чистка идёт только по своим строкам.
    assert db.repo(second, db_path).count("nm_daily") == 1


def test_first_value_skips_rows_where_the_marked_column_is_empty(db_path):
    """«Сутки собраны» это непустая колонка raw, и наименьшая дата ищется по ней."""
    client_id = db.admin_repo(db_path).ensure_client(telegram_id=2103)
    repo = db.repo(client_id, db_path)
    repo.upsert("nm_daily", {"date": "2026-01-01", "nm_id": 111}, stocks_wb=5)
    repo.upsert("nm_daily", {"date": "2026-01-05", "nm_id": 111}, raw="{}")

    assert repo.first_value("nm_daily", "date") == "2026-01-01"
    assert repo.first_value("nm_daily", "date", not_null="raw") == "2026-01-05"
    assert repo.first_value("nm_daily", "date", not_null="raw", nm_id=222) is None


def test_every_repo_method_takes_client_id_by_construction(db_path):
    repo = db.repo(42, db_path)
    assert repo.client_id == 42
    # у репозитория нет способа выполнить произвольный SQL
    assert not hasattr(repo, "execute")
    assert not hasattr(repo, "query")


def test_admin_repo_has_no_open_sql(db_path):
    admin = db.admin_repo(db_path)
    assert not hasattr(admin, "query")
    assert not hasattr(admin, "execute")
    public = {name for name in dir(admin) if not name.startswith("_")}
    allowed = {
        "ensure_client",
        "client",
        "client_by_telegram",
        "all_clients",
        "set_client_fields",
        "delete_client",
        "next_invoice_number",
        "tokens_with_exp",
        "add_event",
        "events",
        "add_task",
        "add_task_once",
        "task",
        "tasks",
        "tasks_by_kind",
        "due_tasks",
        "update_task",
        "add_api_call",
        "api_calls",
        "add_ai_call",
        "ai_calls",
        "diagnostic",
        "mark_diagnostic",
        "trial",
        "trials",
        "start_trial",
    }
    assert public <= allowed, f"в общем слое появились новые методы: {public - allowed}"


def test_business_rows_of_a_client_are_returned_by_nobody_else(db_path):
    """Недоступность доказана данными: деловую строку клиента не отдаёт ни
    общий слой, ни репозиторий другого клиента.

    Обход разбирает и строки базы, и кортежи: метод, который отдаёт пару вместо
    строки, обязан попадать под ту же проверку, иначе она не поймает как раз то,
    что добавят позже.
    """
    admin = db.admin_repo(db_path)
    mine = admin.ensure_client(telegram_id=4004)
    other = admin.ensure_client(telegram_id=4005)

    # приметные значения: если хоть одно всплывёт в общем слое, это утечка
    secret = b"shifrotekst-4004"
    forbidden = {secret, 424242, 99000, "WBR-2026-9999", "srid-4004", 777000}
    exp = "2026-11-30"  # срок токена отдавать разрешено, это исключение

    repo = db.repo(mine, db_path)
    repo.insert("costs", nm_id=424242, cost_per_unit_kop=100)
    repo.insert("wb_tokens", ciphertext=secret, scopes="finance", exp=exp)
    repo.insert("nm_daily", date="2026-09-01", nm_id=424242, orders=1)
    repo.insert("fin_rows", report_id=1, rrd_id=424242, srid="srid-4004")
    repo.insert("fin_weeks", report_id=1, date_from="2026-09-01", date_to="2026-09-07")
    repo.insert("plans", year_month="2026-09", revenue_target_kop=777000)
    repo.insert("invoices", number="WBR-2026-9999", module="finance",
                period_months=1, amount_kop=99000)
    repo.insert("module_access", module="finance", state="active")
    repo.insert("access_log", module="finance", action="grant")
    repo.insert("consents", offer_url="https://example.test/offer")

    for table in CLIENT_DATA_TABLES:
        assert db.repo(other, db_path).rows(table) == [], f"чужой клиент видит {table}"

    seen: dict[str, set] = {}
    writers = ("add_", "set_", "delete_", "update_", "mark_", "start_", "ensure_", "next_")
    for name in dir(admin):
        if name.startswith("_") or name.startswith(writers):
            continue  # пишущие методы не зовём: тест не должен сам менять данные
        method = getattr(admin, name)
        if not callable(method):
            continue
        for args in ((), (mine,), ("seller-1",), ("weekly_check",)):
            try:
                result = method(*args)
            except Exception:  # noqa: BLE001 - метод просто не про эти аргументы
                continue
            seen.setdefault(name, set()).update(_values(result))

    leaks = {name: values & forbidden for name, values in seen.items() if values & forbidden}
    assert not leaks, f"общий слой отдал деловые данные клиента: {leaks}"

    # исключение из правила изоляции, названное в докстринге AdminRepo:
    # срок токена отдаётся по всем клиентам, сам токен нет
    assert exp in seen["tokens_with_exp"], "обход не заглянул внутрь кортежей"
    assert mine in seen["tokens_with_exp"]
    assert secret not in seen["tokens_with_exp"]


def _values(result) -> set:
    """Все скалярные значения результата: и строки базы, и кортежи, и списки."""
    found: set = set()
    stack = [result]
    while stack:
        item = stack.pop()
        if item is None:
            continue
        keys = getattr(item, "keys", None)
        if callable(keys):  # строка базы
            stack.extend(item[key] for key in item.keys())
            continue
        if isinstance(item, (list, tuple, set)):
            stack.extend(item)
            continue
        try:
            found.add(item)
        except TypeError:  # что-то нехешируемое, для проверки бесполезно
            found.add(str(item))
    return found


def test_deleting_client_removes_all_his_rows(db_path):
    admin = db.admin_repo(db_path)
    client_id = admin.ensure_client(telegram_id=3003)
    db.repo(client_id, db_path).insert("costs", nm_id=9, cost_per_unit_kop=100)
    admin.delete_client(client_id)
    assert admin.client(client_id) is None
    assert db.repo(client_id, db_path).count("costs") == 0


# --- общий слой: очередь, вызовы, кабинет ---


def test_invoice_numbers_are_sequential_within_year(db_path):
    admin = db.admin_repo(db_path)
    assert admin.next_invoice_number(2026) == "WBR-2026-0001"
    assert admin.next_invoice_number(2026) == "WBR-2026-0002"
    assert admin.next_invoice_number(2027) == "WBR-2027-0001"


def test_common_layer_writes_only_rows_without_client(db_path):
    """add_* общего слоя не принимают client_id и пишут строку без клиента."""
    import inspect

    admin = db.admin_repo(db_path)
    for name in ("add_event", "add_task", "add_task_once", "add_api_call", "add_ai_call"):
        params = inspect.signature(getattr(admin, name)).parameters
        assert "client_id" not in params, f"{name} снова принимает client_id"

    client_id = admin.ensure_client(telegram_id=5005)
    admin.add_task("housekeeping")
    admin.add_event("system", "бот запущен")
    admin.add_api_call(host="finance-api", method="/ping", status=200)
    admin.add_ai_call(provider="test", model="test")
    assert all(row["client_id"] is None for row in admin.tasks())
    assert all(row["client_id"] is None for row in admin.events())
    assert all(row["client_id"] is None for row in admin.api_calls())
    assert all(row["client_id"] is None for row in admin.ai_calls())

    # задача клиента ставится только через его репозиторий
    db.repo(client_id, db_path).insert("tasks", kind="weekly_finance")
    other = admin.ensure_client(telegram_id=5006)
    assert db.repo(other, db_path).rows("tasks") == []
    assert [row["kind"] for row in db.repo(client_id, db_path).rows("tasks")] == ["weekly_finance"]


def test_tasks_are_reachable_by_named_methods(db_path):
    admin = db.admin_repo(db_path)
    task_id = admin.add_task("weekly_finance", payload="{}", next_run_at="2026-09-01 09:00:00")
    assert admin.task(task_id)["state"] == "queued"
    assert [row["id"] for row in admin.due_tasks("2026-09-02 00:00:00")] == [task_id]
    assert admin.due_tasks("2026-08-01 00:00:00") == []
    admin.update_task(task_id, state="failed", attempts=3, last_error="таймаут")
    assert [row["id"] for row in admin.tasks(state="failed")] == [task_id]


def test_ai_cost_has_a_place_in_the_schema(db_path):
    admin = db.admin_repo(db_path)
    admin.add_ai_call(
        provider="openai",
        model="test-model",
        kind="diagnostic",
        prompt_tokens=100,
        completion_tokens=20,
        cost_kop=db.to_kop(Decimal("1.50")),
    )
    row = admin.ai_calls()[0]
    assert row["cost_kop"] == 150
    assert db.from_kop(row["cost_kop"]) == Decimal("1.50")


def test_api_calls_are_recorded(db_path):
    admin = db.admin_repo(db_path)
    admin.add_api_call(host="finance-api", method="/ping", status=200, duration_ms=120)
    assert admin.api_calls()[0]["host"] == "finance-api"


def test_trial_and_diagnostic_are_tied_to_seller(db_path):
    admin = db.admin_repo(db_path)
    assert admin.start_trial("seller-1", "finance") is True
    assert admin.start_trial("seller-1", "finance") is False
    assert admin.trial("seller-1", "finance") is not None
    assert admin.diagnostic("seller-1") is None
    admin.mark_diagnostic("seller-1", summary="три утечки")
    assert admin.diagnostic("seller-1")["summary"] == "три утечки"


def test_tasks_by_kind_does_not_depend_on_table_size(db_path):
    """Расписание отсекает повтор по виду задачи, поэтому выборка обязана
    видеть всю таблицу, а не последние N строк."""
    admin = db.admin_repo(db_path)
    morning = admin.add_task("daily_report")
    weekly = admin.add_task("weekly_check")
    # Шум, за которым срез окна потерял бы утреннюю задачу. Номер в payload
    # не для красоты: одинаковые незавершённые задачи база больше не хранит,
    # и пятьдесят близнецов превратились бы в одну строку.
    for number in range(50):
        admin.add_task("housekeeping", f'{{"n": {number}}}')

    daily = admin.tasks_by_kind("daily_report")
    assert [row["id"] for row in daily] == [morning]
    assert {row["kind"] for row in daily} == {"daily_report"}
    assert [row["id"] for row in admin.tasks_by_kind("weekly_check")] == [weekly]
    assert admin.tasks_by_kind("нет-такого-вида") == []
    assert len(admin.tasks_by_kind("housekeeping")) == 50
    assert len(admin.tasks_by_kind("housekeeping", limit=5)) == 5


def test_invoice_numbers_survive_parallel_calls(db_path):
    """Соединение одно на весь процесс и общее для потоков, поэтому ручной
    BEGIN на нём ронял вторую параллельную выдачу. Сериализация выдачи живёт
    в слое данных, а не у вызывающего."""
    from concurrent.futures import ThreadPoolExecutor

    admin = db.admin_repo(db_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: admin.next_invoice_number(2026), range(8)))

    assert len(set(results)) == 8, f"номера повторились: {results}"
    numbers = sorted(int(value.rsplit("-", 1)[1]) for value in results)
    assert numbers == list(range(1, 9)), f"в номерах дыра: {numbers}"
    assert all(value.startswith("WBR-2026-") for value in results)
    # счётчик после гонки в согласованном состоянии
    assert admin.next_invoice_number(2026) == "WBR-2026-0009"


def test_parallel_calls_for_different_years_do_not_mix(db_path):
    from concurrent.futures import ThreadPoolExecutor

    admin = db.admin_repo(db_path)
    years = [2026, 2027] * 4
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(admin.next_invoice_number, years))

    for year in (2026, 2027):
        own = sorted(
            int(value.rsplit("-", 1)[1])
            for value in results
            if value.startswith(f"WBR-{year}-")
        )
        assert own == [1, 2, 3, 4], f"{year}: {own}"


def test_token_expiry_is_read_for_everyone_at_once(db_path):
    """Напоминаниям о сроке токена нужен один проход по всем клиентам,
    и сам токен наружу при этом не выходит."""
    admin = db.admin_repo(db_path)
    secret = b"shifrotekst-kotoryy-nelzya-otdavat"
    expected = []
    for telegram_id, exp in ((6001, "2026-10-01"), (6002, "2026-12-31"), (6003, None)):
        client_id = admin.ensure_client(telegram_id=telegram_id)
        db.repo(client_id, db_path).insert("wb_tokens", ciphertext=secret, exp=exp)
        expected.append((client_id, exp))
    admin.ensure_client(telegram_id=6004)  # клиент без токена

    result = admin.tokens_with_exp()
    assert result == expected, result
    assert all(len(pair) == 2 for pair in result)
    assert not any(secret in str(pair).encode() for pair in result)
    assert not any("shifrotekst" in str(pair) for pair in result)


def test_token_expiry_is_empty_without_tokens(db_path):
    admin = db.admin_repo(db_path)
    admin.ensure_client(telegram_id=6100)
    assert admin.tokens_with_exp() == []
