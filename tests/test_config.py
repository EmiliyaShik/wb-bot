"""Конфиг: цены, скидки, секции. Ожидаемые значения взяты из спецификации."""

from decimal import Decimal
from pathlib import Path

import pytest

from core import config


def test_settings_has_all_sections():
    s = config.settings()
    for section in ("modules", "periods", "trial", "access", "alerts", "schedule", "invoice"):
        assert section in s, f"в config.toml нет секции {section}"


def test_modules_from_spec():
    mods = config.modules()
    assert mods["finance"].price_month == 990
    assert mods["rnp"].price_month == 590
    assert mods["ads"].price_month == 1290
    assert mods["funnel"].price_month == 490
    assert mods["all"].price_month == 2290
    assert mods["finance"].visible is True
    assert mods["ads"].visible is False
    assert mods["funnel"].visible is False
    assert mods["finance"].agents == ("finance", "watchdog", "profit")
    assert mods["all"].includes == "*"


def test_price_applies_discounts_from_config():
    # 990 за месяц; 3 месяца это -10 %, 12 месяцев это -20 %
    assert config.price("finance", 1) == 990
    assert config.price("finance", 3) == 2673
    assert config.price("finance", 12) == 9504
    assert config.price("all", 12) == 21984
    assert config.price("rnp", 3) == 1593


def test_price_unknown_module_is_error():
    with pytest.raises(KeyError):
        config.price("нет-такого", 1)


def test_thresholds_and_periods_from_spec():
    s = config.settings()
    assert s["trial"]["days"] == 7
    assert s["access"]["grace_days"] == 3
    assert s["access"]["retention_days"] == 30
    assert s["alerts"]["baseline_weeks"] == 4
    assert s["alerts"]["spp"] == -5.0
    assert s["schedule"]["timezone"] == "Europe/Moscow"
    assert s["invoice"]["valid_bank_days"] == 5
    assert s["invoice"]["vat_note"] == ""


def test_admin_ids_empty_when_env_unset(monkeypatch):
    monkeypatch.delenv("ADMIN_TELEGRAM_IDS", raising=False)
    assert config.admin_ids() == ()
    assert config.is_admin(1) is False


def test_admin_ids_parsed_from_env(monkeypatch):
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "111, 222")
    assert config.admin_ids() == (111, 222)
    assert config.is_admin(222) is True
    assert config.is_admin(333) is False


def test_db_path_follows_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert config.db_path() == tmp_path / "wbrentgen.db"


def test_price_is_counted_in_decimal():
    # 990 за месяц, три месяца со скидкой 10 %: 2970 минус 297
    assert config.price_decimal("finance", 3) == Decimal("2673")
    # 2290 за месяц, год со скидкой 20 %: 27480 минус 5496
    assert config.price_decimal("all", 12) == Decimal("21984")
    assert isinstance(config.price_decimal("finance", 3), Decimal)
    assert isinstance(config.price("finance", 3), int)


def test_price_rounds_half_up_not_to_even(monkeypatch):
    # 990 x 3 = 2970, скидка 15 % даёт 2524.5: по правилу «половина вверх» это
    # 2525. Встроенный round() округлил бы к чётному и вернул 2524.
    monkeypatch.setattr(config, "discount_percent", lambda months: 15)
    assert config.price("finance", 3) == 2525
    assert config.price_decimal("finance", 3) == Decimal("2525")


def test_data_dir_is_local_when_env_is_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", "")
    # /app на машине разработчика нет
    monkeypatch.setattr(config, "HOST_DATA_DIR", tmp_path / "нет-такой" / "data")
    assert config.data_dir() == config.LOCAL_DATA_DIR
    assert config.LOCAL_DATA_DIR == config.ROOT / "data"
    assert config.db_path() == config.ROOT / "data" / "wbrentgen.db"


def test_data_dir_is_host_folder_when_app_exists(monkeypatch, tmp_path):
    monkeypatch.delenv("DATA_DIR", raising=False)
    app = tmp_path / "app"
    app.mkdir()
    monkeypatch.setattr(config, "HOST_DATA_DIR", app / "data")
    assert config.data_dir() == app / "data"


def test_explicit_data_dir_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "своя"))
    assert config.data_dir() == tmp_path / "своя"


def test_token_categories_follow_the_spec():
    categories = config.settings()["token_categories"]
    assert set(categories) == {
        "statistics",
        "finance",
        "analytics",
        "promotion",
        "content",
    }
    # Статистику бот просит на будущее, сегодня она ничего не ломает
    assert categories["statistics"]["breaks"] == []
    assert "на будущее" in categories["statistics"]["note"]
    assert categories["finance"]["breaks"] == ["finance"]
    assert categories["finance"]["breaks_diagnostic"] is True
    assert categories["analytics"]["breaks"] == ["rnp"]
    # без продвижения и без контента модули живут, теряются отдельные числа
    assert categories["promotion"]["breaks"] == []
    assert categories["content"]["breaks"] == []
    assert "реклам" in categories["promotion"]["note"].lower()
    assert "себестоимост" in categories["content"]["note"].lower()
