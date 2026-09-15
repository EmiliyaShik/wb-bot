"""Разбор токена WB. Ни одного сетевого запроса.

Токен это JWT со сроком 180 дней, и весь нужный смысл лежит в payload:
срок (exp), кабинет (sid), тип токена (acc) и битовая маска свойств (s).
Подпись не проверяется: ключа от неё у нас нет и быть не может, а решение
«годится или нет» принимает сам WB при первом запросе.

Сам токен отсюда наружу не уходит: TokenInfo его не хранит.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from core.wbapi.errors import WBTokenFormatError

# Биты маски s, счёт от нуля. Источник: официальная таблица свойств токена.
# Биты 0, 8, 14, 15 в документации отсутствуют, поэтому их здесь нет.
CATEGORY_BITS: dict[str, int] = {
    "content": 1,
    "analytics": 2,
    "prices": 3,
    "marketplace": 4,
    "statistics": 5,
    "promotion": 6,
    "questions": 7,
    "chat": 9,
    "supplies": 10,
    "returns": 11,
    "documents": 12,
    "finance": 13,
    "users": 16,
}

CATEGORY_TITLES: dict[str, str] = {
    "content": "Контент",
    "analytics": "Аналитика",
    "prices": "Цены и скидки",
    "marketplace": "Маркетплейс",
    "statistics": "Статистика",
    "promotion": "Продвижение",
    "questions": "Вопросы и отзывы",
    "chat": "Чат с покупателями",
    "supplies": "Поставки",
    "returns": "Возвраты покупателями",
    "documents": "Документы",
    "finance": "Финансы",
    "users": "Пользователи",
}

READ_ONLY_BIT = 30

ACC_TITLES: dict[int, str] = {
    1: "Базовый",
    2: "Тестовый",
    3: "Персональный",
    4: "Сервисный",
}

# Сообщение об испорченном токене намеренно не показывает саму строку:
# в журнал и в чат она попасть не должна даже обрезанной.
BAD_TOKEN = (
    "Это не похоже на токен Wildberries. Токен выдаётся в личном кабинете "
    "в разделе доступа к API и выглядит как три части через точку."
)


def category_title(name: str) -> str:
    """Человеческое название категории для сообщений селлеру."""
    return CATEGORY_TITLES.get(name, name)


@dataclass(frozen=True)
class TokenInfo:
    """Что известно о токене без единого запроса к WB."""

    sid: str
    exp: int
    mask: int
    acc: int
    categories: tuple[str, ...]
    read_only: bool
    is_test: bool
    token_id: str = ""
    issued_for: str = ""

    @property
    def expires_at(self) -> datetime:
        return datetime.fromtimestamp(self.exp, tz=timezone.utc)

    @property
    def acc_title(self) -> str:
        return ACC_TITLES.get(self.acc, "Неизвестный")

    @property
    def titles(self) -> tuple[str, ...]:
        return tuple(category_title(name) for name in self.categories)

    def has(self, category: str) -> bool:
        return category in self.categories

    def missing(self, needed: tuple[str, ...]) -> tuple[str, ...]:
        """Каких из нужных категорий у токена нет."""
        return tuple(name for name in needed if name not in self.categories)

    def days_left(self, now: datetime | None = None) -> int:
        moment = now or datetime.now(timezone.utc)
        return (self.expires_at - moment).days

    def is_expired(self, now: datetime | None = None) -> bool:
        moment = now or datetime.now(timezone.utc)
        return self.expires_at <= moment


def _decode_part(part: str) -> dict:
    padded = part + "=" * (-len(part) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, UnicodeEncodeError, ValueError) as exc:
        raise WBTokenFormatError(BAD_TOKEN) from exc
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise WBTokenFormatError(BAD_TOKEN) from exc
    if not isinstance(payload, dict):
        raise WBTokenFormatError(BAD_TOKEN)
    return payload


def categories_from_mask(mask: int) -> tuple[str, ...]:
    """Категории из маски. Порядок по номеру бита, чтобы вывод был стабилен."""
    ordered = sorted(CATEGORY_BITS.items(), key=lambda pair: pair[1])
    return tuple(name for name, bit in ordered if mask & (1 << bit))


def verify_token(raw: str) -> TokenInfo:
    """Разбирает токен и отдаёт TokenInfo. Сети не касается.

    Бросает WBTokenFormatError, если это не JWT. Сама строка токена
    в текст ошибки не попадает.
    """
    text = (raw or "").strip()
    parts = text.split(".")
    if len(parts) != 3 or not all(parts[:2]):
        raise WBTokenFormatError(BAD_TOKEN)
    payload = _decode_part(parts[1])
    try:
        mask = int(payload.get("s", 0))
        exp = int(payload.get("exp", 0))
        acc = int(payload.get("acc", 0))
    except (TypeError, ValueError) as exc:
        raise WBTokenFormatError(BAD_TOKEN) from exc
    sid = str(payload.get("sid") or "")
    if not sid or not exp:
        raise WBTokenFormatError(BAD_TOKEN)
    return TokenInfo(
        sid=sid,
        exp=exp,
        mask=mask,
        acc=acc,
        categories=categories_from_mask(mask),
        read_only=bool(mask & (1 << READ_ONLY_BIT)),
        is_test=bool(payload.get("t", False)),
        token_id=str(payload.get("id") or ""),
        issued_for=str(payload.get("for") or ""),
    )
