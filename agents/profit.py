"""Агент 3. Прибыльность артикулов: кто кормит, а кто ест.

Три источника, и только один из них требует похода в Wildberries.

1. **Данные агента 1.** Недели лежат в `fin_weeks`, строки отчёта о
   реализации в `fin_rows`. Отсюда берутся выручка, возвраты, штуки,
   комиссия, эквайринг, логистика, хранение, приёмка, штрафы и удержания.
   В WB за этим ходить не нужно: агент 1 уже сходил.
2. **Себестоимость.** `core.costs.costs_for`, её загружает сам селлер.
3. **Расход рекламы по артикулам.** `GET /adv/v3/fullstats`, цепочка
   `days[] -> apps[] -> nms[].sum`. Это единственный поход в WB, и он идёт
   через очередь: повтор при недоступности живёт только там.

Два случая нехватки данных штатные, а не ошибки, и оба видны в отчёте.

**Нет себестоимости** по артикулу: прибыль по нему не считается, он уходит в
блок «нет себестоимости», остальное считается, и `/profit` работает (R168).

**Нет категории «Продвижение»** или `fullstats` недоступен: колонка «реклама»
помечается «нет данных», прибыль считается без неё, и в отчёте про это
написано честно (R112.1).

Деньги везде `Decimal`, в базе целые копейки. `float` остаётся только
процентам: маржинальность и доля в прибыли, как и в схеме базы.

Текстов бота здесь нет. Отчёт это данные (`ProfitReport`), а во что их
превратить, знает `bot/handlers/profit.py`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from agents import finance
from core import costs as costs_module
from core import db, queue, wbapi

logger = logging.getLogger(__name__)

__all__ = [
    "MODULE",
    "TASK_KIND",
    "PERIODS",
    "TOP_SIZE",
    "ARTICLES_SHEET",
    "PROBLEMS_SHEET",
    "METHOD_SHEET",
    "ADS_OK",
    "ADS_NO_CATEGORY",
    "ADS_UNAVAILABLE",
    "ADS_NOT_REQUESTED",
    "AdSpend",
    "ArticleProfit",
    "ProfitReport",
    "margin",
    "share",
    "spread",
    "ad_totals",
    "collect_ads",
    "build",
    "excel_sheets",
    "excel_bytes",
    "file_name",
    "request_report",
    "report_task",
    "set_sender",
    "register_jobs",
    "METHODOLOGY",
]

# Прибыльность артикулов входит в модуль «Финансы»: отдельной подписки на
# неё нет, см. config.toml.
MODULE = "finance"

# Вид фоновой задачи. Связывается с обработчиком при сборке бота.
TASK_KIND = "profit_report"

# Периоды те же, что у агента 1: прибыль считается по тем же неделям.
PERIODS = finance.PERIODS

# Сколько артикулов показываем в топе и антитопе сообщения.
TOP_SIZE = 5

ZERO = Decimal("0")
CENT = Decimal("0.01")


# --- чистые расчёты ----------------------------------------------------------


def _money(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value or 0))


def margin(profit_value: Decimal, net_revenue: Decimal) -> float | None:
    """Маржинальность: прибыль к выручке за вычетом возвратов, в процентах.

    Без выручки маржинальности не существует, и это не ноль: делить на ноль
    нельзя, а «0%» прочиталось бы как «работаем в ноль».
    """
    revenue = _money(net_revenue)
    if revenue <= ZERO:
        return None
    return float((_money(profit_value) * 100 / revenue).quantize(CENT, rounding=ROUND_HALF_UP))


def share(profit_value: Decimal, total: Decimal) -> float | None:
    """Доля артикула в общей прибыли, в процентах.

    Если общая прибыль не положительная, доли в ней нет: делить убыток на
    убыток и показывать проценты значит выдумывать смысл. У убыточного
    артикула доля отрицательная, и сумма долей прибыльных может оказаться
    больше ста процентов: убытки съедают часть заработанного. Это написано
    на листе «Методология».
    """
    whole = _money(total)
    if whole <= ZERO:
        return None
    return float((_money(profit_value) * 100 / whole).quantize(CENT, rounding=ROUND_HALF_UP))


def spread(amount: Decimal, weights: Mapping[int, Decimal]) -> dict[int, Decimal]:
    """Разносит сумму по артикулам пропорционально весам (выручке).

    Правило названо на листе «Методология» и в тексте отчёта, а не спрятано
    в коде: расход без артикула иначе либо исчезает, либо оседает на случайном
    товаре. Артикулы с нулевым весом доли не получают. Остаток от округления
    достаётся самому крупному: сумма долей обязана сойтись с исходной суммой
    до копейки.
    """
    total = sum((_money(value) for value in weights.values()), ZERO)
    if total <= ZERO or not weights:
        return {}
    amount = _money(amount)
    parts: dict[int, Decimal] = {}
    for nm_id, weight in weights.items():
        value = _money(weight)
        if value <= ZERO:
            continue
        parts[nm_id] = (amount * value / total).quantize(CENT, rounding=ROUND_HALF_UP)
    if not parts:
        return {}
    rest = amount.quantize(CENT, rounding=ROUND_HALF_UP) - sum(parts.values(), ZERO)
    if rest:
        biggest = max(parts, key=lambda nm_id: _money(weights[nm_id]))
        parts[biggest] = parts[biggest] + rest
    return parts


# --- расход рекламы ----------------------------------------------------------

# Почему рекламы может не быть. Это ключи, а не тексты: словами про них
# говорит `bot/handlers/profit.py`, у него на то и тексты.
ADS_OK = "ok"
ADS_NO_CATEGORY = "no_category"
ADS_UNAVAILABLE = "unavailable"
ADS_NOT_REQUESTED = "not_requested"


@dataclass(frozen=True)
class AdSpend:
    """Расход рекламы по артикулам и честный ответ, есть ли он вообще.

    `available=False` это не «реклама стоила ноль», а «данных нет». Разница
    принципиальная: нулём мы приписали бы селлеру прибыль, которой у него
    может и не быть.
    """

    spend: Mapping[int, Decimal] = None  # type: ignore[assignment]
    available: bool = True
    reason: str = ADS_OK

    def __post_init__(self) -> None:
        object.__setattr__(self, "spend", dict(self.spend or {}))

    def of(self, nm_id: int | None) -> Decimal | None:
        """Расход по артикулу или None, если данных о рекламе нет вовсе."""
        if not self.available:
            return None
        if nm_id is None:
            return ZERO
        return _money(self.spend.get(int(nm_id)))

    @property
    def total(self) -> Decimal:
        return sum((_money(value) for value in self.spend.values()), ZERO)


def ad_totals(campaigns: Iterable[dict]) -> dict[int, Decimal]:
    """Расход по артикулам из ответа `fullstats`.

    Цепочка ровно та, что описана в справочнике: `days[] -> apps[] -> nms[]`,
    поле `sum`, артикул называется `nmId` (имя `nm` живёт только в
    `boosterStats` и к расходу отношения не имеет).
    """
    totals: dict[int, Decimal] = {}
    for campaign in campaigns or []:
        for day_row in (campaign or {}).get("days") or []:
            for app in (day_row or {}).get("apps") or []:
                for item in (app or {}).get("nms") or []:
                    raw = (item or {}).get("nmId")
                    if raw in (None, ""):
                        continue
                    try:
                        nm_id = int(raw)
                    except (TypeError, ValueError):
                        continue
                    totals[nm_id] = totals.get(nm_id, ZERO) + _money(item.get("sum"))
    return totals


async def collect_ads(
    client_id: int,
    date_from: date,
    date_to: date,
    *,
    http: Any = None,
    path: str | Path | None = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], Any] | None = None,
) -> AdSpend:
    """Расход рекламы за период. Зовётся только из обработчика очереди.

    Разбивку по ограничениям WB (не больше 50 кампаний и 31 дня за запрос,
    3 запроса в минуту) делает `core.wbapi`: помнить её каждому вызывающему
    незачем.

    Отсутствие рекламы не роняет отчёт. Нет категории «Продвижение» - это
    R112.1 дословно; недоступный метод по последствиям то же самое: прибыль
    считается без рекламы, а в отчёте написано, почему её там нет. Наружу
    летит только 401: его разбирает `core.clients`, и без этого модули не
    встанут на паузу.
    """
    client = wbapi.get_wb_client(client_id, http=http, path=path, clock=clock, sleep=sleep)
    try:
        ids = await client.advert_ids()
        if not ids:
            return AdSpend({}, available=True, reason=ADS_OK)
        return AdSpend(ad_totals(await client.fullstats(ids, date_from, date_to)))
    except wbapi.WBForbiddenError:
        logger.info("у клиента %s нет категории «Продвижение», расход рекламы пропущен", client_id)
        return AdSpend({}, available=False, reason=ADS_NO_CATEGORY)
    except (wbapi.WBUnavailable, wbapi.WBRateLimited, wbapi.WBApiError) as error:
        logger.warning("расход рекламы клиента %s не получен: %s", client_id, error)
        return AdSpend({}, available=False, reason=ADS_UNAVAILABLE)


# --- строки отчёта -----------------------------------------------------------


@dataclass(frozen=True)
class ArticleProfit:
    """Один артикул за период. `None` в деньгах значит «неизвестно»."""

    nm_id: int | None
    vendor_code: str = ""
    subject: str = ""
    units: int = 0
    returns_count: int = 0
    revenue: Decimal = ZERO
    returns_amount: Decimal = ZERO
    cost_per_unit: Decimal | None = None
    cost: Decimal | None = None
    # None это «комиссия неизвестна»: строка отчёта сохранена без `raw`, а
    # подставлять вместо неё соседнее поле WB нельзя.
    commission: Decimal | None = ZERO
    commission_known: bool = True
    acquiring: Decimal = ZERO
    logistics: Decimal = ZERO
    storage: Decimal = ZERO
    acceptance: Decimal = ZERO
    penalties: Decimal = ZERO
    deductions: Decimal = ZERO
    ad_spend: Decimal | None = None
    unallocated: Decimal = ZERO
    profit: Decimal | None = None
    margin: float | None = None
    share: float | None = None

    @property
    def net_revenue(self) -> Decimal:
        """Выручка за вычетом возвратов. Именно от неё считается маржа."""
        return self.revenue - self.returns_amount

    @property
    def wb_costs(self) -> Decimal | None:
        """Всё, что Wildberries забрал по этому артикулу.

        Неизвестная комиссия делает неизвестной и сумму: складывать её с
        нулём значит занизить расходы и завысить прибыль.
        """
        if self.commission is None:
            return None
        return (
            self.commission
            + self.acquiring
            + self.logistics
            + self.storage
            + self.acceptance
            + self.penalties
            + self.deductions
        )

    @property
    def has_cost(self) -> bool:
        return self.cost is not None

    @property
    def is_loss(self) -> bool:
        return self.profit is not None and self.profit < ZERO


def _halves(count: int) -> tuple[int, int]:
    """Сколько строк уходит в топ и сколько в антитоп. Пересечения не бывает.

    Пять и пять, пока артикулов хватает. Когда их меньше десяти, список
    делится пополам: один и тот же товар не может быть и лучшим, и худшим.
    """
    if count > 2 * TOP_SIZE:
        return TOP_SIZE, TOP_SIZE
    bottom = count // 2
    return count - bottom, bottom


@dataclass(frozen=True)
class ProfitReport:
    """Данные отчёта. Текстов здесь нет, их знает поверхность бота."""

    client_id: int
    period: str
    date_from: date
    date_to: date
    articles: tuple[ArticleProfit, ...] = ()
    unallocated: Decimal = ZERO
    unallocated_left: Decimal = ZERO
    ads: AdSpend = None  # type: ignore[assignment]
    weeks: tuple[Any, ...] = ()

    def __post_init__(self) -> None:
        if self.ads is None:
            object.__setattr__(
                self, "ads", AdSpend({}, available=False, reason=ADS_NOT_REQUESTED)
            )

    @property
    def priced(self) -> tuple[ArticleProfit, ...]:
        """Артикулы, по которым прибыль посчитана: у них есть себестоимость."""
        return tuple(item for item in self.articles if item.profit is not None)

    @property
    def without_cost(self) -> tuple[ArticleProfit, ...]:
        """Блок «нет себестоимости»: прибыль по ним не считается (R168)."""
        return tuple(
            item for item in self.articles if item.profit is None and item.cost is None
        )

    @property
    def without_commission(self) -> tuple[ArticleProfit, ...]:
        """Артикулы, у которых комиссия неизвестна: строка без `raw`."""
        return tuple(item for item in self.articles if not item.commission_known)

    @property
    def losses(self) -> tuple[ArticleProfit, ...]:
        return tuple(item for item in self.priced if item.is_loss)

    @property
    def top(self) -> tuple[ArticleProfit, ...]:
        count, _ = _halves(len(self.priced))
        return self.priced[:count]

    @property
    def bottom(self) -> tuple[ArticleProfit, ...]:
        """Антитоп, от самого убыточного вверх. С топом не пересекается."""
        priced = self.priced
        _, count = _halves(len(priced))
        return tuple(reversed(priced[len(priced) - count :])) if count else ()

    @property
    def total_profit(self) -> Decimal:
        return sum((item.profit for item in self.priced), ZERO)

    @property
    def total_revenue(self) -> Decimal:
        return sum((item.net_revenue for item in self.articles), ZERO)

    @property
    def total_ad_spend(self) -> Decimal | None:
        """Реклама за период или None, если данных о ней нет."""
        if not self.ads.available:
            return None
        return sum((item.ad_spend or ZERO for item in self.articles), ZERO)

    @property
    def empty(self) -> bool:
        return not self.articles

    @property
    def title(self) -> str:
        return finance.PERIOD_TITLES.get(self.period, self.period)

    @property
    def incomplete(self) -> tuple[Any, ...]:
        """Недели, выгрузка которых у агента 1 упёрлась в потолок страниц."""
        return tuple(week for week in self.weeks if not week.complete)

    @property
    def unverified(self) -> tuple[Any, ...]:
        """Недели, которые агент 1 не сверил с итогами Wildberries."""
        return tuple(week for week in self.weeks if not week.verified)

# --- разрез по артикулам -----------------------------------------------------


def _articles_without_raw(
    client_id: int,
    weeks: Sequence[Any],
    *,
    path: str | Path | None = None,
) -> set[int]:
    """Артикулы, у которых хоть одна строка сохранена без `raw`.

    Комиссия строки лежит в `raw` (поле `vw`), отдельной колонки под неё в
    схеме нет. Строка без `raw` это не «комиссия ноль» и тем более не повод
    подставить соседнее поле `ppvz_sales_commission_kop`: это другая величина
    Wildberries, и подстановка дала бы третье основание комиссии, о котором
    селлер не узнал бы никогда. Такой артикул помечается, и прибыль по нему не
    считается - ровно как артикул без себестоимости.

    Это проверка одного поля, а не второй разбор строк: складывает выручку,
    возвраты и штуки по-прежнему только `finance.articles_of`.
    """
    found: set[int] = set()
    for report_id in sorted({week.report_id for week in weeks}):
        for row in db.repo(client_id, path).rows("fin_rows", report_id=report_id):
            if row["nm_id"] is not None and not row["raw"]:
                found.add(int(row["nm_id"]))
    return found



# --- сборка отчёта -----------------------------------------------------------


def build(
    client_id: int,
    period: str = "week",
    *,
    today: date | None = None,
    ads: AdSpend | None = None,
    path: str | Path | None = None,
) -> ProfitReport:
    """Прибыльность артикулов за период. В WB отсюда не ходят.

    Всё, кроме рекламы, уже лежит в базе: недели и строки отчёта положил
    агент 1, себестоимость загрузил сам селлер. Реклама приходит параметром,
    потому что за ней ходят через очередь, а не из расчёта.

    Недели берутся все, включая несверенные и обрезанные. Это решение, а не
    недосмотр: отказать в прибыли из-за того, что Wildberries не отдал итоги
    отчёта, значит оставить селлера вообще без ответа, а неточность тут
    меньше, чем молчание. Такие недели названы в `incomplete` и `unverified`,
    и отчёт говорит о них вслух.
    """
    date_from, date_to = finance.period_bounds(period, today)
    weeks = finance.weeks_of(client_id, date_from, date_to, path=path)
    # Разрез по артикулам один на проект, и живёт он у агента 1. Своей копии
    # здесь нет намеренно: два места, где решается, что такое «выручка
    # артикула» и «возврат», разошлись бы молча. Комиссия там уже считается по
    # `vw`, то есть та же, что в /finance.
    pieces = finance.articles_of(client_id, weeks, path=path)
    unknown = _articles_without_raw(client_id, weeks, path=path)
    ads = ads or AdSpend({}, available=False, reason=ADS_NOT_REQUESTED)

    named = [item for item in pieces if item.nm_id is not None]
    faceless = [item for item in pieces if item.nm_id is None]

    # Обезличка: расходы строк без артикула. Разносим пропорционально выручке,
    # потому что другого честного признака у этих строк нет. Правило названо
    # на листе «Методология», а не спрятано здесь.
    unallocated = sum((item.amounts.costs for item in faceless), ZERO)
    weights = {
        item.nm_id: item.amounts.revenue - item.amounts.returns_amount for item in named
    }
    parts = spread(unallocated, {k: v for k, v in weights.items() if v > ZERO})
    left = unallocated.quantize(CENT, rounding=ROUND_HALF_UP) - sum(parts.values(), ZERO)

    known = costs_module.costs_for(client_id, nm_ids=list(weights), path=path)

    rows: list[ArticleProfit] = []
    for item in named:
        amounts = item.amounts
        per_unit = known.get(item.nm_id)
        # Комиссия неизвестна - прибыль не считается. То же правило, что и для
        # себестоимости: показать число, собранное из другого поля, значит
        # соврать уверенным голосом.
        known_commission = item.nm_id not in unknown
        # Возвращённый товар вернулся на склад, его себестоимость не потрачена:
        # считаем по проданным штукам за вычетом возвращённых.
        net_units = max(amounts.sales_count - amounts.returns_count, 0)
        cost = None if per_unit is None else _money(per_unit) * net_units
        ad = ads.of(item.nm_id)
        piece = parts.get(item.nm_id, ZERO)
        net_revenue = amounts.revenue - amounts.returns_amount
        value = None
        if cost is not None and known_commission:
            value = (net_revenue - cost - amounts.costs - (ad or ZERO) - piece).quantize(
                CENT, rounding=ROUND_HALF_UP
            )
        rows.append(
            ArticleProfit(
                nm_id=item.nm_id,
                vendor_code=item.vendor_code,
                subject=item.subject,
                units=amounts.sales_count,
                returns_count=amounts.returns_count,
                revenue=amounts.revenue,
                returns_amount=amounts.returns_amount,
                cost_per_unit=None if per_unit is None else _money(per_unit),
                cost=None if cost is None else cost.quantize(CENT, rounding=ROUND_HALF_UP),
                commission=amounts.commission if known_commission else None,
                commission_known=known_commission,
                acquiring=amounts.acquiring,
                logistics=amounts.logistics,
                storage=amounts.storage,
                acceptance=amounts.acceptance,
                penalties=amounts.penalties,
                deductions=amounts.deductions,
                ad_spend=ad,
                unallocated=piece,
                profit=value,
                margin=None if value is None else margin(value, net_revenue),
            )
        )

    # Сортировка от самых прибыльных к убыточным. Артикулы без себестоимости
    # уходят в конец: прибыли у них нет, и места в этом ряду им не положено.
    rows.sort(key=lambda row: (row.profit is None, -(row.profit or ZERO), row.nm_id or 0))
    total = sum((row.profit for row in rows if row.profit is not None), ZERO)
    articles = tuple(
        row if row.profit is None else replace(row, share=share(row.profit, total))
        for row in rows
    )
    return ProfitReport(
        client_id=client_id,
        period=period,
        date_from=date_from,
        date_to=date_to,
        articles=articles,
        unallocated=unallocated,
        unallocated_left=left,
        ads=ads,
        weeks=tuple(weeks),
    )


# --- книга Excel -------------------------------------------------------------

ARTICLES_SHEET = "Прибыль по артикулам"
PROBLEMS_SHEET = "Убыточные и без данных"
METHOD_SHEET = "Методология"

NO_DATA = "нет данных"

ARTICLE_HEADERS = (
    "Артикул WB",
    "Артикул продавца",
    "Предмет",
    "Продано, шт",
    "Возвращено, шт",
    "Выручка, ₽",
    "Возвраты, ₽",
    "Себестоимость за штуку, ₽",
    "Себестоимость, ₽",
    "Комиссия WB, ₽",
    "Эквайринг, ₽",
    "Логистика, ₽",
    "Хранение, ₽",
    "Приёмка, ₽",
    "Штрафы, ₽",
    "Прочие удержания, ₽",
    "Реклама, ₽",
    "Расходы без артикула, ₽",
    "Чистая прибыль, ₽",
    "Маржинальность, %",
    "Доля в прибыли, %",
)

PROBLEM_HEADERS = ("Блок", "Артикул WB", "Артикул продавца", "Сумма, ₽", "Что это значит")

METHOD_HEADERS = ("Показатель", "Источник", "Формула", "Пояснение")

# Каждая строка: показатель, источник, формула, пояснение. По ней цифру можно
# проверить руками. Разнесение обезлички названо здесь прямым текстом:
# правило, о котором селлер не знает, для него хуже, чем отсутствие правила.
METHODOLOGY: tuple[tuple[str, str, str, str], ...] = (
    (
        "Выручка, возвраты, расходы WB",
        "таблицы fin_rows и fin_weeks (агент 1)",
        "суммы строк отчёта о реализации по nmId за недели периода",
        "В Wildberries за этим отчёт не ходит: недели уже выгружены агентом "
        "«Финансист», цифры те же, что на его листе «По артикулам».",
    ),
    (
        "Комиссия WB, ₽",
        "поле vw строки отчёта о реализации",
        "сумма vw по строкам артикула, включая возвраты",
        "Разрез по артикулам общий с отчётом «Финансы», считает его агент 1: "
        "суммы по артикулам складываются в комиссию недели. Соседнее поле "
        "ppvzSalesCommission (вознаграждение с продаж до вычета услуг "
        "поверенного) это другие деньги, и подставлять его вместо vw нельзя. "
        "Если строка сохранена без исходных полей WB, комиссия считается "
        "неизвестной и прибыль по артикулу не считается.",
    ),
    (
        "Себестоимость, ₽",
        "файл себестоимости, команда /costs",
        "себестоимость за штуку * (продано - возвращено)",
        "Возвращённый товар вернулся на склад, его себестоимость не "
        "потрачена. Артикул без загруженной себестоимости попадает в блок "
        "«нет себестоимости», и прибыль по нему не считается.",
    ),
    (
        "Реклама, ₽",
        "GET /adv/v3/fullstats, категория токена «Продвижение»",
        "сумма days[].apps[].nms[].sum по артикулу",
        "Ограничения Wildberries: не больше 50 кампаний и 31 дня за запрос, "
        "3 запроса в минуту. Без категории «Продвижение» в колонке стоит "
        "«нет данных», а не ноль, и прибыль считается без рекламы.",
    ),
    (
        "Расходы без артикула, ₽",
        "строки отчёта о реализации без nmId",
        "разносятся пропорционально выручке артикула за вычетом возвратов",
        "Это правило, а не факт от Wildberries. Артикулы без выручки доли не "
        "получают. Если выручки нет вовсе, расходы остаются в блоке "
        "«обезличка» и в прибыль не входят.",
    ),
    (
        "Чистая прибыль, ₽",
        "все источники выше",
        "выручка - возвраты - себестоимость - расходы WB - реклама - "
        "расходы без артикула",
        "Расходы WB это комиссия, эквайринг, логистика, хранение, приёмка, "
        "штрафы и прочие удержания.",
    ),
    (
        "Маржинальность, %",
        "-",
        "чистая прибыль / (выручка - возвраты) * 100",
        "Без выручки маржинальности не существует, в таких строках стоит "
        "«нет данных», а не ноль.",
    ),
    (
        "Доля в прибыли, %",
        "-",
        "чистая прибыль артикула / прибыль периода * 100",
        "У убыточного артикула доля отрицательная, поэтому сумма долей "
        "прибыльных может быть больше ста процентов: убытки съедают часть "
        "заработанного.",
    ),
    (
        "Полнота недель",
        "fin_weeks.control_payload (агент 1)",
        "-",
        "Прибыль считается и по несверенным, и по обрезанным неделям: "
        "молчание было бы хуже неточности. Такие недели названы в сообщении "
        "к отчёту.",
    ),
)


def _cell(value: Any) -> Any:
    """Деньги с копейками, а «неизвестно» словами, а не пустой клеткой."""
    if value is None:
        return NO_DATA
    if isinstance(value, Decimal):
        return value.quantize(CENT, rounding=ROUND_HALF_UP)
    return value


def excel_sheets(report: ProfitReport) -> list:
    """Три листа книги: полная таблица, проблемный блок и методология."""
    from core.xlsx import Sheet

    articles = [
        [
            item.nm_id,
            item.vendor_code,
            item.subject,
            item.units,
            item.returns_count,
            _cell(item.revenue),
            _cell(item.returns_amount),
            _cell(item.cost_per_unit),
            _cell(item.cost),
            _cell(item.commission),
            _cell(item.acquiring),
            _cell(item.logistics),
            _cell(item.storage),
            _cell(item.acceptance),
            _cell(item.penalties),
            _cell(item.deductions),
            _cell(item.ad_spend),
            _cell(item.unallocated),
            _cell(item.profit),
            _cell(item.margin),
            _cell(item.share),
        ]
        for item in report.articles
    ]

    problems = [
        [
            "убыточный",
            item.nm_id,
            item.vendor_code,
            _cell(item.profit),
            "Расходы за период больше выручки. Смотрите, что именно съело "
            "прибыль: реклама, логистика или себестоимость.",
        ]
        for item in report.losses
    ]
    problems += [
        [
            "комиссия неизвестна",
            item.nm_id,
            item.vendor_code,
            _cell(item.revenue),
            "Строка отчёта сохранена без исходных полей Wildberries, поэтому "
            "комиссию взять неоткуда. Прибыль по этому артикулу не посчитана. "
            "Запросите период заново, и строки перезапишутся.",
        ]
        for item in report.without_commission
    ]
    problems += [
        [
            "нет себестоимости",
            item.nm_id,
            item.vendor_code,
            _cell(item.revenue),
            "Прибыль по этому артикулу не посчитана. Пришлите себестоимость "
            "командой /costs, и он встанет в общий ряд.",
        ]
        for item in report.without_cost
    ]
    if report.unallocated > ZERO:
        problems.append(
            [
                "обезличка",
                None,
                "",
                _cell(report.unallocated),
                "Расходы из строк отчёта без артикула. Разнесены по артикулам "
                "пропорционально выручке; не разнесённый остаток "
                f"{_cell(report.unallocated_left)} ₽ в прибыль не вошёл.",
            ]
        )

    return [
        Sheet(ARTICLES_SHEET, ARTICLE_HEADERS, articles),
        Sheet(PROBLEMS_SHEET, PROBLEM_HEADERS, problems),
        Sheet(METHOD_SHEET, METHOD_HEADERS, [list(row) for row in METHODOLOGY]),
    ]


def excel_bytes(report: ProfitReport) -> bytes:
    """Книга целиком, байтами: её чаще отправляют, чем сохраняют."""
    from core.xlsx import write_book

    return write_book(excel_sheets(report))


def file_name(report: ProfitReport) -> str:
    return f"profit-{report.date_from.isoformat()}-{report.date_to.isoformat()}.xlsx"


# --- очередь -----------------------------------------------------------------

_sender: Callable[[int, ProfitReport, bytes], Any] | None = None


def set_sender(fn: Callable[[int, ProfitReport, bytes], Any] | None) -> None:
    """Чем отдаётся готовый отчёт: `fn(client_id, report, xlsx)`."""
    global _sender
    _sender = fn


def request_report(
    client_id: int, period: str = "week", *, path: str | Path | None = None
) -> queue.TaskId:
    """Ставит отчёт в очередь. «Принято» клиенту говорит сама очередь.

    Из хендлера в WB не ходят: за рекламой идёт обработчик задачи, и повтор
    при недоступности Wildberries живёт только в очереди.
    """
    if period not in PERIODS:
        raise ValueError(f"неизвестный период: {period}")
    return queue.enqueue(client_id, TASK_KIND, {"period": period}, path=path)


async def report_task(task: Any, *, path: str | Path | None = None) -> ProfitReport:
    """Обработчик задачи: сходить за рекламой, собрать отчёт, отдать клиенту."""
    client_id = int(task.client_id)
    period = str((task.payload or {}).get("period") or "week")
    date_from, date_to = finance.period_bounds(period)
    ads = await collect_ads(client_id, date_from, date_to, path=path)
    report = build(client_id, period, ads=ads, path=path)
    if _sender is None:
        raise RuntimeError("некому отправить отчёт о прибыли: доставка не подключена")
    result = _sender(client_id, report, excel_bytes(report))
    if hasattr(result, "__await__"):
        await result
    return report


def register_jobs() -> None:
    """Связывает вид задачи с обработчиком. Зовёт сборка бота, не импорт."""
    queue.register(TASK_KIND, report_task)
