"""Шифрование токенов клиентов (Fernet, ключ только из окружения).

В базе лежит шифротекст и ничего кроме. Ключ читается при каждом вызове,
чтобы бот, поднятый без ключа, честно жил дальше и объяснял, чего не хватает.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

KEY_ENV = "ENCRYPTION_KEY"

KEY_HINT = (
    "Ключ шифрования не задан. Сгенерируйте его командой:\n"
    'python -c "from cryptography.fernet import Fernet; '
    'print(Fernet.generate_key().decode())"\n'
    f"и положите результат в переменную окружения {KEY_ENV}."
)


class MissingKeyError(RuntimeError):
    """Ключа нет или он испорчен: принимать токены нельзя."""


class DecryptError(RuntimeError):
    """Шифротекст не подходит к текущему ключу."""


def generate_key() -> str:
    """Новый ключ Fernet строкой. Нужен для подсказки и для тестов."""
    return Fernet.generate_key().decode()


def _fernet() -> Fernet:
    raw = (os.getenv(KEY_ENV) or "").strip()
    if not raw:
        raise MissingKeyError(f"Не задана переменная {KEY_ENV}. {KEY_HINT}")
    try:
        return Fernet(raw.encode())
    except (ValueError, TypeError) as exc:
        raise MissingKeyError(
            f"Переменная {KEY_ENV} задана, но это не ключ Fernet. {KEY_HINT}"
        ) from exc


def key_available() -> bool:
    """Можно ли сейчас принимать токены клиентов."""
    try:
        _fernet()
    except MissingKeyError:
        return False
    return True


def encrypt(plain: str) -> bytes:
    """Шифрует строку. Каждый вызов даёт новый шифротекст."""
    return _fernet().encrypt(plain.encode("utf-8"))


def decrypt(blob: bytes) -> str:
    """Расшифровывает то, что зашифровал encrypt на том же ключе."""
    try:
        return _fernet().decrypt(bytes(blob)).decode("utf-8")
    except InvalidToken as exc:
        raise DecryptError(
            "Шифротекст не подходит к текущему ключу шифрования. "
            f"Похоже, {KEY_ENV} сменился: старые токены придётся выпустить заново."
        ) from exc
