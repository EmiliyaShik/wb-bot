"""Ограничитель частоты: token bucket на каждую дорожку методов.

Лимиты WB считаются на их стороне по токену, поэтому бюджет живёт рядом с
клиентом и один на весь процесс: два агента одного клиента делят одну корзину,
иначе вдвоём они легко выйдут за лимит и токен заблокируют.

Числа взяты из официальной документации (сведены в docs/wb-api.md). Дорожка
отчёта о реализации сознательно одна на detailed и list: у обоих 1 запрос
в минуту, и общая корзина это та самая медленная дорожка из ТЗ.

Лимит /ping читается из config.toml, секция [limits]: он общий для проекта
и уже описан там.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from core import config

PING_LIMIT_DEFAULT = 3
PING_WINDOW_DEFAULT = 30.0


@dataclass(frozen=True)
class Limit:
    """Период в секундах, сколько запросов за период и сколько можно подряд."""

    limit: int
    period: float
    burst: int

    @property
    def rate(self) -> float:
        return self.limit / self.period


# Дорожка -> лимит. Дорожку назначает описание метода в client.py.
LANES: dict[str, Limit] = {
    # 1 запрос в минуту, всплеск 1. Самый жёсткий лимит в проекте.
    "finance-report": Limit(1, 60.0, 1),
    # Информация о продавце: 1 в минуту, всплеск 10.
    "common-seller-info": Limit(1, 60.0, 10),
    # Список кампаний: 5 в секунду, всплеск 5.
    "adv-count": Limit(5, 1.0, 5),
    # Статистика рекламы: 3 в минуту, интервал 20 секунд, всплеск 1.
    "adv-fullstats": Limit(3, 60.0, 1),
    # Воронка продаж: 3 в минуту, всплеск 3.
    "analytics-funnel": Limit(3, 60.0, 3),
    # Остатки: 3 в минуту, всплеск 1.
    "analytics-stocks": Limit(3, 60.0, 1),
    # Карточки: 100 в минуту, всплеск 5.
    "content-cards": Limit(100, 60.0, 5),
}


def ping_limit() -> Limit:
    """Лимит /ping на один домен. Берётся из конфига, а не из кода."""
    try:
        section = config.settings().get("limits", {})
        count = int(section.get("wb_requests_per_domain", PING_LIMIT_DEFAULT))
        window = float(section.get("wb_requests_window_sec", PING_WINDOW_DEFAULT))
    except (KeyError, TypeError, ValueError, OSError):
        count, window = PING_LIMIT_DEFAULT, PING_WINDOW_DEFAULT
    return Limit(count, window, count)


def lane_limit(lane: str) -> Limit:
    """Лимит дорожки. У /ping корзина на каждый домен: ping:<домен>."""
    if lane.startswith("ping:"):
        return ping_limit()
    # Неизвестная дорожка получает самый осторожный лимит, а не свободу:
    # ошибиться в сторону «медленнее» безопаснее, чем в сторону блокировки.
    return LANES.get(lane, Limit(1, 60.0, 1))


class Bucket:
    """Одна корзина. Ждёт столько, сколько нужно, и не делает ничего лишнего."""

    def __init__(self, limit: Limit, clock, sleep) -> None:
        self._limit = limit
        self._clock = clock
        self._sleep = sleep
        self._tokens = float(limit.burst)
        self._updated = clock()
        self._not_before = 0.0

    def _refill(self, now: float) -> None:
        grown = (now - self._updated) * self._limit.rate
        self._tokens = min(float(self._limit.burst), self._tokens + max(0.0, grown))
        self._updated = now

    async def take(self) -> None:
        """Пропускает один запрос, дождавшись своей очереди."""
        now = self._clock()
        if self._not_before > now:
            await self._sleep(self._not_before - now)
            now = self._clock()
            self._updated = now
            # Штраф отбыт: WB разрешил повтор именно сейчас, и ждать сверху
            # ещё один интервал значило бы наказать себя дважды за одно и то же.
            self._tokens = max(self._tokens, 1.0)
        self._refill(now)
        if self._tokens < 1.0:
            await self._sleep((1.0 - self._tokens) / self._limit.rate)
            self._refill(self._clock())
        self._tokens = max(0.0, self._tokens - 1.0)

    def penalize(self, seconds: float) -> None:
        """WB сказал «подожди»: корзина пуста и раньше срока никто не пойдёт."""
        self._tokens = 0.0
        now = self._clock()
        self._updated = now
        self._not_before = max(self._not_before, now + max(0.0, float(seconds)))

    def observe_remaining(self, remaining: int) -> None:
        """X-Ratelimit-Remaining: 0 значит, что следующий запрос ждёт паузу."""
        if remaining <= 0:
            self._tokens = 0.0
            self._updated = self._clock()


class Budget:
    """Корзины одного токена. Ключ это дорожка метода."""

    def __init__(self, clock=None, sleep=None) -> None:
        self._clock = clock or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._buckets: dict[str, Bucket] = {}

    def bucket(self, lane: str) -> Bucket:
        found = self._buckets.get(lane)
        if found is None:
            found = Bucket(lane_limit(lane), self._clock, self._sleep)
            self._buckets[lane] = found
        return found

    async def take(self, lane: str) -> None:
        await self.bucket(lane).take()

    def penalize(self, lane: str, seconds: float) -> None:
        self.bucket(lane).penalize(seconds)

    def observe(self, lane: str, headers) -> None:
        """Читает X-Ratelimit-Remaining, чтобы не упираться в лимит вслепую."""
        raw = headers.get("X-Ratelimit-Remaining") if headers else None
        if raw is None:
            return
        try:
            self.bucket(lane).observe_remaining(int(str(raw).strip()))
        except (TypeError, ValueError):
            return


_budgets: dict[int, Budget] = {}


def budget_for(client_id: int | None, clock=None, sleep=None) -> Budget:
    """Бюджет клиента, общий на процесс. Со своими часами бюджет всегда новый.

    Свои часы бывают только в тестах, и общая корзина там мешала бы:
    один тест ждал бы паузу, накопленную другим.
    """
    if clock is not None or sleep is not None:
        return Budget(clock, sleep)
    key = int(client_id or 0)
    found = _budgets.get(key)
    if found is None:
        found = Budget()
        _budgets[key] = found
    return found


def reset_limits() -> None:
    """Забыть все корзины. Нужно перезапуску и тестам."""
    _budgets.clear()
