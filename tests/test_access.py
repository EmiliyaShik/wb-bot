"""Доступ к модулям. Шов один: путь к базе."""

import copy
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from core import access, config, db

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def client_id(db_path):
    return db.admin_repo(db_path).ensure_client(100100)


def test_grant_access_turns_module_on_until_named_date(db_path, client_id):
    granted = access.grant_access(
        client_id, "finance", 30, "WBR-2026-0001", "invoice", "system",
        now=T0, path=db_path,
    )
    assert granted.duplicate is False
    assert granted.state == "active"
    assert granted.until == T0 + timedelta(days=30)
    assert access.has_access(client_id, "finance", now=T0, path=db_path) is True


def test_same_payment_ref_does_not_extend_and_is_logged_as_duplicate(db_path, client_id):
    first = access.grant_access(
        client_id, "finance", 30, "WBR-2026-0001", "invoice", "system",
        now=T0, path=db_path,
    )
    again = access.grant_access(
        client_id, "finance", 30, "WBR-2026-0001", "invoice", "system",
        now=T0 + timedelta(days=10), path=db_path,
    )
    assert again.duplicate is True
    assert again.until == first.until

    actions = [
        row["action"]
        for row in db.repo(client_id, db_path).rows("access_log", module="finance")
    ]
    assert actions == ["grant", "duplicate"]


def test_after_the_term_comes_grace_then_off(db_path, client_id):
    access.grant_access(
        client_id, "rnp", 30, "WBR-2026-0002", "invoice", "system",
        now=T0, path=db_path,
    )
    grace = T0 + timedelta(days=30, hours=1)
    after = T0 + timedelta(days=30 + access.grace_days(), hours=1)

    assert access.access_of(client_id, "rnp", now=grace, path=db_path).state == "grace"
    assert access.has_access(client_id, "rnp", now=grace, path=db_path) is True
    assert access.access_of(client_id, "rnp", now=after, path=db_path).state == "off"
    assert access.has_access(client_id, "rnp", now=after, path=db_path) is False


def test_all_opens_every_visible_module_and_no_hidden_one(
    db_path, client_id, monkeypatch
):
    """Пакет открывает всё видимое и ничего скрытого.

    Скрытых модулей в конфиге сейчас не осталось: открылись все четыре, скрыт
    только сам пакет. Поэтому один модуль прячется здесь, прямо в тесте:
    правило про скрытые должно держаться и в тот день, когда владелец снова
    что-нибудь скроет.
    """
    patched = copy.deepcopy(config.settings())
    patched["modules"]["funnel"]["visible"] = False
    monkeypatch.setattr(config, "settings", lambda: patched)

    access.grant_access(
        client_id, "all", 30, "WBR-2026-0003", "invoice", "system",
        now=T0, path=db_path,
    )
    for name in ("finance", "rnp", "ads"):
        assert access.has_access(client_id, name, now=T0, path=db_path) is True
    assert access.has_access(client_id, "funnel", now=T0, path=db_path) is False


def test_all_opens_the_funnel_too_now_that_it_is_on_sale(db_path, client_id):
    """Модуль открылся и вошёл в пакет сам: список считается из конфига."""
    access.grant_access(
        client_id, "all", 30, "WBR-2026-0033", "invoice", "system",
        now=T0, path=db_path,
    )
    for name in config.visible_modules():
        assert access.has_access(client_id, name, now=T0, path=db_path) is True


def test_pause_does_not_burn_paid_days(db_path, client_id):
    granted = access.grant_access(
        client_id, "finance", 30, "WBR-2026-0004", "invoice", "system",
        now=T0, path=db_path,
    )
    paused_at = T0 + timedelta(days=10)
    resumed_at = T0 + timedelta(days=18)

    access.pause(client_id, now=paused_at, path=db_path)
    during = access.access_of(client_id, "finance", now=T0 + timedelta(days=12), path=db_path)
    assert during.state == "paused"
    assert access.has_access(client_id, "finance", now=T0 + timedelta(days=12), path=db_path) is True

    access.resume(client_id, now=resumed_at, path=db_path)
    after = access.access_of(client_id, "finance", now=resumed_at, path=db_path)
    assert after.state == "active"
    assert after.until == granted.until + timedelta(days=8)


def test_trial_is_once_per_wb_cabinet_not_per_telegram_account(db_path):
    admin = db.admin_repo(db_path)
    first = admin.ensure_client(111111)
    second = admin.ensure_client(222222)
    admin.set_client_fields(first, seller_id="WB-777")
    admin.set_client_fields(second, seller_id="WB-777")

    granted = access.start_trial(first, "finance", now=T0, path=db_path)
    trial_days = config.settings()["trial"]["days"]
    assert granted.until == T0 + timedelta(days=trial_days)

    with pytest.raises(access.TrialDenied) as denied:
        access.start_trial(second, "rnp", now=T0, path=db_path)
    assert denied.value.reason == "used"
    assert access.has_access(second, "rnp", now=T0, path=db_path) is False


def test_status_lists_visible_modules_with_state_and_date(db_path, client_id):
    access.grant_access(
        client_id, "finance", 30, "WBR-2026-0005", "invoice", "system",
        now=T0, path=db_path,
    )
    table = {row.module: row for row in access.status(client_id, now=T0, path=db_path)}
    assert set(table) == set(config.visible_modules())
    assert table["finance"].state == "active"
    assert table["finance"].until == T0 + timedelta(days=30)
    assert table["rnp"].state == "off"
    # Пакет открыт и стоит в таблице наравне с модулями: купленного пакета у
    # этого клиента нет, значит он выключен, а не скрыт.
    assert table["all"].state == "off"


def test_status_shows_a_hidden_module_only_when_asked(db_path, client_id, monkeypatch):
    """Скрытый модуль не купить, поэтому в обычной таблице его нет вовсе.

    Скрытых модулей в конфиге не осталось: владелец открыл и все четыре модуля,
    и пакет. Модуль прячется здесь, прямо в тесте, чтобы правило оставалось
    проверенным и в тот день, когда владелец снова что-нибудь скроет.
    """
    patched = copy.deepcopy(config.settings())
    patched["modules"]["funnel"]["visible"] = False
    monkeypatch.setattr(config, "settings", lambda: patched)

    table = {row.module: row for row in access.status(client_id, now=T0, path=db_path)}
    assert "funnel" not in table
    assert set(table) == set(config.visible_modules())

    with_hidden = {
        row.module: row
        for row in access.status(client_id, now=T0, path=db_path, include_hidden=True)
    }
    assert with_hidden["funnel"].state == "hidden"
    assert set(with_hidden) == set(config.modules())


def test_a_client_with_the_package_sees_every_module_covered_by_it(db_path, client_id):
    """Куплен пакет: в таблице работают все модули, и видно, чем они открыты."""
    access.grant_access(
        client_id, "all", 30, "WBR-2026-0055", "invoice", "system",
        now=T0, path=db_path,
    )
    table = {row.module: row for row in access.status(client_id, now=T0, path=db_path)}
    assert set(table) == set(config.visible_modules())
    for name, row in table.items():
        assert row.works is True, name
        assert row.until == T0 + timedelta(days=30), name
        # У модулей источник называет пакет, у самого пакета - способ оплаты.
        assert row.source == ("invoice" if name == "all" else "all"), name


def test_days_left_is_counted_from_the_moment_the_status_was_built(db_path, client_id):
    access.grant_access(
        client_id, "finance", 30, "WBR-2026-0008", "invoice", "owner",
        now=T0, path=db_path,
    )
    tenth_day = access.access_of(
        client_id, "finance", now=T0 + timedelta(days=10), path=db_path
    )
    assert tenth_day.days_left == 20
    assert tenth_day.as_of == T0 + timedelta(days=10)


def test_revoke_switches_off_logs_once_and_is_safe_to_repeat(db_path, client_id):
    access.grant_access(
        client_id, "finance", 30, "WBR-2026-0009", "invoice", "owner",
        now=T0, path=db_path,
    )
    moment = T0 + timedelta(days=10)
    revoked = access.revoke_access(
        client_id, "finance", "возврат по заявлению", "owner",
        payment_ref="WBR-2026-0009", now=moment, path=db_path,
    )
    assert revoked.state == "off"
    assert access.has_access(client_id, "finance", now=moment, path=db_path) is False

    again = access.revoke_access(
        client_id, "finance", "ещё раз", "owner", now=moment, path=db_path
    )
    assert again.state == "off"

    rows = db.repo(client_id, db_path).rows("access_log", module="finance")
    assert [row["action"] for row in rows] == ["grant", "revoke"]
    assert rows[-1]["actor"] == "owner"
    assert rows[-1]["method"] == "возврат по заявлению"
    assert rows[-1]["payment_ref"] == "WBR-2026-0009"
    # На момент отмены оставалось 20 оплаченных суток из 30.
    assert rows[-1]["days"] == 20


def test_trial_is_refused_for_a_hidden_module(db_path, monkeypatch):
    """callback_data не доверенный канал: запрет живёт в функции, не в клавиатуре.

    Скрытых модулей в конфиге не осталось, владелец открыл все четыре и пакет,
    поэтому модуль прячется здесь же: правило должно держаться и в тот день,
    когда владелец снова что-нибудь скроет.
    """
    patched = copy.deepcopy(config.settings())
    patched["modules"]["funnel"]["visible"] = False
    monkeypatch.setattr(config, "settings", lambda: patched)

    admin = db.admin_repo(db_path)
    client = admin.ensure_client(777777)
    admin.set_client_fields(client, seller_id="WB-555")

    with pytest.raises(access.TrialDenied) as denied:
        access.start_trial(client, "funnel", now=T0, path=db_path)
    assert denied.value.reason == "not_sold"

    # Отказ доказан состоянием: доступа нет и попытка не потрачена.
    assert access.has_access(client, "funnel", now=T0, path=db_path) is False
    assert admin.trials("WB-555") == []
    assert access.start_trial(client, "finance", now=T0, path=db_path).state == "active"


def test_a_package_on_sale_is_never_given_for_a_trial(db_path):
    """Пакет продаётся, но пробно не выдаётся: пробуют по одному модулю.

    Отказ обязан идти по своей причине. Пока пакет был скрыт, до этой проверки
    очередь не доходила: раньше срабатывала видимость, и правило «пакеты пробно
    не даём» держалось случайно. Поэтому первой строкой проверяется, что пакет
    именно продаётся, - иначе тест снова доказывал бы не то.
    """
    assert config.modules()["all"].visible is True
    assert "all" in access.packages()

    admin = db.admin_repo(db_path)
    client = admin.ensure_client(777778)
    admin.set_client_fields(client, seller_id="WB-556")

    with pytest.raises(access.TrialDenied) as denied:
        access.start_trial(client, "all", now=T0, path=db_path)
    assert denied.value.reason == "package"

    # Отказ доказан состоянием: пакета нет, попытка не потрачена, и она
    # по-прежнему доступна для обычного модуля.
    assert access.has_access(client, "all", now=T0, path=db_path) is False
    assert admin.trials("WB-556") == []
    assert access.start_trial(client, "finance", now=T0, path=db_path).state == "active"


def test_the_one_trial_taken_by_another_process_leaves_nothing_to_take(db_path):
    """Квота «один модуль» живёт в базе, а не в порядке чтений.

    Проверяем отдельным соединением с тем же файлом: это второй процесс бота,
    и threading.RLock он ни с кем не разделяет. Значит, отказ может дать только
    сама база - тем же оператором, которым и пишет.

    Настоящие потоки здесь не запускаются намеренно: бот однопоточный, а общее
    соединение sqlite одновременного обращения из двух потоков не переживает.
    Тест, который падает раз через раз, хуже отсутствующего.
    """
    admin = db.admin_repo(db_path)
    client = admin.ensure_client(888888)
    admin.set_client_fields(client, seller_id="WB-RACE")

    other_process = sqlite3.connect(db_path, isolation_level=None)
    try:
        taken = other_process.execute(
            "INSERT INTO trials (seller_id, module) SELECT 'WB-RACE', 'finance'"
            " WHERE (SELECT COUNT(*) FROM trials WHERE seller_id = 'WB-RACE') < 1"
        ).rowcount
    finally:
        other_process.close()
    assert taken == 1, "первая проба не записалась, дальше проверять нечего"

    with pytest.raises(access.TrialDenied) as denied:
        access.start_trial(client, "rnp", now=T0, path=db_path)

    assert denied.value.reason == "used"
    assert len(admin.trials("WB-RACE")) == 1, "бесплатно ушло больше одного модуля"
    assert access.has_access(client, "rnp", now=T0, path=db_path) is False


def test_the_base_itself_refuses_the_second_trial(db_path):
    """Тот же оператор, выполненный дважды, второй раз не пишет ничего."""
    admin = db.admin_repo(db_path)
    client = admin.ensure_client(888889)
    admin.set_client_fields(client, seller_id="WB-QUOTA")

    assert admin.start_trial("WB-QUOTA", "finance") is True
    assert admin.start_trial("WB-QUOTA", "rnp") is False
    assert admin.start_trial("WB-QUOTA", "finance") is False
    assert len(admin.trials("WB-QUOTA")) == 1


def _run_together(*calls) -> None:
    """Запускает вызовы одновременно: барьер отпускает потоки в одной точке."""
    ready = threading.Barrier(len(calls))

    def wrapped(call):
        def run():
            ready.wait()
            call()

        return run

    threads = [threading.Thread(target=wrapped(call)) for call in calls]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


def test_one_payment_paid_twice_at_once_extends_once(db_path, client_id):
    def pay():
        access.grant_access(
            client_id, "finance", 30, "WBR-2026-0010", "invoice", "system",
            now=T0, path=db_path,
        )

    _run_together(pay, pay, pay, pay)

    rows = db.repo(client_id, db_path).rows("access_log", module="finance")
    assert [row["action"] for row in rows].count("grant") == 1
    current = access.access_of(client_id, "finance", now=T0, path=db_path)
    assert current.until == T0 + timedelta(days=30)


def test_the_rule_against_a_twice_paid_invoice_lives_in_the_database(db_path, client_id):
    """Замок в core.access это верхняя граница, база нижняя.

    Второй процесс бота на той же базе замка не видит, поэтому вторая строка
    выдачи по тому же платежу должна быть невозможна и без него.
    """
    access.grant_access(
        client_id, "finance", 30, "WBR-2026-0012", "invoice", "system",
        now=T0, path=db_path,
    )
    repo = db.repo(client_id, db_path)
    row = dict(
        module="finance", action="grant", days=30,
        payment_ref="WBR-2026-0012", method="invoice", actor="system",
    )
    with pytest.raises(sqlite3.IntegrityError):
        repo.insert("access_log", **row)

    # Правило в пределах клиента, как и проверка в grant_access: чужой клиент
    # с тем же номером ни при чём.
    other = db.admin_repo(db_path).ensure_client(100200)
    db.repo(other, db_path).insert("access_log", **row)

    # Запрещена только выдача. Дубли, паузы и возвраты по тому же платежу
    # пишутся сколько угодно раз, иначе журнал перестал бы быть журналом.
    for action in ("duplicate", "duplicate", "revoke"):
        repo.insert("access_log", **{**row, "action": action})


def test_payment_claimed_by_another_process_comes_back_as_a_duplicate(db_path, client_id):
    """Выдачу занял кто-то мимо замка: наружу дубль, а не исключение базы.

    Второй процесс бота это отдельное соединение с тем же файлом: своего
    threading.RLock он не разделяет ни с кем. Здесь он успевает записать
    выдачу первым.
    """
    other_process = sqlite3.connect(db_path, isolation_level=None)
    try:
        other_process.execute(
            "INSERT INTO access_log (client_id, module, action, days, payment_ref,"
            " method, actor) VALUES (?, 'finance', 'grant', 30, 'WBR-2026-0013',"
            " 'invoice', 'system')",
            (client_id,),
        )
    finally:
        other_process.close()

    granted = access.grant_access(
        client_id, "finance", 30, "WBR-2026-0013", "invoice", "system",
        now=T0, path=db_path,
    )

    assert granted.duplicate is True
    actions = [
        row["action"]
        for row in db.repo(client_id, db_path).rows("access_log", module="finance")
    ]
    assert sorted(actions) == ["duplicate", "grant"]
    # Второй процесс выдал доступ по-своему, наша сторона его не продлевала.
    assert access.access_of(client_id, "finance", now=T0, path=db_path).state == "off"


def test_two_simultaneous_revokes_write_one_line(db_path, client_id):
    access.grant_access(
        client_id, "rnp", 30, "WBR-2026-0011", "invoice", "owner",
        now=T0, path=db_path,
    )

    def cancel():
        access.revoke_access(client_id, "rnp", "возврат", "owner", now=T0, path=db_path)

    _run_together(cancel, cancel, cancel, cancel)

    rows = db.repo(client_id, db_path).rows("access_log", module="rnp")
    assert [row["action"] for row in rows].count("revoke") == 1
    assert access.has_access(client_id, "rnp", now=T0, path=db_path) is False
