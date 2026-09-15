"""Журнал событий: что происходило, без секретов.

В журнал не попадают токены, ключи и банковские реквизиты. Но артикул товара
и Telegram ID остаются: ради них журнал и читают, и вырезать их означало бы
сделать его бесполезным. Поэтому чистка адресная, а не «любое длинное число».
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from core import config, db

logger = logging.getLogger("wbrentgen.audit")

HIDDEN = "[скрыто]"

# Токены и ключи: их в журнале быть не должно ни в каком виде.
_TOKEN_PATTERNS = (
    # JWT: три части через точку, начинается с eyJ
    re.compile(r"\beyJ[A-Za-z0-9_\-]{5,}(?:\.[A-Za-z0-9_\-]*){1,3}"),
    # длинная непрерывная строка без пробелов похожа на ключ или токен
    re.compile(r"\b[A-Za-z0-9_\-]{40,}\b"),
)

# Реквизиты, которые однозначны по длине: ОГРНИП это 15 цифр,
# расчётный и корреспондентский счёт - 20. Артикул WB короче (4-12),
# Telegram ID тоже, поэтому они под это правило не попадают.
_LONG_NUMBER = re.compile(r"\b\d{15,}\b")

# Остальное узнаём по слову рядом: ИНН и БИК по длине не отличить от артикула.
_LABELLED_NUMBER = re.compile(
    r"(?i)\b(ИНН|КПП|БИК|ОГРНИП|ОГРН|счёт|счет|р/с|к/с|карта|карты)"
    r"(\s*(?:N|№|:)?\s*)(\d{8,20})"
)

_SECRET_ENV = (
    "TELEGRAM_BOT_TOKEN",
    "ENCRYPTION_KEY",
    "DADATA_API_KEY",
    "WB_PROXY",
    "SELLER_INN",
    "SELLER_OGRNIP",
    "SELLER_ACCOUNT",
    "SELLER_BIK",
    "SELLER_CORR_ACCOUNT",
)


def redact(text: str) -> str:
    """Прячет токены, ключи и реквизиты, оставляя артикулы и Telegram ID."""
    cleaned = str(text)
    for name in _SECRET_ENV:
        value = config.env(name)
        if value and len(value) >= 4:
            cleaned = cleaned.replace(value, HIDDEN)
    for pattern in _TOKEN_PATTERNS:
        cleaned = pattern.sub(HIDDEN, cleaned)
    cleaned = _LABELLED_NUMBER.sub(lambda m: f"{m.group(1)}{m.group(2)}{HIDDEN}", cleaned)
    return _LONG_NUMBER.sub(HIDDEN, cleaned)


def log(
    kind: str,
    client_id: int | None,
    message: str,
    level: str = "info",
    path: str | Path | None = None,
) -> None:
    """Записывает событие в журнал. Ошибка записи не должна ронять бота.

    Событие клиента идёт через repo(client_id), общее - через именованный
    метод общего слоя. Записи мимо client_id в слое данных не существует.
    """
    safe = redact(message)
    logger.log(
        {"error": logging.ERROR, "warning": logging.WARNING}.get(level, logging.INFO),
        "%s | клиент %s | %s",
        kind,
        client_id if client_id is not None else "нет",
        safe,
    )
    try:
        if client_id is None:
            db.admin_repo(path).add_event(kind=kind, message=safe, level=level)
        else:
            db.repo(client_id, path).insert(
                "events", level=level, kind=kind, message=safe
            )
    except Exception:  # журнал не имеет права остановить работу бота
        logger.exception("не удалось записать событие в базу")


def recent(
    limit: int = 50,
    level: str | None = None,
    client_id: int | None = None,
    path: str | Path | None = None,
) -> list[Any]:
    """Последние записи журнала, свежие сверху."""
    if client_id is not None:
        conditions: dict[str, Any] = {"level": level} if level else {}
        return db.repo(client_id, path).rows(
            "events", order_by="id DESC", limit=limit, **conditions
        )
    return db.admin_repo(path).events(limit=limit, level=level)
