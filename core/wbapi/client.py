"""HTTP к WB: заголовки, лимиты, ретраи, ошибки и обёртки методов.

Обёртки пишутся только здесь. Агент, которому нужен WB, берёт клиент через
get_wb_client(client_id) и вызывает метод; ни хоста, ни пути, ни версии,
ни тем более токена он не знает.

Методы, которые что-то меняют в кабинете, тут не появляются: токен только
на чтение, и единственный способ это гарантировать - не написать ни одной
такой обёртки.

Четыре метода из исходного ТЗ Wildberries отключил в 2026 году. Их путей здесь
нет вообще, даже в комментариях: что именно отключено и чем заменено, написано
один раз в docs/wb-api.md. Реализованы только замены.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Sequence

import httpx

from core import audit, config, crypto, db
from core.wbapi import limits
from core.wbapi.errors import (
    WBApiError,
    WBAuthError,
    WBError,
    WBForbiddenError,
    WBRateLimited,
    WBTokenMissing,
    WBUnavailable,
)
from core.wbapi.token import TokenInfo, category_title, verify_token

logger = logging.getLogger("wbrentgen.wbapi")

# Домены WB. Ключ это короткое имя для журнала и /diag.
HOSTS: dict[str, str] = {
    "finance": "finance-api.wildberries.ru",
    "analytics": "seller-analytics-api.wildberries.ru",
    "advert": "advert-api.wildberries.ru",
    "content": "content-api.wildberries.ru",
    "common": "common-api.wildberries.ru",
    "statistics": "statistics-api.wildberries.ru",
}

# Какая категория токена нужна домену. Пусто значит «любая».
HOST_CATEGORY: dict[str, str] = {
    HOSTS["finance"]: "finance",
    HOSTS["analytics"]: "analytics",
    HOSTS["advert"]: "promotion",
    HOSTS["content"]: "content",
    HOSTS["common"]: "",
    HOSTS["statistics"]: "statistics",
}

TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)

# Сколько раз ждать после 429 и как растёт пауза, читается из config.toml,
# секция [queue]: это те же числа, по которым повторяет задачи очередь.
# Лимиты методов при этом остаются в коде: они не настройка, а факт про WB.
QUEUE_DEFAULTS = {
    "attempts": 3,
    "retry_base_sec": 60.0,
    "retry_factor": 3.0,
    "retry_max_sec": 3600.0,
}

# Ограничения самого WB на размер запроса.
FULLSTATS_MAX_IDS = 50
FULLSTATS_MAX_DAYS = 31
REPORT_PAGE = 1000
CARDS_PAGE = 100
ANALYTICS_PAGE = 1000
MAX_PAGES = 500           # защита от бесконечной пагинации на кривом ответе

# У /diag короткий срок ожидания: владельцу нужен быстрый ответ, а не терпение.
PING_TIMEOUT = httpx.Timeout(connect=8.0, read=15.0, write=8.0, pool=8.0)

USER_AGENT = "WBRentgen/1.0"


@dataclass(frozen=True)
class RetryPolicy:
    """Сколько попыток и какая пауза. Источник один: секция [queue] конфига."""

    attempts: int
    base: float
    factor: float
    cap: float


def retry_policy() -> RetryPolicy:
    """Повторы из конфига. Своих чисел у клиента WB нет."""
    section = dict(QUEUE_DEFAULTS)
    try:
        section.update(config.settings().get("queue", {}))
    except (KeyError, TypeError, OSError):
        pass
    try:
        return RetryPolicy(
            attempts=max(1, int(section["attempts"])),
            base=float(section["retry_base_sec"]),
            factor=max(1.0, float(section["retry_factor"])),
            cap=float(section["retry_max_sec"]),
        )
    except (KeyError, TypeError, ValueError):
        return RetryPolicy(3, 60.0, 3.0, 3600.0)


@dataclass(frozen=True)
class Endpoint:
    """Один метод WB: глагол, домен, путь и дорожка лимита."""

    verb: str
    host: str
    path: str
    lane: str

    @property
    def url(self) -> str:
        return f"https://{self.host}{self.path}"

    @property
    def category(self) -> str:
        return HOST_CATEGORY.get(self.host, "")

    @property
    def label(self) -> str:
        return f"{self.verb} {self.path}"


ENDPOINTS: dict[str, Endpoint] = {
    "sales_report_detailed": Endpoint(
        "POST", HOSTS["finance"], "/api/finance/v1/sales-reports/detailed", "finance-report"
    ),
    "sales_reports_list": Endpoint(
        "POST", HOSTS["finance"], "/api/finance/v1/sales-reports/list", "finance-report"
    ),
    "promotion_count": Endpoint(
        "GET", HOSTS["advert"], "/adv/v1/promotion/count", "adv-count"
    ),
    "fullstats": Endpoint("GET", HOSTS["advert"], "/adv/v3/fullstats", "adv-fullstats"),
    "sales_funnel_products": Endpoint(
        "POST", HOSTS["analytics"], "/api/analytics/v3/sales-funnel/products", "analytics-funnel"
    ),
    "sales_funnel_history": Endpoint(
        "POST",
        HOSTS["analytics"],
        "/api/analytics/v3/sales-funnel/products/history",
        "analytics-funnel",
    ),
    "stocks_wb_warehouses": Endpoint(
        "POST",
        HOSTS["analytics"],
        "/api/analytics/v1/stocks-report/wb-warehouses",
        "analytics-stocks",
    ),
    "cards_list": Endpoint(
        "POST", HOSTS["content"], "/content/v2/get/cards/list", "content-cards"
    ),
    "seller_info": Endpoint(
        "GET", HOSTS["common"], "/api/v1/seller-info", "common-seller-info"
    ),
}

AUTH_MESSAGE = (
    "Wildberries не принял токен. Так бывает, если токен отозвали, "
    "у него кончился срок (180 дней) или он выпущен в другом кабинете."
)
UNAVAILABLE_MESSAGE = "Wildberries сейчас не отвечает."


def day(value: date | datetime | str) -> str:
    """Дата в виде ГГГГ-ММ-ДД: так её ждут реклама и аналитика."""
    if isinstance(value, str):
        return value[:10]
    return value.strftime("%Y-%m-%d")


def moment(value: date | datetime | str) -> str:
    """Дата и время в RFC3339: так её ждёт отчёт о реализации."""
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%S")
    return f"{value.strftime('%Y-%m-%d')}T00:00:00"


def _as_date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def chunks(items: Sequence[Any], size: int) -> list[list[Any]]:
    """Режет список на куски не длиннее size."""
    values = list(items)
    return [values[start : start + size] for start in range(0, len(values), size)] or [[]]


def date_windows(
    begin: date | datetime | str, end: date | datetime | str, max_days: int
) -> list[tuple[date, date]]:
    """Режет период на окна не длиннее max_days дней включительно."""
    start, finish = _as_date(begin), _as_date(end)
    if finish < start:
        start, finish = finish, start
    windows: list[tuple[date, date]] = []
    cursor = start
    while cursor <= finish:
        last = min(cursor + timedelta(days=max_days - 1), finish)
        windows.append((cursor, last))
        cursor = last + timedelta(days=1)
    return windows


def unwrap(payload: Any) -> Any:
    """Снимает обёртку data, если она есть. WB отвечает и так, и так."""
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def _listify(payload: Any, *keys: str) -> list[dict]:
    """Достаёт список строк из ответа, какой бы формы он ни был."""
    body = unwrap(payload)
    if isinstance(body, list):
        return [row for row in body if isinstance(row, dict)]
    if isinstance(body, dict):
        for key in keys:
            found = body.get(key)
            if isinstance(found, list):
                return [row for row in found if isinstance(row, dict)]
    return []


class WBClient:
    """Клиент одного кабинета. Токен внутри и наружу не выходит."""

    def __init__(
        self,
        client_id: int | None,
        token: str,
        http: httpx.AsyncClient,
        *,
        path: str | None = None,
        budget: limits.Budget | None = None,
        clock=None,
        sleep=None,
    ) -> None:
        self._client_id = int(client_id) if client_id is not None else None
        self._token = token
        self._http = http
        self._path = path
        self._budget = budget or limits.budget_for(client_id, clock, sleep)
        self._sleep = sleep or asyncio.sleep
        self._clock = clock or time.monotonic
        self._info: TokenInfo | None = None

    def __repr__(self) -> str:
        # Токена тут нет и быть не может: repr попадает и в логи, и в трассировки.
        return f"<WBClient client_id={self._client_id} sid={self.sid}>"

    # --- что о токене можно знать агенту ---

    @property
    def client_id(self) -> int | None:
        return self._client_id

    @property
    def session(self) -> httpx.AsyncClient:
        """Соединение, на котором работает клиент. Общее на процесс."""
        return self._http

    @property
    def token_info(self) -> TokenInfo:
        """Разбор токена без сети. Самой строки токена тут нет."""
        if self._info is None:
            self._info = verify_token(self._token)
        return self._info

    @property
    def sid(self) -> str:
        try:
            return self.token_info.sid
        except WBError:
            return ""

    def has_category(self, category: str) -> bool:
        try:
            return self.token_info.has(category)
        except WBError:
            return False

    # --- транспорт ---

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": self._token,   # без Bearer: это Seller API, а не WBD
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }

    def _safe(self, text: str) -> str:
        """Ни в одном тексте наружу не должно остаться токена."""
        cleaned = audit.redact(str(text))
        if self._token:
            cleaned = cleaned.replace(self._token, audit.HIDDEN)
        return cleaned.strip()

    def _detail(self, response: httpx.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            body = response.text
        if isinstance(body, dict):
            for key in ("detail", "title", "message", "errorText", "error", "code"):
                value = body.get(key)
                if isinstance(value, str) and value:
                    body = value
                    break
            else:
                body = ""
        return self._safe(str(body))[:200]

    def _record(self, endpoint: Endpoint, status: int | None, duration_ms: int) -> None:
        """Строка в api_calls на каждый вызов, удачный и нет."""
        try:
            if self._client_id is None:
                db.admin_repo(self._path).add_api_call(
                    endpoint.host, endpoint.label, status, duration_ms
                )
            else:
                db.repo(self._client_id, self._path).insert(
                    "api_calls",
                    host=endpoint.host,
                    method=endpoint.label,
                    status=status,
                    duration_ms=duration_ms,
                )
        except Exception:  # учёт вызовов не должен ронять сам вызов
            logger.exception("не записал вызов WB в api_calls")

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        for name in ("X-Ratelimit-Retry", "Retry-After", "X-Ratelimit-Reset"):
            raw = response.headers.get(name)
            if raw:
                try:
                    return float(str(raw).strip())
                except (TypeError, ValueError):
                    continue
        return None

    async def request(self, key: str, **kwargs: Any) -> Any:
        """Вызов метода по имени обёртки. Описание метода берётся из ENDPOINTS."""
        return await self.request_endpoint(ENDPOINTS[key], **kwargs)

    async def request_endpoint(
        self,
        spot: Endpoint,
        *,
        json: dict | None = None,
        params: dict | None = None,
        attempts: int | None = None,
        timeout: httpx.Timeout | float | None = None,
        raw: bool = False,
    ) -> Any:
        """Один вызов WB с лимитами, ожиданием после 429 и переводом ошибок.

        Повторов после 5xx и таймаута здесь нет намеренно. Их делает очередь
        (core.queue, секция [queue] конфига), и второй слой поверх неё
        перемножался бы с первым: одна пятисотка превращалась бы в дюжину
        обращений к WB. Ожидание после 429 это не повтор, а соблюдение лимита,
        и число таких ожиданий взято из той же секции конфига.
        """
        policy = retry_policy()
        backoff = policy.base
        pending: WBError | None = None
        tries = policy.attempts if attempts is None else max(1, int(attempts))

        for attempt in range(1, tries + 1):
            await self._budget.take(spot.lane)
            started = self._clock()
            try:
                response = await self._http.request(
                    spot.verb,
                    spot.url,
                    headers=self._headers(),
                    json=json,
                    params=params,
                    timeout=timeout or TIMEOUT,
                )
            except httpx.TransportError as exc:
                spent = int((self._clock() - started) * 1000)
                self._record(spot, None, spent)
                # Повторит очередь: здесь мы честно говорим, что ответа не было.
                raise WBUnavailable(
                    f"{UNAVAILABLE_MESSAGE} {self._safe(type(exc).__name__)}",
                    path=spot.label,
                ) from exc

            spent = int((self._clock() - started) * 1000)
            status = response.status_code
            self._record(spot, status, spent)
            self._budget.observe(spot.lane, response.headers)

            if 200 <= status < 300:
                if raw:
                    return response
                try:
                    return response.json()
                except ValueError:
                    return {}

            if status == 401:
                raise WBAuthError(
                    f"{AUTH_MESSAGE} {self._detail(response)}".strip(),
                    status=status,
                    path=spot.label,
                )

            if status == 403:
                category = spot.category
                need = (
                    f"У токена нет категории «{category_title(category)}», "
                    "поэтому этот метод недоступен."
                    if category
                    else "Wildberries отказал в доступе к этому методу."
                )
                raise WBForbiddenError(
                    f"{need} {self._detail(response)}".strip(),
                    status=status,
                    path=spot.label,
                    category=category,
                )

            if status == 429:
                named = self._retry_after(response)
                pause = named if named is not None else backoff
                self._budget.penalize(spot.lane, pause)
                pending = WBRateLimited(
                    "Wildberries просит подождать: слишком много запросов.",
                    retry_after=named if named is not None else pause,
                    status=status,
                    path=spot.label,
                )
                backoff = min(backoff * policy.factor, policy.cap)
                if attempt == tries:
                    raise pending
                continue

            if status >= 500:
                raise WBUnavailable(
                    f"{UNAVAILABLE_MESSAGE} Код {status}.", status=status, path=spot.label
                )

            raise WBApiError(
                f"Wildberries вернул ошибку {status}. {self._detail(response)}".strip(),
                status=status,
                path=spot.label,
            )

        raise pending or WBUnavailable(UNAVAILABLE_MESSAGE, path=spot.label)

    # --- проверка связи ---

    async def ping(self, host: str, *, timeout: httpx.Timeout | float | None = None) -> int:
        """GET /ping на домен. Отдаёт код ответа, который прислал сам WB.

        Ожидание после 429 тут выключено: документация прямо запрещает
        автоматизировать этот метод, а владельцу нужен настоящий ответ,
        а не настойчивость. Вызывается только вручную, из probe_hosts.
        """
        spot = Endpoint("GET", host, "/ping", f"ping:{host}")
        response = await self.request_endpoint(
            spot, attempts=1, timeout=timeout or PING_TIMEOUT, raw=True
        )
        return int(response.status_code)

    async def pause(self, seconds: float) -> None:
        """Подождать по часам этого клиента. Нужно проверке связи."""
        await self._sleep(seconds)

    async def seller_info(self) -> dict:
        """Информация о продавце: name, sid, tin, tradeMark. Токен любой категории."""
        body = unwrap(await self.request("seller_info"))
        return body if isinstance(body, dict) else {}

    # --- отчёт о реализации: замена отключённому методу из ТЗ ---

    async def sales_report_detailed(
        self,
        date_from: date | datetime | str,
        date_to: date | datetime | str,
        *,
        period: str = "weekly",
        limit: int = REPORT_PAGE,
        fields: Sequence[str] | None = None,
        max_pages: int = MAX_PAGES,
    ) -> list[dict]:
        """Все строки отчёта за период. Пагинация по rrdId, как велит документация.

        Дорожка медленная: 1 запрос в минуту. Страница за страницей это долго,
        и так и задумано: лучше медленнее, чем блокировка токена.
        """
        rows: list[dict] = []
        cursor = 0
        for _ in range(max_pages):
            body: dict[str, Any] = {
                "dateFrom": moment(date_from),
                "dateTo": moment(date_to),
                "limit": int(limit),
                "rrdId": int(cursor),
                "period": period,
            }
            if fields:
                body["fields"] = list(fields)
            page = _listify(await self.request("sales_report_detailed", json=body), "rows", "items")
            rows.extend(page)
            if len(page) < int(limit):
                break
            last = page[-1].get("rrdId")
            if last is None or int(last) == cursor:
                break
            cursor = int(last)
        return rows

    async def sales_reports_list(
        self,
        date_from: date | datetime | str,
        date_to: date | datetime | str,
        *,
        limit: int = 100,
    ) -> list[dict]:
        """Готовые агрегаты по отчётам: ими сверяется наш расчёт с кабинетом."""
        body = {
            "dateFrom": moment(date_from),
            "dateTo": moment(date_to),
            "limit": int(limit),
        }
        return _listify(await self.request("sales_reports_list", json=body), "reports", "items")

    # --- реклама ---

    async def promotion_count(self) -> dict:
        """Список кампаний: adverts[].advert_list[].advertId. Параметров нет."""
        body = unwrap(await self.request("promotion_count"))
        return body if isinstance(body, dict) else {}

    async def advert_ids(self) -> list[int]:
        """Плоский список ID кампаний, как их ждёт fullstats."""
        found: list[int] = []
        for group in self.promotion_groups(await self.promotion_count()):
            found.extend(group)
        return found

    @staticmethod
    def promotion_groups(payload: dict) -> list[list[int]]:
        groups: list[list[int]] = []
        for advert in payload.get("adverts") or []:
            ids = [
                int(item["advertId"])
                for item in (advert.get("advert_list") or [])
                if item.get("advertId") is not None
            ]
            if ids:
                groups.append(ids)
        return groups

    async def fullstats(
        self,
        ids: Iterable[int],
        begin_date: date | datetime | str,
        end_date: date | datetime | str,
    ) -> list[dict]:
        """Статистика кампаний. Сама режется по ограничениям WB.

        WB принимает максимум 50 кампаний и 31 день за запрос, поэтому список
        и период рубятся здесь, а не у агента: иначе это ограничение пришлось бы
        помнить каждому вызывающему.
        """
        collected: list[dict] = []
        wanted = [int(value) for value in ids]
        if not wanted:
            return collected
        for window in date_windows(begin_date, end_date, FULLSTATS_MAX_DAYS):
            for part in chunks(wanted, FULLSTATS_MAX_IDS):
                if not part:
                    continue
                params = {
                    "ids": ",".join(str(value) for value in part),
                    "beginDate": day(window[0]),
                    "endDate": day(window[1]),
                }
                collected.extend(_listify(await self.request("fullstats", params=params)))
        return collected

    # --- воронка продаж: замена отключённому методу из ТЗ ---

    async def sales_funnel_products(
        self,
        start: date | datetime | str,
        end: date | datetime | str,
        *,
        past: tuple[date | datetime | str, date | datetime | str] | None = None,
        nm_ids: Sequence[int] | None = None,
        limit: int = 100,
        max_pages: int = MAX_PAGES,
    ) -> list[dict]:
        """Воронка за период, страницами по limit/offset.

        past это прошлый период для сравнения. Без него WB не присылает
        statistic.past и comparison, а сравнение с прошлой неделей нужно
        плану-факту: поэтому параметр есть, но остаётся необязательным.
        """
        found: list[dict] = []
        offset = 0
        for _ in range(max_pages):
            body: dict[str, Any] = {
                "selectedPeriod": {"start": moment(start), "end": moment(end)},
                "limit": int(limit),
                "offset": int(offset),
                "skipDeletedNm": False,
            }
            if past:
                body["pastPeriod"] = {"start": moment(past[0]), "end": moment(past[1])}
            if nm_ids:
                body["nmIds"] = [int(value) for value in nm_ids]
            page = _listify(
                await self.request("sales_funnel_products", json=body), "products", "items"
            )
            found.extend(page)
            if len(page) < int(limit):
                break
            offset += int(limit)
        return found

    async def sales_funnel_history(
        self,
        start: date | datetime | str,
        end: date | datetime | str,
        *,
        nm_ids: Sequence[int] | None = None,
        aggregation_level: str = "day",
    ) -> list[dict]:
        """Воронка по дням. WB отдаёт максимум за последнюю неделю.

        Поэтому суточные данные копятся у нас в nm_daily: за месяц назад
        их уже не спросить.
        """
        body: dict[str, Any] = {
            "selectedPeriod": {"start": day(start), "end": day(end)},
            "aggregationLevel": aggregation_level,
            "skipDeletedNm": False,
        }
        if nm_ids:
            body["nmIds"] = [int(value) for value in nm_ids]
        return _listify(await self.request("sales_funnel_history", json=body), "products", "items")

    # --- остатки: замена отключённому методу из ТЗ ---

    async def stocks_wb_warehouses(
        self,
        *,
        nm_ids: Sequence[int] | None = None,
        limit: int = ANALYTICS_PAGE,
        max_pages: int = MAX_PAGES,
    ) -> list[dict]:
        """Остатки на складах WB, страницами по limit/offset."""
        found: list[dict] = []
        offset = 0
        for _ in range(max_pages):
            body: dict[str, Any] = {"limit": int(limit), "offset": int(offset)}
            if nm_ids:
                body["nmIds"] = [int(value) for value in nm_ids]
            page = _listify(
                await self.request("stocks_wb_warehouses", json=body), "items", "stocks"
            )
            found.extend(page)
            if len(page) < int(limit):
                break
            offset += int(limit)
        return found

    # --- карточки товаров ---

    async def cards_list(
        self, *, limit: int = CARDS_PAGE, max_pages: int = MAX_PAGES
    ) -> list[dict]:
        """Карточки для шаблона себестоимости. Курсор updatedAt плюс nmID.

        Алгоритм из документации: повторять, пока total в ответе не станет
        меньше запрошенного limit.
        """
        found: list[dict] = []
        cursor: dict[str, Any] = {"limit": int(limit), "updatedAt": "", "nmID": 0}
        for _ in range(max_pages):
            body = {
                "settings": {
                    "sort": {"ascending": True},
                    "cursor": dict(cursor),
                    "filter": {"withPhoto": -1},
                }
            }
            payload = unwrap(await self.request("cards_list", json=body))
            page = _listify(payload, "cards")
            found.extend(page)
            answer = payload.get("cursor") if isinstance(payload, dict) else None
            total = int((answer or {}).get("total", len(page)))
            if total < int(limit) or not page:
                break
            cursor = {
                "limit": int(limit),
                "updatedAt": (answer or {}).get("updatedAt", ""),
                "nmID": (answer or {}).get("nmID", 0),
            }
        return found


_session: httpx.AsyncClient | None = None


def shared_session() -> httpx.AsyncClient:
    """Одно соединение на процесс.

    Новый AsyncClient на каждый вызов означал бы новый пул соединений, который
    никто не закрывает: на длинной работе бота их число росло бы молча.
    """
    global _session
    if _session is None or _session.is_closed:
        _session = httpx.AsyncClient(timeout=TIMEOUT)
    return _session


async def close_session() -> None:
    """Закрыть общее соединение. Зовётся при остановке бота и в тестах."""
    global _session
    if _session is not None and not _session.is_closed:
        await _session.aclose()
    _session = None


def load_token(client_id: int, path: str | None = None) -> str:
    """Достаёт и расшифровывает токен клиента. Дальше этой функции он не идёт."""
    row = db.repo(client_id, path).one("wb_tokens")
    if row is None or not row["ciphertext"]:
        raise WBTokenMissing(
            "Кабинет Wildberries ещё не подключён: токена нет. Команда /connect."
        )
    try:
        return crypto.decrypt(row["ciphertext"])
    except crypto.DecryptError as exc:
        raise WBTokenMissing(str(exc)) from exc
    except crypto.MissingKeyError as exc:
        raise WBTokenMissing(str(exc)) from exc


def get_wb_client(
    client_id: int,
    *,
    http: httpx.AsyncClient | None = None,
    path: str | None = None,
    clock=None,
    sleep=None,
) -> WBClient:
    """Клиент WB для кабинета.

    Токен достаётся и расшифровывается здесь, внутри. Наружу он не выходит
    ни возвращаемым значением, ни атрибутом, ни текстом ошибки: именно это
    позже позволит подставить сервисный токен или OAuth, не трогая агентов.
    """
    token = load_token(client_id, path)
    return WBClient(
        client_id,
        token,
        http or shared_session(),
        path=path,
        clock=clock,
        sleep=sleep,
    )
