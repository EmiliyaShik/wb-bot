"""Агент 4. План-факт: накопление суток, утренний отчёт, остатки.

Главное ограничение, вокруг которого построен весь модуль: историю воронки
Wildberries отдаёт **максимум за последнюю неделю**. Поэтому суточные данные
забираются каждый день и копятся в `nm_daily`; месяц назад их уже никто не
спросит. Из этого следует второе: **сбор идёт у всех подключённых кабинетов,
независимо от подписки.** Выключенная рассылка это «не присылай отчёт», а не
«не копи историю»: потерянную историю не восстановить, а доступ к модулю
клиент может оформить и завтра.

Деньги внутри только в копейках (целые) и в Decimal (рубли). Нигде нет
деления на ноль: ни при нулевой выручке (ДРР), ни при нулевой скорости
продаж (дни остатка), ни в первый день месяца (прогноз) - в каждом таком
месте возвращается None или сам факт, а не исключение.

Текстов здесь нет. Отчёт это данные (`RnpReport`), а во что их превратить,
знает `bot/handlers/rnp.py`.
"""

from __future__ import annotations

import calendar
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from core import access, config, db, queue, scheduler, wbapi

logger = logging.getLogger(__name__)

MODULE = "rnp"

# Виды задач. Первые две ставит расписание (одна на всех), вторые две -
# разбор по клиентам: так недоступность WB у одного клиента не отменяет
# работу остальных, а повтор достаётся ровно тому, кто упал.
COLLECT_ALL = "rnp_collect"
REPORT_ALL = "rnp_report"
COLLECT_ONE = "rnp_collect_client"
REPORT_ONE = "rnp_report_client"

# Сколько суток забираем за один заход. Больше недели WB всё равно не даст,
# а окно (а не один вчерашний день) закрывает дыры после простоя бота.
WINDOW_DAYS = 7
# На скольких накопленных сутках считается скорость продаж. По одному дню
# её считать нельзя: случайный всплеск превратился бы в «завтра кончится».
SPEED_DAYS = 14
# Порог из ТЗ: товар в отчёте, если остатка хватит меньше чем на столько дней.
STOCK_ALERT_DAYS = 14
# Сколько артикулов показываем в блоке остатков.
STOCK_LIMIT = 10
# Сколько артикулов забирать суточной воронкой, если в config.toml про это
# не сказано ничего. Число настраивает владелец, секция [funnel].
FUNNEL_ARTICLES_DEFAULT = 200

CENT = Decimal("0.01")


# --- данные отчёта ---


@dataclass(frozen=True)
class Plan:
    """План на месяц. Любая из целей может быть не задана."""

    year_month: str
    revenue: Decimal | None = None
    orders: int | None = None

    @property
    def is_set(self) -> bool:
        return self.revenue is not None or self.orders is not None


@dataclass(frozen=True)
class StockRisk:
    """Артикул, которому осталось меньше двух недель при текущем темпе."""

    nm_id: int
    stock: int
    per_day: Decimal
    days: int


@dataclass(frozen=True)
class RnpReport:
    """Всё, что нужно утреннему отчёту. Без текста и без форматирования."""

    client_id: int
    date: date
    has_data: bool
    orders: int = 0
    revenue: Decimal = Decimal("0")
    avg_orders: Decimal | None = None
    avg_revenue: Decimal | None = None
    avg_days: int = 0
    month_orders: int = 0
    month_revenue: Decimal = Decimal("0")
    days_passed: int = 0
    days_in_month: int = 30
    plan: Plan | None = None
    ad_spend: Decimal = Decimal("0")
    risks: tuple[StockRisk, ...] = ()

    @property
    def drr(self) -> Decimal | None:
        return drr(self.ad_spend, self.revenue)

    @property
    def forecast_revenue(self) -> Decimal:
        return forecast(self.month_revenue, self.days_passed, self.days_in_month)

    @property
    def forecast_orders(self) -> int:
        value = forecast(Decimal(self.month_orders), self.days_passed, self.days_in_month)
        return int(value.to_integral_value())

    @property
    def revenue_percent(self) -> Decimal | None:
        return percent(self.month_revenue, self.plan.revenue if self.plan else None)

    @property
    def orders_percent(self) -> Decimal | None:
        target = Decimal(self.plan.orders) if self.plan and self.plan.orders else None
        return percent(Decimal(self.month_orders), target)


# --- чистые расчёты ---


def drr(spend: Decimal, revenue: Decimal) -> Decimal | None:
    """Доля рекламных расходов: расход / выручка * 100.

    Без выручки доли не существует, и это не ноль: делить на ноль нельзя,
    а «ДРР 0%» прочиталось бы как «реклама бесплатна».
    """
    if revenue is None or Decimal(revenue) <= 0:
        return None
    return (Decimal(spend) * 100 / Decimal(revenue)).quantize(CENT)


def forecast(fact: Decimal, days_passed: int, days_in_month: int) -> Decimal:
    """Сколько выйдет к концу месяца, если темп не изменится.

    В первый день месяца прошедших дней ноль, темпа ещё нет: возвращается
    сам факт, а не деление на ноль.
    """
    fact = Decimal(fact)
    if days_passed <= 0:
        return fact
    return (fact / Decimal(days_passed) * Decimal(days_in_month)).quantize(CENT)


def stock_days(stock: int | None, per_day: Decimal) -> int | None:
    """На сколько дней хватит остатка. Без продаж срок неизвестен, а не бесконечен."""
    if stock is None or per_day is None or Decimal(per_day) <= 0:
        return None
    return int(Decimal(stock) / Decimal(per_day))


def percent(fact: Decimal, target: Decimal | None) -> Decimal | None:
    """Процент выполнения. Без цели процента нет."""
    if target is None or Decimal(target) <= 0:
        return None
    return (Decimal(fact) * 100 / Decimal(target)).quantize(CENT)


# --- разбор ответов WB ---
#
# Имя артикула у Wildberries пишется по-разному в разных методах: nmID в
# карточках, nmId в рекламе и остатках. Разбор собран в одном месте, чтобы
# эта разница не расползлась по расчётам.

_NM_KEYS = ("nmID", "nmId", "nm_id", "nm")


def _nm_id(row: dict) -> int | None:
    for source in (row, row.get("product") if isinstance(row.get("product"), dict) else {}):
        for key in _NM_KEYS:
            value = (source or {}).get(key)
            if value not in (None, ""):
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return None
    return None


def _day_of(item: dict) -> str:
    """Дата строки WB в виде ГГГГ-ММ-ДД. Время, если оно есть, отбрасывается."""
    raw = item.get("date") or item.get("dt") or ""
    return str(raw)[:10]


def _int(value: Any) -> int:
    try:
        return int(Decimal(str(value or 0)))
    except Exception:  # noqa: BLE001 - чужое поле не должно ронять сбор
        return 0


def _kop(value: Any) -> int:
    try:
        return db.to_kop(Decimal(str(value or 0)))
    except Exception:  # noqa: BLE001 - чужое поле не должно ронять сбор
        return 0


def _real(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _funnel_rows(products: Iterable[dict]) -> dict[tuple[str, int], dict[str, Any]]:
    """Воронка по дням -> строки `nm_daily`, ключ (дата, артикул)."""
    collected: dict[tuple[str, int], dict[str, Any]] = {}
    for product in products:
        nm_id = _nm_id(product)
        if nm_id is None:
            continue
        history = product.get("history") or product.get("statistics") or []
        for item in history if isinstance(history, list) else []:
            if not isinstance(item, dict):
                continue
            day = _day_of(item)
            if not day:
                continue
            collected[(day, nm_id)] = {
                "orders": _int(item.get("orderCount")),
                "orders_sum_kop": _kop(item.get("orderSum")),
                "buyouts": _int(item.get("buyoutCount")),
                "buyouts_sum_kop": _kop(item.get("buyoutSum")),
                "open_card_count": _int(item.get("openCount")),
                "add_to_cart_count": _int(item.get("cartCount")),
                "cart_to_order_pct": _real(item.get("cartToOrderConversion")),
                "buyout_pct": _real(item.get("buyoutPercent")),
                "raw": json.dumps(item, ensure_ascii=False),
            }
    return collected


def _stock_totals(items: Iterable[dict]) -> dict[int, int]:
    """Остаток по артикулу: сумма по складам и размерам.

    Поля `quantityFull` в новом методе нет, ближайшее по смыслу это
    `quantity + inWayToClient + inWayFromClient`. Разрез по складам не нужен:
    считаем, на сколько дней хватит товара вообще.
    """
    totals: dict[int, int] = {}
    for item in items:
        nm_id = _nm_id(item)
        if nm_id is None:
            continue
        totals[nm_id] = totals.get(nm_id, 0) + (
            _int(item.get("quantity"))
            + _int(item.get("inWayToClient"))
            + _int(item.get("inWayFromClient"))
        )
    return totals


def _ad_totals(campaigns: Iterable[dict]) -> dict[tuple[str, int], dict[str, int]]:
    """Расход, показы и клики по (дата, артикул). Суммируется по площадкам."""
    totals: dict[tuple[str, int], dict[str, int]] = {}
    for campaign in campaigns:
        for day_row in campaign.get("days") or []:
            if not isinstance(day_row, dict):
                continue
            day = _day_of(day_row)
            for app in day_row.get("apps") or []:
                for item in (app or {}).get("nms") or []:
                    nm_id = _nm_id(item)
                    if nm_id is None or not day:
                        continue
                    slot = totals.setdefault(
                        (day, nm_id), {"ad_spend_kop": 0, "views": 0, "clicks": 0}
                    )
                    slot["ad_spend_kop"] += _kop(item.get("sum"))
                    slot["views"] += _int(item.get("views"))
                    slot["clicks"] += _int(item.get("clicks"))
    return totals


# --- какие артикулы спрашивать ---


def funnel_articles_limit() -> int:
    """Сколько артикулов забирать суточной воронкой. Число из `config.toml`.

    Ноль и меньше значат «без предела»: у кабинета на две тысячи артикулов
    это больше получаса машинного времени в сутки, и решает это владелец,
    а не код.
    """
    try:
        section = config.settings().get("funnel") or {}
        return int(section.get("daily_articles", FUNNEL_ARTICLES_DEFAULT))
    except (KeyError, TypeError, ValueError, OSError):
        return FUNNEL_ARTICLES_DEFAULT


def _period_revenue_kop(product: dict) -> int:
    """Выручка артикула за период из свода воронки, в копейках."""
    statistic = product.get("statistic")
    selected = (statistic or {}).get("selected") if isinstance(statistic, dict) else None
    return _kop((selected or {}).get("orderSum")) if isinstance(selected, dict) else 0


def _ranked_articles(products: Iterable[dict]) -> list[int]:
    """Артикулы свода воронки: сначала те, что продаются.

    Порядок важен из-за предела: подряд первыми ушли бы случайные артикулы,
    а про товар, который приносит деньги, знать нужнее. При равной выручке
    порядок по номеру, чтобы от захода к заходу собирались одни и те же.
    """
    revenue: dict[int, int] = {}
    for product in products:
        nm_id = _nm_id(product)
        if nm_id is None:
            continue
        revenue[nm_id] = max(revenue.get(nm_id, 0), _period_revenue_kop(product))
    return [nm_id for nm_id, _ in sorted(revenue.items(), key=lambda item: (-item[1], item[0]))]


def _articles_from_history(
    client_id: int, last: date, *, days: int = SPEED_DAYS, path: str | Path | None = None
) -> list[int]:
    """Запасной источник: артикулы, которые уже встречались в собранных сутках.

    Нужен, когда свод воронки не ответил. Терять из-за одного неудачного
    запроса весь суточный сбор нельзя: WB отдаёт историю максимум за неделю,
    и не забранное сегодня не вернуть никогда.
    """
    repo = db.repo(client_id, path)
    orders: dict[int, int] = {}
    for stamp in _days_back(last, days):
        for row in repo.rows("nm_daily", date=stamp):
            nm_id = int(row["nm_id"])
            orders[nm_id] = orders.get(nm_id, 0) + int(row["orders"] or 0)
    return [nm_id for nm_id, _ in sorted(orders.items(), key=lambda item: (-item[1], item[0]))]


async def _funnel_articles(
    client: Any,
    client_id: int,
    start: date,
    last: date,
    *,
    known: Iterable[int] = (),
    path: str | Path | None = None,
) -> list[int]:
    """Список артикулов для суточной воронки, в порядке отбора.

    Первый источник это свод воронки за то же окно: он лежит в той же
    категории токена «Аналитика», что и сама суточная воронка, то есть
    новых требований к кабинету не создаёт, берёт до тысячи артикулов за
    один запрос и заодно говорит, что у селлера продаётся.

    Каталог карточек (категория «Контент») сюда не годится по той же
    причине, по какой годится свод: категории «Контент» у клиента может не
    быть вовсе, а без суточной воронки остаётся весь план-факт. Если свод
    не ответил, идут запасные источники: уже собранные нами сутки, а если и
    их нет (первый день кабинета), артикулы из остатков, за которыми мы всё
    равно только что сходили.
    """
    ranked: list[int] = []
    try:
        ranked = _ranked_articles(await client.sales_funnel_products(start, last))
    except (
        wbapi.WBForbiddenError,
        wbapi.WBUnavailable,
        wbapi.WBRateLimited,
        wbapi.WBApiError,
    ) as error:
        logger.warning(
            "свод воронки клиента %s не получен, беру артикулы из собранного: %s",
            client_id,
            error,
        )
    if not ranked:
        ranked = _articles_from_history(client_id, last, path=path)
    if not ranked:
        ranked = sorted({int(value) for value in known})
    limit = funnel_articles_limit()
    if limit > 0 and len(ranked) > limit:
        logger.info(
            "у клиента %s артикулов больше предела (%s из %s), беру самые оборотистые",
            client_id,
            limit,
            len(ranked),
        )
        return ranked[:limit]
    return ranked


# --- сбор суток ---


def yesterday(today: date | None = None) -> date:
    """Вчера по часовому поясу расписания. Своего разбора пояса тут нет."""
    today = today or datetime.now(scheduler.tz()).date()
    return today - timedelta(days=1)


async def collect(
    client_id: int,
    *,
    day: date | None = None,
    window: int = WINDOW_DAYS,
    http: Any = None,
    path: str | Path | None = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], Any] | None = None,
) -> int:
    """Забирает сутки из WB и складывает их в `nm_daily`. Возвращает число строк.

    Забирается не один вчерашний день, а окно в неделю: WB больше недели всё
    равно не отдаёт, а окно само закрывает дыры, если бот сутки не работал.
    Повторный заход за тот же день ничего не портит, строка перезаписывается.

    Воронка по дням спрашивается по списку артикулов (у метода WB это
    обязательное поле) пачками по двадцать, и на большом кабинете сбор идёт
    минутами. Предел на число артикулов живёт в `config.toml`, секция
    `[funnel]`.
    """
    day = day or yesterday()
    start = day - timedelta(days=max(1, int(window)) - 1)
    client = wbapi.get_wb_client(client_id, http=http, path=path, clock=clock, sleep=sleep)

    # Остатки идут первыми не случайно: этот запрос делается всё равно, а
    # его артикулы годятся в самый последний запасной список для воронки.
    stocks = _stock_totals(await client.stocks_wb_warehouses())

    # Суточную воронку Wildberries не отдаёт без артикулов: nmIds у метода
    # обязателен. Откуда их взять и сколько взять, решает _funnel_articles.
    nm_ids = await _funnel_articles(client, client_id, start, day, known=stocks, path=path)
    rows: dict[tuple[str, int], dict[str, Any]] = {}
    if nm_ids:
        rows = _funnel_rows(await client.sales_funnel_history(start, day, nm_ids))
    else:
        logger.warning("у клиента %s не нашлось ни одного артикула для воронки", client_id)

    # Реклама это отдельная категория токена. Её может не быть, и тогда
    # пропадает только расход: терять из-за этого всю историю нельзя.
    ads: dict[tuple[str, int], dict[str, int]] = {}
    try:
        ids = await client.advert_ids()
        if ids:
            ads = _ad_totals(await client.fullstats(ids, start, day))
    except wbapi.WBForbiddenError:
        logger.info("у клиента %s нет категории «Продвижение», расход рекламы пропущен", client_id)

    for key, values in ads.items():
        rows.setdefault(key, {}).update(values)

    # Остаток это снимок на момент сбора, а не история: он пишется в строку
    # последнего собранного дня. Артикул без заказов тоже попадает в базу,
    # иначе о залежавшемся товаре никто бы не узнал.
    stamp = day.isoformat()
    for nm_id, total in stocks.items():
        rows.setdefault((stamp, nm_id), {})["stocks_wb"] = total

    repo = db.repo(client_id, path)
    for (stamp, nm_id), values in rows.items():
        repo.upsert("nm_daily", {"date": stamp, "nm_id": nm_id}, **values)
    return len(rows)


# --- план на месяц ---


def year_month(moment: date) -> str:
    """Месяц в том виде, в каком он лежит в таблице plans: ГГГГ-ММ."""
    return f"{moment.year:04d}-{moment.month:02d}"


def set_plan(
    client_id: int,
    month: str,
    *,
    revenue: Decimal | None = None,
    orders: int | None = None,
    path: str | Path | None = None,
) -> Plan:
    """Ставит план на месяц. Повторный вызов переписывает прежний."""
    db.repo(client_id, path).upsert(
        "plans",
        {"year_month": month},
        revenue_target_kop=None if revenue is None else db.to_kop(Decimal(revenue)),
        orders_target=None if orders is None else int(orders),
    )
    return Plan(month, revenue, None if orders is None else int(orders))


def plan_of(client_id: int, month: str, *, path: str | Path | None = None) -> Plan | None:
    """План месяца или None. None это «плана нет», а не ноль: ноль был бы целью."""
    row = db.repo(client_id, path).one("plans", year_month=month)
    if row is None:
        return None
    revenue = row["revenue_target_kop"]
    orders = row["orders_target"]
    plan = Plan(
        month,
        None if revenue is None else db.from_kop(revenue),
        None if orders is None else int(orders),
    )
    return plan if plan.is_set else None


# --- утренний отчёт ---


def _days_back(last: date, count: int) -> list[str]:
    return [(last - timedelta(days=shift)).isoformat() for shift in range(count)]


def _sum(rows: Iterable[Any], column: str) -> int:
    return sum(int(row[column] or 0) for row in rows)


def _speed_and_stock(
    by_day: dict[str, list[Any]], days: Sequence[str]
) -> dict[int, tuple[Decimal, int | None]]:
    """Скорость продаж и остаток по каждому артикулу за накопленные сутки.

    Скорость это заказы за собранные дни, делённые на число этих дней, а не
    вчерашние заказы: один удачный день иначе объявил бы товар кончающимся.
    Остаток берётся самый свежий известный, старее он не становится.
    """
    orders: dict[int, int] = {}
    seen: dict[int, int] = {}
    stock: dict[int, int] = {}
    for stamp in days:  # дни идут от свежего к старому
        for row in by_day.get(stamp, []):
            nm_id = int(row["nm_id"])
            orders[nm_id] = orders.get(nm_id, 0) + int(row["orders"] or 0)
            seen[nm_id] = seen.get(nm_id, 0) + 1
            if row["stocks_wb"] is not None and nm_id not in stock:
                stock[nm_id] = int(row["stocks_wb"])
    return {
        nm_id: (Decimal(orders[nm_id]) / Decimal(count), stock.get(nm_id))
        for nm_id, count in seen.items()
    }


def daily(
    client_id: int,
    *,
    today: date | None = None,
    path: str | Path | None = None,
) -> RnpReport:
    """Утренний отчёт по накопленным суткам. В WB этот вызов не ходит.

    Всё, что нужно отчёту, уже лежит в `nm_daily`: за месяц назад WB историю
    воронки не отдаст, поэтому единственный источник тут наш собственный.
    """
    today = today or datetime.now(scheduler.tz()).date()
    target = today - timedelta(days=1)
    repo = db.repo(client_id, path)

    month_start = today.replace(day=1)
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    # Вчера может оказаться прошлым месяцем: первого числа факта ещё нет.
    days_passed = target.day if target >= month_start else 0

    wanted = set(_days_back(target, max(SPEED_DAYS, WINDOW_DAYS + 1)))
    wanted.update(
        (month_start + timedelta(days=shift)).isoformat() for shift in range(days_passed)
    )
    by_day = {stamp: repo.rows("nm_daily", date=stamp) for stamp in sorted(wanted, reverse=True)}

    day_rows = by_day.get(target.isoformat(), [])

    # Среднее берётся по неделе ДО вчера и делится на дни, за которые данные
    # правда есть: в первую неделю работы деление на семь занизило бы его.
    week = _days_back(target - timedelta(days=1), WINDOW_DAYS)
    week_days = [stamp for stamp in week if by_day.get(stamp)]
    week_rows = [row for stamp in week_days for row in by_day[stamp]]
    avg_orders = avg_revenue = None
    if week_days:
        avg_orders = (Decimal(_sum(week_rows, "orders")) / len(week_days)).quantize(CENT)
        avg_revenue = (
            db.from_kop(_sum(week_rows, "orders_sum_kop")) / len(week_days)
        ).quantize(CENT)

    month_rows = [
        row
        for shift in range(days_passed)
        for row in by_day.get((month_start + timedelta(days=shift)).isoformat(), [])
    ]

    risks = []
    for nm_id, (per_day, stock) in _speed_and_stock(by_day, _days_back(target, SPEED_DAYS)).items():
        left = stock_days(stock, per_day)
        if left is not None and left < STOCK_ALERT_DAYS:
            risks.append(StockRisk(nm_id, int(stock or 0), per_day.quantize(CENT), left))
    risks.sort(key=lambda risk: (risk.days, risk.nm_id))

    return RnpReport(
        client_id=client_id,
        date=target,
        has_data=bool(day_rows),
        orders=_sum(day_rows, "orders"),
        revenue=db.from_kop(_sum(day_rows, "orders_sum_kop")),
        avg_orders=avg_orders,
        avg_revenue=avg_revenue,
        avg_days=len(week_days),
        month_orders=_sum(month_rows, "orders"),
        month_revenue=db.from_kop(_sum(month_rows, "orders_sum_kop")),
        days_passed=days_passed,
        days_in_month=days_in_month,
        plan=plan_of(client_id, year_month(today), path=path),
        ad_spend=db.from_kop(_sum(day_rows, "ad_spend_kop")),
        risks=tuple(risks[:STOCK_LIMIT]),
    )


# --- задачи расписания и очереди ---


def _payload_date(task: Any) -> date:
    raw = str((getattr(task, "payload", None) or {}).get("date") or "")
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return datetime.now(scheduler.tz()).date()


def _connected_clients(path: str | Path | None = None) -> list[int]:
    """Клиенты с подключённым кабинетом. Подписка тут ни при чём."""
    found: list[int] = []
    for row in db.admin_repo(path).all_clients():
        client_id = int(row["id"])
        try:
            if db.repo(client_id, path).count("wb_tokens"):
                found.append(client_id)
        except Exception:  # noqa: BLE001 - один клиент не ломает обход
            logger.exception("не удалось проверить кабинет клиента %s", client_id)
    return found


def fan_out_collect(task: Any, *, path: str | Path | None = None) -> list[int]:
    """Утренний сбор: по задаче на каждый подключённый кабинет.

    **Доступ к модулю здесь не проверяется, и это не упущение.** История
    воронки живёт у WB одну неделю: не собранное сегодня пропадёт навсегда,
    а подписку клиент может оформить и через месяц.
    """
    target = _payload_date(task) - timedelta(days=1)
    return [
        queue.enqueue(
            client_id, COLLECT_ONE, {"date": target.isoformat()}, notify=False, path=path
        )
        for client_id in _connected_clients(path)
    ]


def fan_out_report(task: Any, *, path: str | Path | None = None) -> list[int]:
    """Утренняя рассылка: только тем, у кого модуль работает."""
    stamp = _payload_date(task).isoformat()
    return [
        queue.enqueue(client_id, REPORT_ONE, {"date": stamp}, notify=False, path=path)
        for client_id in _connected_clients(path)
        if access.has_access(client_id, MODULE, path=path)
    ]


def request_report(
    client_id: int, *, day: date | None = None, path: str | Path | None = None
) -> queue.TaskId:
    """Отчёт по требованию: задача в очередь, а не поход в WB из хендлера.

    «Принято, пришлю, когда будет готово» говорит сама очередь.
    """
    day = day or datetime.now(scheduler.tz()).date()
    return queue.enqueue(client_id, REPORT_ONE, {"date": day.isoformat()}, path=path)


async def collect_client(task: Any, *, path: str | Path | None = None) -> int:
    """Сбор одного кабинета. Повтор при недоступности WB делает очередь."""
    return await collect(task.client_id, day=_payload_date(task), path=path)


_delivery: Callable[[int, RnpReport], Any] | None = None


def set_delivery(fn: Callable[[int, RnpReport], Any] | None) -> None:
    """Чем отчёт уходит клиенту. Ставит поверхность бота: текстов здесь нет."""
    global _delivery
    _delivery = fn


async def report_client(task: Any, *, path: str | Path | None = None) -> RnpReport:
    """Собирает утренний отчёт и отдаёт его тому, кто умеет писать клиенту."""
    report = daily(task.client_id, today=_payload_date(task), path=path)
    if _delivery is None:
        logger.warning("отчёт клиента %s некому отправить", task.client_id)
        return report
    result = _delivery(task.client_id, report)
    if hasattr(result, "__await__"):
        await result
    return report


def register_jobs() -> None:
    """Ставит сбор в расписание, а разбор по клиентам в очередь.

    Утреннюю рассылку отсюда не регистрируем: её ставит agents.lifecycle
    под своим именем, потому что кому и в котором часу слать, решают
    тумблеры и время клиента из /settings, а не агент. Сбор наоборот
    остаётся здесь и идёт у всех подключённых кабинетов.
    """
    scheduler.register_daily(COLLECT_ALL, fan_out_collect)
    queue.register(COLLECT_ONE, collect_client, quiet=True)
    # Суточный план-факт ставит и расписание, и сам клиент командой. Молчать
    # о его сбое нельзя: в обоих случаях селлер ждёт этот разбор утром.
    queue.register(REPORT_ONE, report_client, title="план-факт")
