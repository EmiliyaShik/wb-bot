"""Доступ к модулям. Шов один: путь к базе."""

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


def test_all_opens_every_visible_module_and_no_hidden_one(db_path, client_id):
    access.grant_access(
        client_id, "all", 30, "WBR-2026-0003", "invoice", "system",
        now=T0, path=db_path,
    )
    for name in ("finance", "rnp"):
        assert access.has_access(client_id, name, now=T0, path=db_path) is True
    assert access.has_access(client_id, "ads", now=T0, path=db_path) is False


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

    with_hidden = {
        row.module: row
        for row in access.status(client_id, now=T0, path=db_path, include_hidden=True)
    }
    assert with_hidden["ads"].state == "hidden"


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
