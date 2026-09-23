"""Ограничение частоты обращений одного человека.

Одно окно в минуту на каждого, число берётся из конфига: секция `[limits]`,
ключ `client_messages_per_minute`. В коде числа нет намеренно, владелец
меняет его в конфиге, а не правкой модуля.

Счёт идёт в памяти процесса, а не в базе. Так решено сознательно: строка в
базу на каждое сообщение это запись ради того, что живёт минуту и никому не
нужно после перезапуска. Перезапуск бота обнуляет окно, и это ровно то
поведение, которое здесь хочется: после падения никто не должен ждать.

Превышение это не молчание. Вызывающий получает `Decision` с `retry_after` и
говорит человеку «подождите немного» словами (`bot.texts.RATE_LIMITED`).

Считается не только команда. Дорогое в этом боте как раз не команда: разбор
присланного файла и проверка токена живым запросом к Wildberries идут по
обычному сообщению, а бот обрабатывает по одному обновлению за раз. Поэтому
у счётчика есть области (`scope`): сообщения, кнопки и предупреждения ведутся
отдельными окнами и не съедают друг друга.
"""

from __future__ import annotations

import math
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

from core import config

WINDOW_SEC = 60

# Области счёта. Сообщение это работа бота, нажатие кнопки почти всегда нет.
MESSAGES = "message"
BUTTONS = "button"
WARNED = "warn"

# Кнопки терпимее: двойное нажатие это норма, а не атака, и человек не должен
# получать отказ за то, что палец дрогнул. Порог кратен тому же числу из
# конфига, чтобы владелец правил одно место, а не два.
BUTTON_FACTOR = 3

_seen: dict[tuple[str, int], deque[float]] = {}
_lock = threading.Lock()


@dataclass(frozen=True)
class Decision:
    """Пропускаем или просим подождать, и сколько секунд ждать."""

    allowed: bool
    retry_after: int = 0


def per_minute() -> int:
    """Сколько обращений в минуту разрешено одному человеку. Из конфига."""
    return int(config.settings()["limits"].get("client_messages_per_minute", 0))


def button_per_minute() -> int:
    """Порог для нажатий кнопок: тот же ключ конфига, с запасом."""
    return per_minute() * BUTTON_FACTOR


def _seconds(moment: datetime | None) -> float:
    if moment is None:
        moment = datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def check(
    who: int,
    *,
    scope: str = MESSAGES,
    limit: int | None = None,
    now: datetime | None = None,
) -> Decision:
    """Учитывает обращение и говорит, пропускать его или просить подождать.

    `who` это Telegram ID человека, а не внутренний id клиента: ограничение
    защищает бота от одного собеседника, и знать про базу для этого не нужно.

    `scope` это отдельное окно: поток сообщений не должен запрещать человеку
    нажать кнопку, а служебные окна не должны съедать его сообщения.
    """
    allowed_count = per_minute() if limit is None else int(limit)
    if allowed_count <= 0:  # лимит выключен
        return Decision(True)

    stamp = _seconds(now)
    key = (str(scope), int(who))
    with _lock:
        window = _seen.setdefault(key, deque())
        while window and stamp - window[0] >= WINDOW_SEC:
            window.popleft()
        if len(window) < allowed_count:
            window.append(stamp)
            return Decision(True)
        wait = WINDOW_SEC - (stamp - window[0])
    return Decision(False, max(1, math.ceil(wait)))


def allow(
    who: int,
    *,
    scope: str = MESSAGES,
    limit: int | None = None,
    now: datetime | None = None,
) -> bool:
    """Короткий вопрос: пропускаем ли это обращение."""
    return check(who, scope=scope, limit=limit, now=now).allowed


def reset(who: int | None = None, *, scope: str | None = None) -> None:
    """Забыть окно: всё целиком, про одного человека или про одну область."""
    with _lock:
        if who is None and scope is None:
            _seen.clear()
            return
        for key in [
            key
            for key in _seen
            if (who is None or key[1] == int(who)) and (scope is None or key[0] == scope)
        ]:
            _seen.pop(key, None)
