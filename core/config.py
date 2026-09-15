"""Конфиг: разбор config.toml и переменных окружения.

Наружу выставлены settings(), modules(), price(module, months) плюс несколько
мелочей про окружение (папка данных, ID владельцев). Всё остальное - подробности
разбора, за эту границу они не выходят.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.toml"

DB_FILENAME = "wbrentgen.db"
# На хостинге папка данных задаётся переменной DATA_DIR=/app/data. Локально её
# нет, и база должна лечь рядом с проектом, а не в несуществующий /app.
HOST_DATA_DIR = Path("/app/data")
LOCAL_DATA_DIR = ROOT / "data"


@dataclass(frozen=True)
class ModuleInfo:
    """Один модуль из секции [modules.*] конфига."""

    name: str
    price_month: int
    visible: bool
    title: str = ""
    agents: tuple[str, ...] = field(default_factory=tuple)
    includes: str | None = None


@lru_cache(maxsize=1)
def settings() -> dict[str, Any]:
    """Весь конфиг как есть. Читается один раз за процесс."""
    with open(CONFIG_PATH, "rb") as fh:
        return tomllib.load(fh)


@lru_cache(maxsize=1)
def modules() -> dict[str, ModuleInfo]:
    """Модули из конфига, включая скрытые (у них visible = false)."""
    result: dict[str, ModuleInfo] = {}
    for name, raw in settings()["modules"].items():
        result[name] = ModuleInfo(
            name=name,
            price_month=int(raw["price_month"]),
            visible=bool(raw.get("visible", False)),
            title=str(raw.get("title", name)),
            agents=tuple(raw.get("agents", ())),
            includes=raw.get("includes"),
        )
    return result


def visible_modules() -> dict[str, ModuleInfo]:
    """Модули, которые показываем клиенту."""
    return {name: info for name, info in modules().items() if info.visible}


def discount_percent(months: int) -> int:
    """Скидка за период в процентах. Неизвестный период идёт без скидки."""
    return int(settings()["periods"].get(f"months_{int(months)}", 0))


def price(module: str, months: int) -> int:
    """Цена модуля за период в рублях, со скидкой из секции [periods].

    Деньги считаются в Decimal и округляются до рубля по правилу «половина
    вверх». Float здесь не используется: одна и та же цена должна получаться
    одинаковой и в счёте, и в тексте бота, и в отчёте владельцу.
    """
    info = modules().get(module)
    if info is None:
        raise KeyError(f"нет такого модуля: {module}")
    months = int(months)
    if months < 1:
        raise ValueError("период меньше месяца не бывает")
    full = Decimal(info.price_month) * months
    percent = Decimal(discount_percent(months))
    amount = full * (Decimal(100) - percent) / Decimal(100)
    return int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def price_decimal(module: str, months: int) -> Decimal:
    """Та же цена, но объектом Decimal: для счёта и любых денежных расчётов."""
    return Decimal(price(module, months))


def data_dir() -> Path:
    """Папка данных.

    DATA_DIR задан - берём его. Не задан: на хостинге есть /app, туда и пишем,
    а локально база ложится в ./data рядом с проектом.
    """
    raw = (os.getenv("DATA_DIR") or "").strip()
    if raw:
        return Path(raw)
    if HOST_DATA_DIR.parent.exists():
        return HOST_DATA_DIR
    return LOCAL_DATA_DIR


def db_path() -> Path:
    """Файл базы внутри папки данных."""
    return data_dir() / DB_FILENAME


def admin_ids() -> tuple[int, ...]:
    """ID владельцев из ADMIN_TELEGRAM_IDS. Пусто - значит админов нет."""
    raw = os.getenv("ADMIN_TELEGRAM_IDS") or ""
    out: list[int] = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk.lstrip("-").isdigit():
            out.append(int(chunk))
    return tuple(out)


def is_admin(telegram_id: int | None) -> bool:
    """Владелец ли это. Пустой список означает, что админ-команд нет ни у кого."""
    if telegram_id is None:
        return False
    return int(telegram_id) in admin_ids()


def env(name: str, default: str = "") -> str:
    """Переменная окружения без сюрпризов: пустая строка вместо None."""
    value = os.getenv(name)
    return default if value is None else value.strip()


def seller_details() -> dict[str, str]:
    """Реквизиты ИП для счёта. Пустые значения означают, что счёт не собрать."""
    keys = (
        "SELLER_NAME",
        "SELLER_INN",
        "SELLER_OGRNIP",
        "SELLER_ADDRESS",
        "SELLER_ACCOUNT",
        "SELLER_BANK",
        "SELLER_BIK",
        "SELLER_CORR_ACCOUNT",
    )
    return {key: env(key) for key in keys}


def missing_seller_details() -> tuple[str, ...]:
    """Каких реквизитов не хватает, чтобы выставить счёт."""
    return tuple(key for key, value in seller_details().items() if not value)
