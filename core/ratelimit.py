"""Ограничение частоты команд на одного человека.

Одно окно в минуту на каждого, число берётся из конфига: секция `[limits]`,
ключ `client_messages_per_minute`. В коде числа нет намеренно, владелец
меняет его в конфиге, а не правкой модуля.

Счёт идёт в памяти процесса, а не в базе. Так решено сознательно: строка в
базу на каждое сообщение это запись ради того, что живёт минуту и никому не
нужно после перезапуска. Перезапуск бота обнуляет окно, и это ровно то
поведение, которое здесь хочется: после падения никто не должен ждать.

Превышение это не молчание. Вызывающий получает `Decision` с `retry_after` и
говорит человеку «подождите немного» словами (`bot.texts.RATE_LIMITED`).
"""

from __future__ import annotations

import math
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

from core import config

WINDOW_SEC = 60

_seen: dict[int, deque[float]] = {}
_lock = threading.Lock()


@dataclass(frozen=True)
class Decision:
    """Пропускаем или просим подождать, и сколько секунд ждать."""

    allowed: bool
    retry_after: int = 0


def per_minute() -> int:
    """Сколько команд в минуту разрешено одному человеку. Из конфига."""
    return int(config.settings()["limits"].get("client_messages_per_minute", 0))


def _seconds(moment: datetime | None) -> float:
    if moment is None:
        moment = datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def check(who: int, *, limit: int | None = None, now: datetime | None = None) -> Decision:
    """Учитывает команду и говорит, пропускать её или просить подождать.

    `who` это Telegram ID человека, а не внутренний id клиента: ограничение
    защищает бота от одного собеседника, и знать про базу для этого не нужно.
    """
    allowed_count = per_minute() if limit is None else int(limit)
    if allowed_count <= 0:  # лимит выключен
        return Decision(True)

    stamp = _seconds(now)
    key = int(who)
    with _lock:
        window = _seen.setdefault(key, deque())
        while window and stamp - window[0] >= WINDOW_SEC:
            window.popleft()
        if len(window) < allowed_count:
            window.append(stamp)
            return Decision(True)
        wait = WINDOW_SEC - (stamp - window[0])
    return Decision(False, max(1, math.ceil(wait)))


def allow(who: int, *, limit: int | None = None, now: datetime | None = None) -> bool:
    """Короткий вопрос: пропускаем ли эту команду."""
    return check(who, limit=limit, now=now).allowed


def reset(who: int | None = None) -> None:
    """Забыть окно: всё целиком или про одного человека. Нужно тестам."""
    with _lock:
        if who is None:
            _seen.clear()
        else:
            _seen.pop(int(who), None)
