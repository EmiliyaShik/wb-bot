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

Четвёртый источник не обязателен и на цифры не влияет: названия карточек
(`POST /content/v2/get/cards/list`, категория токена «Контент»). В отчёте о
реализации названия нет вовсе, там только артикулы, бренд и предмет, а
предмет это категория («Наматрасник»), а не название товара. Названия лежат
в `card_names` и обновляются в двух случаях: в периоде появился артикул, о
котором мы ещё не спрашивали, или известное название протухло по сроку из
`config.toml` (секция `[cards]`). Без срока переименование карточки в
кабинете не доехало бы до отчёта никогда.

Два случая нехватки данных штатные, а не ошибки, и оба видны в отчёте.

**Нет себестоимости** по артикулу: прибыль по нему не считается, он уходит в
блок «нет себестоимости», остальное считается, и `/profit` работает (R168).

**Нет категории «Продвижение»** или `fullstats` недоступен: колонка «реклама»
помечается «нет данных», прибыль считается без неё, и в отчёте про это
написано честно (R112.1).

**Компенсация это не продажа.** Возмещение за товар, который покупатель
вернул или потерял, Wildberries отдаёт строкой типа «Продажа» с нулевым
`retailAmount`: деньги приходят, товар выбывает. Такая строка не даёт ни
выручки, ни проданных штук, иначе товар, которого нет на остатках, выглядит
проданным. Деньги при этом остаются видимыми: они в «к перечислению» отчёта
«Финансы», а каждая компенсация названа по имени в листе «Убыточные и без
данных». Себестоимость выбывшего товара в прибыль не списывается и показана
рядом с компенсацией; почему именно так - на листе «Методология».

Пятый источник приходит не из Wildberries, а от самого селлера: режим
налогообложения и ставка из `/settings` (`core.tax`). База «доходов» это то,
что заплатил покупатель, а не то, что перечислил Wildberries. Режим не
выбран - налог не считается вовсе, колонок под него в книге нет, и отчёт
говорит, что прибыль показана до налога: молчаливый ноль тут хуже
отсутствующей колонки.

Налог стоит колонкой у каждого товара, потому что владельцу нужна прибыль по
каждому артикулу. Два режима при этом ведут себя по-разному, и подпись
колонки у них разная. На «доходах» налог товара это ставка от его выручки за
вычетом возвратов: точная величина этого товара, и колонка называется
«Налог, ₽». На «доходы минус расходы» база считается по кабинету целиком, и
разложить её по товарам можно только делением по выручке, поэтому колонка
называется «Налог, наша раскладка, ₽» и раскладкой же названа в
«Методологии». Считает всё `core.tax.allocate`, здесь только подписи.

Деньги везде `Decimal`, в базе целые копейки. `float` остаётся только
процентам: маржинальность и доля в прибыли, как и в схеме базы.

Текстов бота здесь нет. Отчёт это данные (`ProfitReport`), а во что их
превратить, знает `bot/handlers/profit.py`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from agents import finance
from core import costs as costs_module
from core import tax as tax_module
from core import audit, config, db, queue, wbapi

logger = logging.getLogger(__name__)

__all__ = [
    "MODULE",
    "TASK_KIND",
    "PERIODS",
    "TOP_SIZE",
    "ARTICLES_SHEET",
    "PROBLEMS_SHEET",
    "TAX_SHEET",
    "TAX_HEADERS",
    "METHOD_SHEET",
    "ADS_OK",
    "ADS_NO_CATEGORY",
    "ADS_UNAVAILABLE",
    "ADS_NOT_REQUESTED",
    "CARDS_TABLE",
    "COST_ITEMS",
    "NOT_SUMMABLE",
    "TOTAL_LABEL",
    "TAX_NOTE_LABEL",
    "TAX_OFF_LABEL",
    "TAX_UNPRICED_LABEL",
    "BEFORE_TAX",
    "COMPENSATION_BLOCK",
    "PROFIT_COLUMN",
    "MARGIN_COLUMN",
    "TAX_COLUMN",
    "TAX_SPREAD_COLUMN",
    "AFTER_TAX_COLUMN",
    "tax_columns",
    "article_columns",
    "tax_total_rows",
    "AdSpend",
    "ArticleProfit",
    "CostItem",
    "ProfitReport",
    "Totals",
    "margin",
    "share",
    "spread",
    "ad_totals",
    "collect_ads",
    "card_titles",
    "names_of",
    "names_ttl_days",
    "remember_names",
    "collect_names",
    "with_names",
    "build",
    "article_headers",
    "tax_rows",
    "method_rows",
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


# --- названия карточек -------------------------------------------------------

# Где лежат названия. Отдельная таблица, а не колонка в `fin_rows`: строк
# отчёта у одного артикула десятки, а название у него одно.
CARDS_TABLE = "card_names"


TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def _now() -> str:
    """Отметка времени в UTC: в базе время хранится только так."""
    return datetime.now(timezone.utc).strftime(TIME_FORMAT)


def names_ttl_days() -> int:
    """Сколько дней хранимое название карточки считается свежим.

    Число живёт в `config.toml`, секция `[cards]`: сроки в этом проекте
    настраивает владелец, а не правка кода. Ноль значит «не обновлять»:
    спросили один раз и запомнили навсегда.
    """
    return int((config.settings().get("cards") or {}).get("name_ttl_days", 0))


def _stamped(value: Any) -> datetime | None:
    """Отметка времени из базы. Непонятная отметка это «неизвестно когда»."""
    try:
        return datetime.strptime(str(value or "")[:19], TIME_FORMAT).replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def _fresh_names(
    client_id: int, *, ttl_days: int, path: str | Path | None = None
) -> set[int]:
    """Артикулы, чьё название спрашивали недавно и переспрашивать рано.

    Просроченное название не выбрасывается: в отчёт оно всё равно пойдёт, а
    вот сходить за каталогом в следующий раз уже стоит. Иначе переименование
    карточки в кабинете не доехало бы до отчёта никогда: за названиями бот
    шёл только ради артикула, о котором ещё не спрашивал.
    """
    now = datetime.now(timezone.utc)
    found: set[int] = set()
    for row in db.repo(client_id, path).rows(CARDS_TABLE):
        if ttl_days <= 0:
            found.add(int(row["nm_id"]))
            continue
        seen = _stamped(row["updated_at"])
        if seen is not None and now - seen < timedelta(days=ttl_days):
            found.add(int(row["nm_id"]))
    return found


def names_of(
    client_id: int,
    nm_ids: Iterable[int] | None = None,
    *,
    path: str | Path | None = None,
) -> dict[int, str]:
    """Известные названия карточек. Это чтение базы, в WB отсюда не ходят.

    Пустая строка в ответе значит «спрашивали, а карточки у Wildberries нет»:
    товар удалили из кабинета. Отсутствие ключа значит «ещё не спрашивали», и
    только оно отправляет за названиями в WB.
    """
    wanted = (
        None
        if nm_ids is None
        else {int(value) for value in nm_ids if value is not None}
    )
    found: dict[int, str] = {}
    for row in db.repo(client_id, path).rows(CARDS_TABLE):
        nm_id = int(row["nm_id"])
        if wanted is None or nm_id in wanted:
            found[nm_id] = str(row["title"] or "")
    return found


def card_titles(cards: Iterable[Mapping[str, Any]]) -> dict[int, str]:
    """Название по артикулу из ответа метода карточек.

    Название карточки пишет сам продавец, то есть это чужой текст: в книгу
    Excel он едет как есть, а в сообщение бота попадёт только через
    `bot.texts.fill`.
    """
    found: dict[int, str] = {}
    for item in cards or []:
        row = item or {}
        raw = row.get("nmID", row.get("nmId"))
        try:
            nm_id = int(raw)
        except (TypeError, ValueError):
            continue
        title = str(row.get("title") or "").strip()
        if title:
            found[nm_id] = title
    return found


def remember_names(
    client_id: int,
    cards: Iterable[Mapping[str, Any]],
    *,
    asked: Iterable[int] = (),
    path: str | Path | None = None,
) -> dict[int, str]:
    """Кладёт названия в базу и помечает артикулы, которых у WB не нашлось.

    Пометка нужна затем, чтобы один удалённый товар не гонял отчёт в
    Wildberries снова и снова: спросили один раз и запомнили ответ. Уже
    известное имя пометка не затирает - карточки больше нет, а как товар
    назывался, мы знаем, и это не повод забыть.
    """
    titles = card_titles(cards)
    repo = db.repo(client_id, path)
    now = _now()
    for nm_id, title in titles.items():
        repo.upsert(CARDS_TABLE, {"nm_id": nm_id}, title=title, updated_at=now)
    for value in asked:
        nm_id = int(value)
        if nm_id in titles:
            continue
        if repo.insert_once(CARDS_TABLE, nm_id=nm_id, title="") is None:
            # Строка уже была, а карточки у Wildberries по-прежнему нет. Имя
            # не трогаем, а отметку освежаем: без неё срок жизни названий
            # гонял бы бот в WB на каждом отчёте из-за одного удалённого
            # товара.
            repo.update(CARDS_TABLE, {"nm_id": nm_id}, updated_at=now)
    return titles


async def collect_names(
    client_id: int,
    nm_ids: Iterable[int],
    *,
    http: Any = None,
    path: str | Path | None = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], Any] | None = None,
) -> dict[int, str]:
    """Названия карточек для артикулов периода. Зовётся из обработчика очереди.

    В Wildberries идём в двух случаях: в периоде есть артикул, о котором мы
    ещё не спрашивали, или известное название успело протухнуть (срок жизни
    в `config.toml`, секция `[cards]`). Метод карточек самый мягкий по лимиту
    в проекте (100 запросов в минуту), но тянуть весь каталог на каждый отчёт
    незачем: имя товара меняется куда реже, чем считается прибыль, а платит
    за лишний запрос токен клиента.

    Отсутствие названий отчёт не роняет. Нет категории «Контент», WB не
    отвечает - в книге останется артикул, и это честнее выдуманного имени.
    Наружу летит только 401: его разбирает `core.clients`.

    Но отказ Wildberries и удалённая карточка это разные вещи, и снаружи они
    выглядят одинаково: артикул вместо имени. Поэтому отказ пишется в журнал.
    Ровно на этом проект и обжёгся: запрос каталога был собран неверно, WB
    отвечал 400, а отчёт выглядел штатным, потому что пустое имя это
    предусмотренный случай.
    """
    wanted = sorted({int(value) for value in nm_ids if value is not None})
    known = names_of(client_id, path=path)
    fresh = _fresh_names(client_id, ttl_days=names_ttl_days(), path=path)
    if not wanted or all(nm_id in fresh for nm_id in wanted):
        return {nm_id: known[nm_id] for nm_id in wanted if nm_id in known}
    client = wbapi.get_wb_client(client_id, http=http, path=path, clock=clock, sleep=sleep)
    try:
        cards = await client.cards_list()
    except wbapi.WBForbiddenError:
        logger.info(
            "у клиента %s нет категории «Контент», названия товаров пропущены", client_id
        )
        # Это единственный отказ, у которого есть понятная причина на стороне
        # клиента, и он не ошибка бота. В журнал всё равно: иначе «почему у
        # меня нет названий» не на что ответить.
        audit.log(
            "profit",
            client_id,
            "названия товаров не получены: у ключа нет категории «Контент»",
            level="warning",
            path=path,
        )
        return {nm_id: known[nm_id] for nm_id in wanted if nm_id in known}
    except (wbapi.WBUnavailable, wbapi.WBRateLimited, wbapi.WBApiError) as error:
        logger.warning("названия товаров клиента %s не получены: %s", client_id, error)
        audit.log(
            "profit",
            client_id,
            f"названия товаров не получены, Wildberries отказал: {error}",
            level="error",
            path=path,
        )
        return {nm_id: known[nm_id] for nm_id in wanted if nm_id in known}
    remember_names(client_id, cards, asked=wanted, path=path)
    return names_of(client_id, wanted, path=path)


# --- строки отчёта -----------------------------------------------------------


@dataclass(frozen=True)
class ArticleProfit:
    """Один артикул за период. `None` в деньгах значит «неизвестно»."""

    nm_id: int | None
    vendor_code: str = ""
    subject: str = ""
    # Название карточки. Пустое значит «названия нет»: карточку удалили или за
    # ней ещё не ходили. Пустая клетка в отчёте читается как потеря данных,
    # поэтому в книгу идёт `display_name`, а не это поле.
    title: str = ""
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
    # Возмещение Wildberries за выбывший товар: деньги пришли, товар не
    # продался. В выручку и в проданные штуки оно не входит, в прибыль тоже,
    # и названо в блоке «компенсация WB» листа проблем. Почему не входит -
    # в `METHODOLOGY`.
    compensation: Decimal = ZERO
    compensation_units: int = 0
    # Себестоимость выбывшего по компенсации товара, если она загружена. В
    # колонку «Себестоимость, ₽» и в прибыль не идёт, показывается рядом с
    # самой компенсацией.
    compensation_cost: Decimal | None = None
    compensation_reasons: tuple[str, ...] = ()
    profit: Decimal | None = None
    margin: float | None = None
    share: float | None = None

    @property
    def display_name(self) -> str:
        """Чем товар назван в отчёте: название карточки, а иначе артикул WB.

        Товар могли удалить из кабинета, и тогда названия у Wildberries не
        спросить. Выдумывать имя нельзя, а пустая клетка прочиталась бы как
        потерянные данные, поэтому остаётся артикул: он же ключ строки.
        """
        if self.title:
            return self.title
        return "" if self.nm_id is None else str(self.nm_id)

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

    @property
    def compensated(self) -> bool:
        """По артикулу есть возмещение за выбывший товар."""
        return self.compensation != ZERO or self.compensation_units > 0


# Статьи расходов Wildberries. Ключ это поле `finance.Amounts`, название то
# же, что в заголовке колонки. Порядок тот же, что в книге.
COST_ITEMS: tuple[tuple[str, str], ...] = (
    ("commission", "Комиссия WB"),
    ("acquiring", "Эквайринг"),
    ("logistics", "Логистика"),
    ("storage", "Хранение"),
    ("acceptance", "Приёмка"),
    ("penalties", "Штрафы"),
    ("deductions", "Прочие удержания"),
)


@dataclass(frozen=True)
class CostItem:
    """Статья расходов: сколько пришло по товарам, а сколько без них."""

    key: str
    title: str
    by_article: Decimal = ZERO
    faceless: Decimal = ZERO

    @property
    def only_faceless(self) -> bool:
        """Статья есть, а разреза по товарам у неё нет вовсе.

        Само правило живёт в `core.xlsx`: им же пользуется финансовая книга,
        а две копии одного правила разошлись бы молча и показали бы в двух
        книгах разные колонки за один и тот же период.
        """
        from core import xlsx

        return xlsx.only_faceless(self.by_article, self.faceless)


@dataclass(frozen=True)
class Totals:
    """Итог по кабинету. Складывается только то, что складывается.

    Проценты в итог не переносятся: маржинальность по кабинету это отношение
    суммарной прибыли к суммарной выручке, а доля в прибыли в итоге всегда сто
    процентов и смысла не несёт.

    Деньги и штуки складываются по всем артикулам, прибыль - только по тем, у
    которых она посчитана. Разница между `articles` и `priced` говорит, что в
    итог прибыли вошли не все: молчаливый ноль вместо неизвестного числа врёт.

    Счётчики `priced`, `costed` и `no_commission` нужны не для красоты: по ним
    книга решает, показать сумму или «нет данных». Сумма, сложенная из пустого
    множества, это ноль, а ноль в отчёте читается как «не заработали».
    """

    articles: int = 0
    priced: int = 0
    costed: int = 0
    no_commission: int = 0
    units: int = 0
    returns_count: int = 0
    revenue: Decimal = ZERO
    returns_amount: Decimal = ZERO
    cost: Decimal = ZERO
    commission: Decimal = ZERO
    acquiring: Decimal = ZERO
    logistics: Decimal = ZERO
    storage: Decimal = ZERO
    acceptance: Decimal = ZERO
    penalties: Decimal = ZERO
    deductions: Decimal = ZERO
    ad_spend: Decimal | None = None
    unallocated: Decimal = ZERO
    # Возмещения за выбывший товар. В прибыль не входят и складываются
    # отдельно: это не выручка и не расход, а своя статья.
    compensation: Decimal = ZERO
    compensation_units: int = 0
    profit: Decimal = ZERO
    margin: float | None = None

    @property
    def complete(self) -> bool:
        """Прибыль посчитана по всем артикулам: итогу нечего оговаривать."""
        return self.priced == self.articles


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
    # Из чего состоят расходы периода: по каждой статье отдельно то, что
    # Wildberries разнёс по товарам, и то, что отдал общей строкой.
    costs_breakdown: tuple[CostItem, ...] = ()
    ads: AdSpend = None  # type: ignore[assignment]
    weeks: tuple[Any, ...] = ()
    # Режим налогообложения селлера. Пустое правило значит «не выбран», и это
    # не ноль процентов: отчёт тогда честно говорит, что прибыль до налога.
    tax_rule: Any = None

    def __post_init__(self) -> None:
        if self.ads is None:
            object.__setattr__(
                self, "ads", AdSpend({}, available=False, reason=ADS_NOT_REQUESTED)
            )
        if self.tax_rule is None:
            object.__setattr__(self, "tax_rule", tax_module.Rule())

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
    def faceless_parts(self) -> tuple[CostItem, ...]:
        """Расшифровка обезлички: её сумма сходится с `unallocated`."""
        return tuple(item for item in self.costs_breakdown if item.faceless != ZERO)

    @property
    def unshared_items(self) -> tuple[CostItem, ...]:
        """Статьи, которых в разрезе по товарам нет вовсе."""
        return tuple(item for item in self.costs_breakdown if item.only_faceless)

    @property
    def compensations(self) -> tuple[ArticleProfit, ...]:
        """Артикулы с возмещением за выбывший товар.

        Ради этого блока работа и затевалась: товар, которого нет на остатках,
        показывался проданным, потому что Wildberries называет возмещение
        продажей. Теперь он из продаж ушёл, а деньги не исчезли и названы
        по имени.
        """
        return tuple(item for item in self.articles if item.compensated)

    @property
    def total_compensation(self) -> Decimal:
        return sum((item.compensation for item in self.articles), ZERO)

    @property
    def total_compensation_units(self) -> int:
        return sum(item.compensation_units for item in self.articles)

    @property
    def total_compensation_cost(self) -> Decimal:
        """Себестоимость выбывшего по компенсациям товара, где она загружена.

        В прибыль не входит, как и сама компенсация: это справка о том, во
        что выбывший товар обошёлся, а не статья расчёта.
        """
        return sum(
            (
                item.compensation_cost
                for item in self.articles
                if item.compensation_cost is not None
            ),
            ZERO,
        )

    @property
    def totals(self) -> Totals:
        """Итог по кабинету. Проценты не складываются, см. `Totals`."""
        priced = self.priced
        counted_revenue = sum((item.net_revenue for item in priced), ZERO)
        profit_total = self.total_profit

        def money(name: str) -> Decimal:
            return sum(
                (
                    value
                    for value in (getattr(item, name) for item in self.articles)
                    if value is not None
                ),
                ZERO,
            )

        return Totals(
            articles=len(self.articles),
            priced=len(priced),
            costed=sum(1 for item in self.articles if item.cost is not None),
            no_commission=len(self.without_commission),
            units=sum(item.units for item in self.articles),
            returns_count=sum(item.returns_count for item in self.articles),
            revenue=money("revenue"),
            returns_amount=money("returns_amount"),
            cost=money("cost"),
            commission=money("commission"),
            acquiring=money("acquiring"),
            logistics=money("logistics"),
            storage=money("storage"),
            acceptance=money("acceptance"),
            penalties=money("penalties"),
            deductions=money("deductions"),
            ad_spend=self.total_ad_spend,
            unallocated=money("unallocated"),
            compensation=money("compensation"),
            compensation_units=sum(item.compensation_units for item in self.articles),
            profit=profit_total,
            margin=margin(profit_total, counted_revenue),
        )

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
    def period_amounts(self) -> Any:
        """Статьи периода целиком, как их видит агент 1.

        Не сумма по артикулам, и это важно: строки отчёта без `nmId` в разрез
        по товарам не попадают, а в неделю попадают. Для налога нужна вся
        выручка периода, а не та её часть, которую Wildberries разнёс по
        товарам.
        """
        total = finance.Amounts()
        for week in self.weeks:
            total = total + week.amounts
        return total

    @property
    def tax(self) -> Any:
        """Оценка налога за период или None, если режим не выбран.

        База «доходов» это то, что заплатил покупатель (`retailAmount`), за
        вычетом возвратов, а не то, что перечислил Wildberries (`forPay`).
        Рядом кладётся и перечисленное: разница между ними и есть та ошибка,
        ради которой отчёт вообще научился считать налог.

        На «доходы минус расходы» в базу идут себестоимость проданного товара,
        удержания Wildberries и реклама. Штрафы не идут: их обычно не
        принимают. Чего не хватило, названо в `missing`: неполные расходы
        завышают налог, и отчёт об этом говорит вслух.
        """
        rule = self.tax_rule
        if not rule.known:
            return None
        amounts = self.period_amounts
        income = amounts.revenue - amounts.returns_amount
        expenses: Decimal | None = None
        missing: list[str] = []
        if rule.with_expenses:
            totals = self.totals
            ad_spend = self.total_ad_spend
            expenses = (
                amounts.costs
                - amounts.penalties
                + totals.cost
                + (ad_spend if ad_spend is not None else ZERO)
            )
            if totals.costed < totals.articles:
                missing.append(tax_module.MISSING_COST)
            if ad_spend is None:
                missing.append(tax_module.MISSING_ADS)
        return tax_module.Estimate(
            rule=rule,
            income=income,
            transferred=amounts.for_pay,
            expenses=expenses,
            missing=tuple(missing),
        )

    @property
    def profit_after_tax(self) -> Decimal | None:
        """Прибыль за вычетом оценки налога. Без режима налога нет и строки."""
        estimate = self.tax
        return None if estimate is None else estimate.after(self.total_profit)

    @property
    def tax_by_article(self) -> Any:
        """Налог по артикулам или None, если режим не выбран.

        Считает `core.tax.allocate`, здесь только подготовка входа. Выручка за
        вычетом возвратов служит и базой («доходы»), и весом («доходы минус
        расходы»): какой из двух смыслов в деле, говорит `Allocation.exact`.

        Строки округлены каждая сама по себе, поэтому их сумма и налог от
        общей базы расходятся на копейки. Разница лежит в `Allocation.gap`, и
        книга называет её на листе «Методология», а не подгоняет строки.
        """
        estimate = self.tax
        if estimate is None:
            return None
        bases = {item.nm_id: item.net_revenue for item in self.articles}
        return tax_module.allocate(estimate, bases)

    def after_tax_of(self, item: ArticleProfit, amount: Decimal | None) -> Decimal | None:
        """Прибыль артикула после его налога. Неизвестное не становится нулём.

        Прибыли нет (не загружена себестоимость или неизвестна комиссия) -
        нет и прибыли после налога, хотя сам налог посчитан: он считается от
        выручки, а она известна всегда.
        """
        if item.profit is None or amount is None:
            return None
        return (item.profit - amount).quantize(CENT, rounding=ROUND_HALF_UP)

    @property
    def tax_without_profit(self) -> Decimal | None:
        """Налог тех артикулов, у которых прибыли после налога нет.

        Ровно на это число «итого прибыль после налога» отличается от
        вычитания одного итога из другого: налог сложен по всем артикулам, а
        прибыль после налога только по тем, у кого посчитана прибыль. Оба
        итога при этом остаются суммами своих столбцов, иначе сложение
        столбца, которым селлер и проверяет таблицу, перестало бы сходиться.
        Поэтому разница не подгоняется, а называется числом.
        """
        allocation = self.tax_by_article
        if allocation is None:
            return None
        total = ZERO
        for item in self.articles:
            amount = allocation.of(item.nm_id)
            if amount is None or self.after_tax_of(item, amount) is not None:
                continue
            total += amount
        return total.quantize(CENT, rounding=ROUND_HALF_UP)

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

    @property
    def unknown_doc_types(self) -> tuple[str, ...]:
        """Типы документов WB, которых бот не знает, за весь период.

        Деньги из таких строк в раскладку вошли, а штуки нет: продажа это или
        нет, неизвестно, и посчитать наугад один раз уже вышло боком. Прежде
        это оставалось только в журнале, то есть селлер видел прибыль, у
        которой часть штук тихо не посчитана, и спросить ему было не о чем.
        """
        return tuple(
            dict.fromkeys(
                name
                for week in self.weeks
                for name in getattr(week, "unknown_doc_types", ())
            )
        )

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
    # Названия читаются из базы: за ними ходит обработчик очереди, а расчёт в
    # WB не ходит вовсе. Нет названия - в книге останется артикул.
    titles = names_of(client_id, list(weights), path=path)

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
        # Себестоимость выбывшего по компенсации товара считается, но в `cost`
        # не идёт: колонка «Себестоимость, ₽» это себестоимость за штуку на
        # проданные штуки, и селлер проверяет её этим умножением. Подмешать
        # сюда компенсационные штуки значит сломать проверку ровно тем же
        # способом, каким она сломалась в прошлый раз.
        compensation_cost = (
            None
            if per_unit is None or amounts.compensation_units <= 0
            else (_money(per_unit) * amounts.compensation_units).quantize(
                CENT, rounding=ROUND_HALF_UP
            )
        )
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
                title=titles.get(item.nm_id, ""),
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
                compensation=amounts.compensation,
                compensation_units=amounts.compensation_units,
                compensation_cost=compensation_cost,
                compensation_reasons=item.compensation_reasons,
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
    # Расшифровка расходов. Считается по тем же строкам, что и всё остальное,
    # и отвечает на вопрос «где мои деньги за хранение»: сумма по статьям,
    # пришедшим без артикула, сходится с обезличкой копейка в копейку, потому
    # что обезличка и есть сумма этих же статей.
    breakdown = tuple(
        CostItem(
            key=key,
            title=title,
            by_article=sum((getattr(item.amounts, key) for item in named), ZERO),
            faceless=sum((getattr(item.amounts, key) for item in faceless), ZERO),
        )
        for key, title in COST_ITEMS
    )
    return ProfitReport(
        client_id=client_id,
        period=period,
        date_from=date_from,
        date_to=date_to,
        articles=articles,
        unallocated=unallocated,
        unallocated_left=left,
        costs_breakdown=breakdown,
        ads=ads,
        weeks=tuple(weeks),
        # Режим налога читается из настроек селлера ровно так же, как
        # себестоимость и названия: это чтение базы, в WB отсюда не ходят.
        tax_rule=tax_module.rule_of(client_id, path=path),
    )


def with_names(report: ProfitReport, names: Mapping[int, str]) -> ProfitReport:
    """Подставляет названия карточек в готовый отчёт.

    Названия приезжают после расчёта, потому что за ними ходят в WB, а в WB
    ходит только обработчик очереди. На цифры они не влияют, поэтому и
    подставляются последними. Пустое имя ничего не затирает.
    """
    if not names:
        return report
    return replace(
        report,
        articles=tuple(
            replace(item, title=names.get(item.nm_id or 0) or item.title)
            for item in report.articles
        ),
    )


# --- книга Excel -------------------------------------------------------------

ARTICLES_SHEET = "Прибыль по артикулам"
PROBLEMS_SHEET = "Убыточные и без данных"
# Лист с подробностями. Налог по товарам стоит колонкой в первом листе, а
# здесь лежит то, что к товару не привязано: база, поступление от Wildberries
# и разница между ними. Ради этой разницы отчёт и научился считать налог.
TAX_SHEET = "Налог"
METHOD_SHEET = "Методология"

NO_DATA = "нет данных"

# Подпись итоговой строки и ответ в клетках, где сложение бессмысленно.
TOTAL_LABEL = "Итого по кабинету"
NOT_SUMMABLE = "не складывается"

# Подписи строк под итогом. Самих сумм налога здесь больше нет: они стоят в
# колонках у каждого товара и в итоговой строке, а повторять их третьим
# способом значит заводить в книге две правды об одних деньгах. Под итогом
# осталось то, чему в колонке места нет: как налог посчитан и чем сумма
# столбца отличается от налога по кабинету.
TAX_NOTE_LABEL = "Как посчитан налог"
TAX_OFF_LABEL = "Налог не учтён"
# Мост между двумя итогами. Налог есть у каждого товара, прибыль после налога
# только у тех, у кого посчитана прибыль, поэтому вычитание одного итога из
# другого не сходится. Числа этой разницы нет больше нигде в книге, то есть
# это не третья копия тех же денег, а единственный способ дать селлеру
# проверить строку сложением, как он и проверяет.
TAX_UNPRICED_LABEL = "Налог артикулов без прибыли"
# Что стоит в клетке прибыли, когда режим не выбран. Ноль там читался бы как
# «налога нет», а мы его просто не знаем.
BEFORE_TAX = "прибыль до налога"

# Подпись блока компенсаций в листе проблем.
COMPENSATION_BLOCK = "компенсация WB"

# Маржинальность названа «до налога» нарочно. Считается она как раньше, от
# прибыли до налога, и смысл колонки не поменялся: маржа это свойство экономики
# товара, по ней товары сравнивают между собой и ставят цену, а налог это
# свойство режима селлера. Но рядом теперь стоит прибыль после налога, и без
# подписи пришлось бы гадать, от какой из двух прибылей посчитан процент.
# Молча оставить прежний заголовок значило бы завести эту загадку и промолчать
# о ней. Почему не считать маржу после налога - на листе «Методология».
MARGIN_COLUMN = "Маржинальность до налога, %"

# Колонки листа артикулов: заголовок и ключ статьи расходов, если колонка
# показывает именно её. Ключ нужен ровно для одного: убрать из книги колонку,
# в которой у Wildberries нечему появиться (см. `CostItem.only_faceless`).
ARTICLE_COLUMNS: tuple[tuple[str, str | None], ...] = (
    ("Артикул WB", None),
    ("Артикул продавца", None),
    ("Название товара", None),
    ("Предмет", None),
    ("Продано, шт", None),
    ("Возвращено, шт", None),
    ("Выручка, ₽", None),
    ("Возвраты, ₽", None),
    ("Себестоимость за штуку, ₽", None),
    ("Себестоимость, ₽", None),
    ("Комиссия WB, ₽", "commission"),
    ("Эквайринг, ₽", "acquiring"),
    ("Логистика, ₽", "logistics"),
    ("Хранение, ₽", "storage"),
    ("Приёмка, ₽", "acceptance"),
    ("Штрафы, ₽", "penalties"),
    ("Прочие удержания, ₽", "deductions"),
    ("Реклама, ₽", None),
    ("Расходы без артикула, ₽", None),
    ("Чистая прибыль, ₽", None),
    (MARGIN_COLUMN, None),
    ("Доля в прибыли, %", None),
)

ARTICLE_HEADERS = tuple(header for header, _ in ARTICLE_COLUMNS)

# Колонки налога. Их две, и появляются они только у выбранного режима, потому
# что заголовок у них разный: на «доходах» это налог именно этого товара, а на
# «доходы минус расходы» наша раскладка кабинетного налога, и называть одним
# словом два разных числа нельзя.
#
# Готовый механизм скрытия колонок (`xlsx.only_faceless`) здесь не подходит, и
# это видно по его условию: он решает по данным Wildberries, пришла ли статья
# с ключом, и держит один заголовок на колонку. Здесь решает настройка
# селлера, а заголовков у колонки два. Поэтому колонки не скрываются, а просто
# не заводятся: правило «колонка, которая никогда не заполнится, хуже
# отсутствующей» выполняется тем же способом, что и там, а механизм свой.
TAX_COLUMN = "Налог, ₽"
TAX_SPREAD_COLUMN = "Налог, наша раскладка, ₽"
AFTER_TAX_COLUMN = "Прибыль после налога, ₽"

# Ключи колонок налога. Нужны затем же, зачем ключи статей: по ним строка
# собирается в том же порядке, что заголовки.
TAX_KEY = "tax"
AFTER_TAX_KEY = "after_tax"


def tax_columns(report: ProfitReport) -> tuple[tuple[str, str | None], ...]:
    """Колонки налога для этого отчёта. Режим не выбран - их нет вовсе.

    Ноль в колонке налога у человека без выбранного режима читался бы как
    «налога нет», а мы его просто не считали.
    """
    allocation = report.tax_by_article
    if allocation is None:
        return ()
    head = TAX_COLUMN if allocation.exact else TAX_SPREAD_COLUMN
    return ((head, TAX_KEY), (AFTER_TAX_COLUMN, AFTER_TAX_KEY))


def article_columns(report: ProfitReport) -> tuple[tuple[str, str | None], ...]:
    """Колонки листа артикулов целиком, вместе с налогом этого режима."""
    return ARTICLE_COLUMNS + tax_columns(report)

PROBLEM_HEADERS = ("Блок", "Артикул WB", "Артикул продавца", "Сумма, ₽", "Что это значит")

TAX_HEADERS = ("Показатель", "Сумма, ₽", "Пояснение")

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
        "Название товара",
        "карточки товаров, категория токена «Контент»",
        "-",
        "В отчёте о реализации Wildberries названия нет вовсе: там артикул WB, "
        "артикул продавца, бренд и предмет, а предмет это категория "
        "(«Наматрасник»), а не название карточки. Название берётся из карточек "
        "товаров и хранится у нас, чтобы не дёргать Wildberries на каждый "
        "отчёт. Если товар удалён из кабинета, названия взять негде, и в "
        "колонке остаётся артикул WB.",
    ),
    (
        "Статьи без разреза по товарам",
        "строки отчёта о реализации без nmId",
        "-",
        "Часть расходов Wildberries отдаёт общей суммой по кабинету и по "
        "товарам не разносит: чаще всего это хранение, приёмка и прочие "
        "удержания. Колонки под такую статью в листе «Прибыль по артикулам» "
        "нет: ноль в каждой строке читался бы как «не платили». Эти деньги "
        "расшифрованы в листе «Убыточные и без данных» и разнесены по товарам "
        "в колонке «Расходы без артикула, ₽».",
    ),
    (
        "Итого по кабинету",
        "строки этого же листа",
        "суммы колонок; маржинальность считается заново",
        "Складываются штуки, выручка, возвраты, себестоимость и расходы. "
        "Маржинальность не складывается: по кабинету это прибыль, делённая на "
        "выручку за вычетом возвратов, и считается она по тем артикулам, у "
        "которых прибыль посчитана. Доля в прибыли в итоге всегда сто "
        "процентов, поэтому её там нет. Артикулы без себестоимости и с "
        "неизвестной комиссией в итог прибыли не входят, и рядом с итогом "
        "написано, сколько их. В колонках налога в итоге стоят суммы самих "
        "колонок, чтобы таблица сходилась при проверке сложением. Налог "
        "сложен по всем артикулам, а прибыль после налога только по тем, у "
        "которых прибыль посчитана: налог считается от выручки и известен "
        "всегда, а прибыли после налога у товара без прибыли не бывает.",
    ),
    (
        "Компенсация Wildberries",
        "docTypeName и retailAmount строки отчёта о реализации",
        "строки типа «Продажа» с нулевым retailAmount",
        "Так Wildberries платит за товар, который покупатель вернул или "
        "потерял: тип документа «Продажа», quantity проставлено, деньги "
        "приходят в forPay, а retailAmount ровно ноль, потому что покупатель "
        "не платил ничего. Товар по такой строке выбыл, а не продался, и на "
        "остатках его нет. Поэтому ни в выручку, ни в проданные штуки, ни в "
        "прибыль она не входит: иначе товар без цены выглядел бы проданным, а "
        "маржинальность пришлось бы делить на нулевую выручку. Деньги не "
        "потеряны, они в «к перечислению» отчёта «Финансы», а каждая такая "
        "строка названа в листе «Убыточные и без данных». Себестоимость "
        "выбывшего товара тоже не списывается: колонка «Себестоимость, ₽» это "
        "себестоимость за штуку на проданные штуки, и селлер проверяет её "
        "этим умножением; компенсационные штуки внутри неё сломали бы "
        "проверку. Во что обошёлся выбывший товар, сказано рядом с самой "
        "компенсацией. Вид строки решается типом документа и суммой, а не "
        "обоснованием sellerOperName: это свободный текст, и «Добровольная "
        "компенсация при возврате» содержит слово «возврат», хотя документ не "
        "возврат. Обоснование показано как пояснение, но ничего не "
        "классифицирует.",
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
        "Маржинальность до налога, %",
        "-",
        "чистая прибыль до налога / (выручка - возвраты) * 100",
        "Без выручки маржинальности не существует, в таких строках стоит "
        "«нет данных», а не ноль. Считается она от прибыли до налога, и "
        "заголовок об этом говорит прямо. Так и было раньше, но раньше рядом "
        "не стояла прибыль после налога, и вопроса «от какой из двух» не "
        "возникало. Считать маржу после налога нельзя: маржинальность это "
        "свойство экономики товара, по ней товары сравнивают между собой и "
        "ставят цену, а налог это свойство режима селлера, и на «доходы минус "
        "расходы» налог товара вообще наша раскладка. Маржа после такой "
        "раскладки унаследовала бы её условность и перестала бы сравниваться "
        "хоть с чем-нибудь.",
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
        "Доходы для налога, ₽",
        "поле retailAmount отчёта о реализации",
        "сумма retailAmount по продажам - сумма retailAmount по возвратам",
        "Это то, что заплатил покупатель, а не то, что перечислил Wildberries. "
        "Классическая ошибка: селлер видит поступление на счёт и считает "
        "процент с него, а налоговая считает с полной цены продажи, вместе с "
        "удержанной комиссией и скидкой площадки. Перечисленное (поле forPay) "
        "стоит в этом же листе строкой ниже, чтобы разница была видна. "
        "Возвраты базу уменьшают: возврат отличается от продажи типом "
        "документа (поле docTypeName), тем же самым, по которому возвраты "
        "отделены во всех остальных отчётах бота.",
    ),
    (
        "Налог, оценка, ₽",
        "режим и ставка из /settings",
        "УСН «доходы»: доходы * ставка / 100; УСН «доходы минус расходы»: "
        "(доходы - принятые расходы) * ставка / 100",
        "Это оценка, а не налог к уплате. Бот не ведёт налоговый учёт и не "
        "заменяет бухгалтера: страховых взносов, вычетов, авансовых платежей "
        "и годового пересчёта он не видит вовсе. Ставку выбирает сам селлер, "
        "потому что в регионах она льготная. Пока режим не выбран, налог не "
        "считается и прибыль показана до налога: ноль вместо неизвестного "
        "числа был бы хуже пустой строки.",
    ),
    (
        "Расходы, принятые в базу, ₽",
        "те же источники, что и в прибыли",
        "себестоимость проданного товара + удержания Wildberries без штрафов "
        "+ реклама",
        "Только на УСН «доходы минус расходы». Штрафы Wildberries в базу не "
        "идут: такие расходы обычно не принимаются. Себестоимость принимается "
        "после продажи, поэтому берётся по проданным штукам за вычетом "
        "возвращённых. Чего бот не знает, того в расходах нет: страховых "
        "взносов, зарплаты, аренды, упаковки, услуг банка и всего, что "
        "проходит мимо кабинета Wildberries. Из-за этого оценка налога "
        "скорее завышена, чем занижена. Если себестоимость загружена не по "
        "всем артикулам или расход рекламы не получен, об этом сказано прямо "
        "в листе «Налог».",
    ),
    (
        "Налог, ₽ (УСН «доходы»)",
        "выручка артикула за вычетом возвратов и ставка из /settings",
        "(выручка артикула - его возвраты) * ставка / 100",
        "На этом режиме налог считается с выручки, поэтому налог товара это "
        "точная величина самого товара, а не доля чего-то общего: колонка так "
        "и называется, «Налог, ₽». Налог известен и у товара, по которому "
        "прибыль не посчитана: выручка известна всегда. Каждая строка "
        "округлена до копейки сама по себе, по обычным правилам округления, и "
        "ни одна не подогнана под итог: подогнанная копейка это число, "
        "которого у товара нет.",
    ),
    (
        "Налог, наша раскладка, ₽ (УСН «доходы минус расходы»)",
        "налог по кабинету и выручка артикулов",
        "налог кабинета * (выручка артикула - его возвраты) / (выручка "
        "периода - возвраты)",
        "На этом режиме база считается по кабинету целиком: доходы минус "
        "принятые расходы. У товара своей базы нет, поэтому разложить налог по "
        "товарам можно только делением, и это наша раскладка, а не налог "
        "именно этого товара: так и написано в заголовке колонки. Делим по "
        "выручке за вычетом возвратов, тем же основанием, что и расходы без "
        "артикула. По прибыли делить нельзя: у убыточного товара доля вышла бы "
        "отрицательной, то есть налог в минус, а если прибыль по кабинету "
        "неположительная, доли не существует вовсе, хотя налог платится. Товар "
        "без выручки доли не получает.",
    ),
    (
        "Прибыль после налога, ₽",
        "колонки «Чистая прибыль, ₽» и налога этого же листа",
        "чистая прибыль - налог артикула",
        "Если прибыль по артикулу не посчитана (нет себестоимости или "
        "неизвестна комиссия), здесь стоит «нет данных», а не ноль: налог "
        "посчитать было чем, а прибыль нет, и ноль прочитался бы как "
        "«отработали в ноль». В итоге из-за этого два соседних числа не "
        "связаны вычитанием: налог сложен по всем артикулам, а прибыль после "
        "налога только по тем, у которых прибыль есть. Подрезать итог налога "
        "для красоты нельзя, иначе сложение столбца перестало бы с ним "
        f"сходиться, поэтому разница стоит под итогом отдельной строкой "
        f"«{TAX_UNPRICED_LABEL}», и по ней строку можно проверить.",
    ),
    (
        "Почему сумма столбца налога и налог по кабинету расходятся",
        "лист «Прибыль по артикулам» и лист «Налог»",
        "-",
        "Налог кабинета считается от общей базы: ставка умножается на всю "
        "выручку за вычетом возвратов, а не складывается из округлённых "
        "кусочков. В столбце же каждая строка округлена до копейки сама по "
        "себе. От этого сумма столбца и налог от общей базы расходятся на "
        "копейки: не больше полкопейки на каждую округлённую строку, то есть "
        "на десятке товаров это копейки четыре или пять. Это не ошибка и не "
        "потерянные деньги, а обычное следствие округления. В итоге листа "
        "артикулов стоит сумма столбца, чтобы таблица сходилась при проверке "
        "сложением, а на листе «Налог» настоящий расчёт от общей базы. Если "
        "разница появилась, она названа в строке «Как посчитан налог» под "
        "итогом. У разницы бывает и вторая причина, и с округлением она не "
        "складывается в одно слово: если Wildberries часть выручки по товарам "
        "не разнёс (строка отчёта без артикула), в базу налога она входит, а "
        "товара у неё нет, и налог с неё в столбце не окажется. Эта часть "
        "известна нам числом, поэтому в строке под итогом она названа числом, "
        "а округлением не называется никогда: разница в рубли округлением не "
        "бывает, сколько бы товаров в отчёте ни было.",
    ),
    (
        "Минимальный налог",
        "-",
        "1% от доходов за год",
        "На УСН «доходы минус расходы» за год платится не меньше одного "
        "процента от доходов. Правило годовое, а отчёт за неделю, месяц или "
        "квартал, поэтому честно посчитать минимальный налог здесь нельзя: "
        "бот показывает один процент от доходов периода отметкой, а не "
        "платежом.",
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


def _hidden_columns(report: ProfitReport) -> frozenset[str]:
    """Статьи, под которые колонки в книге не будет."""
    return frozenset(item.key for item in report.unshared_items)


def article_headers(report: ProfitReport) -> tuple[str, ...]:
    """Заголовки листа артикулов для этого отчёта.

    Список не постоянный, и это решение по двум разным поводам. Колонка, в
    которой у Wildberries нечему появиться, из книги убирается: ноль в каждой
    строке читается как «расхода не было», а расход был, просто Wildberries не
    разнёс его по товарам, и деньги видны в расшифровке обезлички. А колонок
    налога нет вовсе, пока селлер не выбрал режим.
    """
    from core import xlsx

    return xlsx.visible_headers(article_columns(report), _hidden_columns(report))


def _visible(
    values: Sequence[Any],
    columns: Sequence[tuple[str, str | None]],
    hidden: frozenset[str],
) -> list[Any]:
    from core import xlsx

    return xlsx.visible_row(values, columns, hidden)


def _totals_note(report: ProfitReport) -> str:
    """Оговорка рядом с итогом: что именно в него не вошло.

    Итог, который молча считает неизвестную прибыль нулём, врёт. Поэтому
    складывается только посчитанное, а сколько артикулов осталось за бортом,
    написано прямо в строке.
    """
    totals = report.totals
    if totals.complete and not totals.no_commission:
        return f"сложено по всем артикулам, их {totals.articles}"
    note = (
        f"прибыль сложена по {totals.priced} артикулам из {totals.articles}: "
        f"остальные в листе «{PROBLEMS_SHEET}» и в итог не вошли"
    )
    if totals.no_commission:
        note += f"; комиссия неизвестна по {totals.no_commission} артикулам"
    if report.tax is not None:
        # Налог и прибыль после него складываются по разному числу строк, и
        # молчать об этом нельзя: налог есть у каждого товара, он считается от
        # выручки, а прибыли после налога у товара без прибыли не бывает.
        note += (
            "; налог сложен по всем артикулам, а прибыль после налога только "
            "по тем, у которых посчитана прибыль"
        )
        if report.tax_without_profit:
            # Оговорка словами ничего не доказывает: селлер проверяет строку
            # вычитанием. Поэтому здесь ссылка на число, а не ещё одно «так и
            # должно быть».
            note += (
                ", поэтому вычитание одного итога из другого не сходится: "
                f"насколько, сказано строкой «{TAX_UNPRICED_LABEL}» под итогом"
            )
    return note


def _tax_totals(report: ProfitReport, allocation: Any) -> list[Any]:
    """Клетки налога в итоговой строке: суммы столбцов, а не свой расчёт.

    Селлер проверяет таблицу сложением, поэтому в итоге стоит ровно то, что
    даст сложение колонки, и ни одна из двух клеток от этого правила не
    отступает. Настоящий налог кабинета считается от общей базы и живёт на
    листе «Налог»; с суммой столбца он расходится, и разница разобрана по
    причинам в строке под итогом, а не подогнана.

    Налог сложен по всем артикулам, а прибыль после налога только по тем, у
    кого посчитана прибыль, поэтому вычитание одной клетки из другой не
    сходится. Подрезать для сходимости нельзя ни одну из них: клетка, в
    которой не сумма своего столбца, ломает ту самую проверку сложением.
    Поэтому разница названа отдельной строкой под итогом
    (`TAX_UNPRICED_LABEL`), и по ней строку можно проверить.
    """
    if allocation is None:
        return []
    after = sum(
        (
            value
            for value in (
                report.after_tax_of(item, allocation.of(item.nm_id))
                for item in report.articles
            )
            if value is not None
        ),
        ZERO,
    )
    return [
        _cell(allocation.total),
        _cell(after if report.totals.priced else None),
    ]


def _totals_values(report: ProfitReport, allocation: Any = None) -> list[Any]:
    """Итоговая строка в порядке колонок листа."""
    totals = report.totals
    return [
        TOTAL_LABEL,
        _totals_note(report),
        "",
        "",
        totals.units,
        totals.returns_count,
        _cell(totals.revenue),
        _cell(totals.returns_amount),
        # Цена за штуку это не сумма: складывать её значит получить число,
        # которого нет ни у одного товара.
        NOT_SUMMABLE,
        # Пустая сумма это не ноль. Себестоимости нет ни у одного артикула -
        # в итоге «нет данных», иначе селлер прочитает «товар достался даром».
        _cell(totals.cost if totals.costed else None),
        _cell(totals.commission if totals.no_commission < totals.articles else None),
        _cell(totals.acquiring),
        _cell(totals.logistics),
        _cell(totals.storage),
        _cell(totals.acceptance),
        _cell(totals.penalties),
        _cell(totals.deductions),
        _cell(totals.ad_spend),
        _cell(totals.unallocated),
        _cell(totals.profit if totals.priced else None),
        _cell(totals.margin),
        # Доля в прибыли в итоге всегда сто процентов и не значит ничего.
        NOT_SUMMABLE,
    ] + _tax_totals(report, allocation)


def _compensation_note(item: ArticleProfit) -> str:
    """Что означает строка компенсации. Читает это селлер, а не программист.

    Главный вопрос здесь не «сколько», а «куда делся товар»: именно с него
    и начался разбор. Поэтому в строке сказано и про штуки, и про то, где
    остались деньги, и чем Wildberries обосновал выплату.
    """
    text = (
        f"Wildberries возместил {item.compensation_units} шт этого товара. "
        "Деньги пришли и остались в «к перечислению» отчёта «Финансы», а товар "
        "выбыл, а не продался: поэтому его нет на остатках. В выручку, в "
        "проданные штуки и в прибыль эта сумма не вошла, иначе товар без цены "
        "снова выглядел бы продажей."
    )
    if item.compensation_cost is not None:
        text += (
            f" Себестоимость выбывшего товара {_cell(item.compensation_cost)} ₽ в "
            "прибыль тоже не вошла: почему, написано на листе "
            f"«{METHOD_SHEET}»."
        )
    else:
        text += (
            " Себестоимость этого артикула не загружена, поэтому во что обошёлся "
            "выбывший товар, сказать нечем."
        )
    if item.compensation_reasons:
        reasons = "; ".join(item.compensation_reasons)
        text += f" Обоснование Wildberries: {reasons}."
    return text


def _faceless_note(item: CostItem) -> str:
    """Что означает строка расшифровки. Читает это селлер, а не программист."""
    if item.only_faceless:
        return (
            f"{item.title}: Wildberries отдаёт эту статью общей суммой по "
            "кабинету и по товарам не разносит, поэтому отдельной колонки под "
            "неё в листе «Прибыль по артикулам» нет. Деньги не потеряны: они "
            "разнесены по товарам пропорционально выручке и сидят в колонке "
            "«Расходы без артикула, ₽»."
        )
    return (
        f"{item.title}: часть этой статьи Wildberries отдал строками без "
        "артикула. В колонке товара стоит только то, что он разнёс сам, "
        "остальное разнесено пропорционально выручке."
    )


# Чего могло не хватить в расходах на «доходы минус расходы». Ключи знает
# core.tax, слова живут здесь: в книгу они едут как есть.
TAX_MISSING_NOTES: dict[str, str] = {
    tax_module.MISSING_COST: "себестоимость загружена не по всем артикулам",
    tax_module.MISSING_ADS: "расход рекламы у Wildberries получить не вышло",
}

TAX_NOT_CHOSEN = (
    "Режим налогообложения не выбран, поэтому прибыль в этой книге показана "
    "до налога. Ноль вместо налога бот не подставляет: молчаливый ноль хуже "
    "отсутствия строки. Выберите режим и ставку командой /settings, и налог "
    "встанет сюда отдельной строкой."
)

TAX_DISCLAIMER = (
    "Это оценка, а не налог к уплате: бот не ведёт налоговый учёт, не знает "
    "про страховые взносы, вычеты и авансовые платежи и не заменяет "
    "бухгалтера."
)


def tax_rows(report: ProfitReport) -> list[list[Any]]:
    """Лист «Налог» для этого отчёта.

    Главное здесь не умножение на ставку, а две строки рядом: что заплатил
    покупатель и что перечислил Wildberries. Селлер сверяет вторую с
    поступлением на счёт и видит, что налог считается с первой.
    """
    estimate = report.tax
    if estimate is None:
        return [["Налог не посчитан", "", TAX_NOT_CHOSEN]]

    rule = estimate.rule
    rows: list[list[Any]] = [
        [
            "Доходы: заплатил покупатель",
            _cell(estimate.income),
            "База налога. Полная цена продажи вместе с удержанной комиссией и "
            "скидкой площадки, за вычетом возвратов.",
        ],
        [
            "Для сравнения: перечислил Wildberries",
            _cell(estimate.transferred),
            "Это сумма, которая приходит на счёт. Налог считается не с неё: "
            "процент с поступления это и есть та ошибка, из-за которой "
            "приходят доначисления.",
        ],
        [
            "Разница",
            _cell(estimate.gap),
            "Комиссия, логистика, хранение и скидка площадки. Налоговая "
            "считает и с этих денег тоже, хотя до вашего счёта они не дошли.",
        ],
    ]
    if estimate.expenses is not None:
        note = (
            "Себестоимость проданного товара, удержания Wildberries и реклама. "
            "Штрафы Wildberries сюда не входят: такие расходы обычно не "
            "принимаются. Страховых взносов, зарплаты, аренды и всего, что "
            "проходит мимо кабинета, бот не видит, и вы добавляете их сами."
        )
        if estimate.missing:
            what = ", ".join(
                TAX_MISSING_NOTES[key]
                for key in estimate.missing
                if key in TAX_MISSING_NOTES
            )
            note += (
                f" Расходы посчитаны не полностью: {what}. Налог в этой оценке "
                "из-за этого завышен."
            )
        rows.append(["Расходы, принятые в базу", _cell(estimate.expenses), note])
        rows.append(
            [
                "База: доходы минус расходы",
                _cell(estimate.base),
                "С отрицательной базы налог не берётся, в такой период оценка "
                "нулевая. За год картина может быть другой.",
            ]
        )
    rows.append(
        [
            "Налог, оценка",
            _cell(estimate.amount),
            f"{rule.title}, ставка {rule.rate_text}%. Это расчёт от общей базы: "
            f"ставка на всю базу периода сразу. В листе «{ARTICLES_SHEET}» налог "
            "разобран по товарам, и каждая строка там округлена до копейки сама "
            "по себе, поэтому сумма того столбца может отличаться от этого "
            f"числа на копейки. Почему - на листе «{METHOD_SHEET}». "
            f"{TAX_DISCLAIMER}",
        ]
    )
    if estimate.expenses is not None:
        rows.append(
            [
                "Отметка минимального налога: 1% от доходов",
                _cell(estimate.minimum),
                "За год на этом режиме платится не меньше одного процента от "
                "доходов. Правило годовое, а это отчёт за период, поэтому "
                "посчитать минимальный налог честно здесь нельзя: отметка "
                "показана для сравнения, платежом она не является.",
            ]
        )
    totals = report.totals
    after_note = (
        "Прибыль из листа «Прибыль по артикулам» за вычетом оценки налога. "
        "Если прибыль посчитана не по всем артикулам, это видно в итоговой "
        "строке того листа, и здесь остаётся та же оговорка."
    )
    if report.tax_without_profit:
        # Иначе в книге два разных «прибыль после налога»: здесь вычтен налог
        # кабинета целиком, а в итоге того листа сумма столбца. Селлер найдёт
        # оба числа, и молчание о разнице он прочитает как ошибку.
        after_note += (
            f" Здесь вычтен налог кабинета целиком, вместе с налогом "
            f"артикулов без прибыли; в итоге того листа стоит сумма столбца, "
            f"то есть налог только тех артикулов, у которых прибыль есть. "
            f"Разница названа там же, строками под итогом."
        )
    rows.append(
        [
            "Чистая прибыль после налога",
            _cell(report.profit_after_tax if totals.priced else None),
            after_note,
        ]
    )
    return rows


# Куда в листе артикулов встают строки под итогом. Колонка ищется по
# заголовку, а не по номеру: номер разъедется при первой же вставке колонки в
# середину, а заголовок останется тем же.
PROFIT_COLUMN = "Чистая прибыль, ₽"

# Пояснение под итогом. Самих сумм здесь нет: налог стоит колонкой у каждого
# товара и в итоговой строке, а третья копия тех же денег превратила бы лист в
# спор с самим собой. Здесь только то, чего в клетке не скажешь.
TAX_IN_TOTAL_EXACT = (
    "{mode}, ставка {rate}%. Налог каждого товара это ставка от его выручки за "
    "вычетом возвратов, то есть налог именно этого товара, а не доля общего. "
    "В итоге стоит сумма столбца. База, поступление от Wildberries и разница "
    "между ними на листе «{sheet}». Это оценка, а не налог к уплате."
)

TAX_IN_TOTAL_SPREAD = (
    "{mode}, ставка {rate}%. База на этом режиме считается по кабинету целиком "
    "(доходы минус принятые расходы), поэтому по товарам налог разложен нами, "
    "пропорционально выручке за вычетом возвратов: это наша раскладка, а не "
    "налог именно этого товара. В итоге стоит сумма столбца. Основание "
    "раскладки и почему не по прибыли - на листе «{method}»; база и расходы на "
    "листе «{sheet}». Это оценка, а не налог к уплате."
)

# Разница между суммой столбца и налогом от общей базы. Прятать её нельзя, но
# и тревогой она не является: строки округлены каждая сама по себе.
TAX_GAP_IN_TOTAL = (
    " Сумма столбца {total} ₽, а налог от общей базы {amount} ₽: разница "
    "{gap} ₽ это округление строк до копейки, подробнее на листе «{method}»."
)

# Та же разница, но причина у неё известна точно: часть выручки Wildberries по
# товарам не разнёс, в базу налога она входит, а строки в столбце у неё нет.
# Прежде такая разница называлась округлением, если по числу товаров проходила
# под предел, и это была уверенная неправда: селлер получал объяснение
# настоящей нехватки налога словом «копейки». Теперь выручка без артикула
# известна нам числом, и она называется числом.
TAX_GAP_OFF_ARTICLE = (
    " Сумма столбца {total} ₽, а налог от общей базы {amount} ₽: разница "
    "{gap} ₽ это не округление. За период Wildberries не разнёс по товарам "
    "выручку на {uncovered} ₽, налог с неё {uncovered_tax} ₽: в базу она "
    "входит, а товара у неё нет, и строки в столбце ей не нашлось. Подробнее "
    "на листе «{method}»."
)

# А здесь разница не объясняется ни выручкой без артикула, ни округлением.
# Называть её округлением было бы ровно тем, чего в этом проекте делать нельзя:
# уверенным голосом сказать неправду. Округление строки не даёт больше половины
# копейки, и предел считается по строкам, которые действительно округлялись.
TAX_GAP_UNEXPLAINED = (
    " Сумма столбца {total} ₽, а налог от общей базы {amount} ₽: разница "
    "{gap} ₽ больше, чем дают округление строк и выручка без артикула "
    "(её за период {uncovered} ₽). Чем объясняется остаток, бот не знает, и "
    "придумывать объяснение не станет. Подробнее на листе «{method}»."
)

# Минус в колонке налога. Расчёт при этом верный, и чинить его нельзя: возврат
# по-настоящему уменьшает налог периода, и сумма строк сходится с налогом
# кабинета именно благодаря минусу. Чинить надо подпись, и вот она. Говорится
# это только тогда, когда отрицательная строка в книге действительно есть:
# предупреждение про случай, которого нет, читать перестанут.
TAX_NEGATIVE_IN_TOTAL = (
    " В столбце есть налог с минусом (артикулов с минусом {count}, вместе "
    "{amount} ₽): такой товар продан в прошлом периоде, а вернулся в этом, и "
    "выручка за период у него ушла в минус. Минус в колонке «Налог» значит "
    "«возврат уменьшил налог», а не доплату вам от государства. Отсекать его "
    "в ноль нельзя: тогда сложение столбца перестало бы сходиться с налогом "
    "кабинета. У такого товара прибыль после налога выходит больше прибыли, и "
    "ровно на этот минус. Какие это артикулы, сказано на листе «{method}», "
    "строка «Налог с минусом»."
)

# Та же история на листе «Методология»: там она останется с книгой, когда
# текста сообщения селлер уже не помнит.
TAX_NEGATIVE_METHOD = (
    "Налог артикула это ставка от его выручки за вычетом возвратов. Если товар "
    "продан в прошлом периоде, а вернулся в этом, выручка за период у него "
    "отрицательная, и налог выходит с минусом: {names}. Всего {amount} ₽. Это "
    "не доплата от государства, а уменьшение налога периода на возврат: "
    "возврат действительно уменьшает базу. Минус не отсекается в ноль "
    "намеренно, иначе сложение столбца перестало бы сходиться с налогом "
    "кабинета, а сложение здесь главная проверка. По той же причине прибыль "
    "после налога у такого товара выходит больше его прибыли."
)

# Почему два соседних итога не связаны вычитанием. Пояснение к строке
# «Налог артикулов без прибыли»: число стоит в клетке, а здесь то, что в
# клетке не скажешь, и готовая проверка сложением.
TAX_UNPRICED_NOTE = (
    "Артикулов, по которым прибыль не посчитана: {count} из {articles} (нет "
    "себестоимости или неизвестна комиссия). Налог по ним посчитан: он "
    "считается от выручки, а она известна всегда. Поэтому «Итого прибыль "
    "после налога» это не «Итого прибыль» минус «Итого налог»: из прибыли "
    "вычтен налог только тех артикулов, у которых прибыль есть. Проверить "
    "можно так: «Итого налог» минус это число, и результат вычесть из «Итого "
    "прибыль». Сами артикулы названы в листе «{sheet}»."
)


def _under_total(
    label: str,
    note: str,
    amount: Any,
    columns: Sequence[tuple[str, str | None]] = ARTICLE_COLUMNS,
    column: str = PROFIT_COLUMN,
) -> list[Any]:
    """Строка под итогом: подпись, пояснение и число в одной колонке.

    Клеток товара в такой строке нет вовсе: пустая клетка честнее нуля, если
    складывать нечего. Число, если оно есть, встаёт ровно под тем итогом, к
    которому относится, чтобы читалось сверху вниз, а не искалось по листу:
    деньги прибыли под прибылью, деньги налога под налогом.
    """
    headers = [header for header, _ in columns]
    row: list[Any] = ["" for _ in columns]
    row[0] = label
    row[1] = note
    row[headers.index(column)] = amount
    return row


def tax_total_rows(report: ProfitReport) -> list[list[Any]]:
    """Строка под итогом листа «Прибыль по артикулам»: как посчитан налог.

    Прежде здесь стояли две строки с суммами, «Налог, оценка» и «Прибыль после
    налога». Теперь обе суммы есть в колонках у каждого товара и в итоговой
    строке, и повторять их третьим способом значит заводить в книге две правды
    об одних деньгах: селлер сложит столбец и не поймёт, какому числу верить.
    Поэтому сумм здесь не осталось, а осталось то, чего в клетке не скажешь:
    режим, ставка, чем налог товара отличается от нашей раскладки и откуда
    копейки расхождения с листом «Налог».

    Режим не выбран - строка тем более нужна: колонок налога в книге нет, и
    без этой строки селлер не узнает, что прибыль показана до налога.

    Вторая строка появляется только там, где она нужна: когда прибыль
    посчитана не по всем артикулам, два соседних итога не связаны вычитанием,
    и без её числа селлер проверит строку, увидит нехватку и перестанет верить
    таблице.
    """
    estimate = report.tax
    columns = article_columns(report)
    if estimate is None:
        return [_under_total(TAX_OFF_LABEL, TAX_NOT_CHOSEN, BEFORE_TAX, columns)]
    allocation = report.tax_by_article
    rule = estimate.rule
    template = TAX_IN_TOTAL_EXACT if allocation.exact else TAX_IN_TOTAL_SPREAD
    note = template.format(
        mode=rule.title, rate=rule.rate_text, sheet=TAX_SHEET, method=METHOD_SHEET
    )
    if not allocation.matches:
        # Причину разницы выбирает `core.tax`, а не этот текст: там известны и
        # выручка без артикула, и число строк, которые правда округлялись.
        # Прежний предел рос вместе с числом товаров, и при двух сотнях
        # артикулов называл округлением два рубля настоящей нехватки налога.
        if allocation.rounding:
            template = TAX_GAP_IN_TOTAL
        elif allocation.explained:
            template = TAX_GAP_OFF_ARTICLE
        else:
            template = TAX_GAP_UNEXPLAINED
        note += template.format(
            total=_cell(allocation.total),
            amount=_cell(estimate.amount),
            gap=_cell(allocation.gap),
            uncovered=_cell(allocation.uncovered),
            uncovered_tax=_cell(allocation.uncovered_tax),
            method=METHOD_SHEET,
        )
    if allocation.negative:
        note += TAX_NEGATIVE_IN_TOTAL.format(
            count=len(allocation.negative),
            amount=_cell(allocation.negative_total),
            method=METHOD_SHEET,
        )
    rows = [_under_total(TAX_NOTE_LABEL, note, "", columns)]
    totals = report.totals
    unpriced = report.tax_without_profit
    if unpriced:
        # Число стоит в колонке налога, а не прибыли: это налог, и читаться
        # оно должно под тем итогом, из которого вычитается.
        rows.append(
            _under_total(
                TAX_UNPRICED_LABEL,
                TAX_UNPRICED_NOTE.format(
                    count=totals.articles - totals.priced,
                    articles=totals.articles,
                    sheet=PROBLEMS_SHEET,
                ),
                _cell(unpriced),
                columns,
                column=TAX_COLUMN if allocation.exact else TAX_SPREAD_COLUMN,
            )
        )
    return rows


# Сколько артикулов называется в пояснении по именам. Больше десятка номеров
# подряд читать невозможно, а остаток назван числом: «и ещё 40» это честно, а
# обрыв списка без счёта выглядел бы полным списком.
NAMED_ARTICLES = 10


def _article_list(keys: Sequence[Any]) -> str:
    """Артикулы в пояснении: первые по именам, остальные числом."""
    head = ", ".join(str(key) for key in keys[:NAMED_ARTICLES])
    rest = len(keys) - NAMED_ARTICLES
    return f"{head} и ещё {rest}" if rest > 0 else head


def method_rows(report: ProfitReport) -> list[list[Any]]:
    """Лист «Методология» для этого отчёта.

    К общим правилам добавляется строка про этот период: какие именно статьи
    Wildberries не разнёс по товарам. Общее правило селлер прочитает и так, а
    «где мои деньги за хранение» это вопрос про его отчёт, а не про правило.
    """
    rows = [list(row) for row in METHODOLOGY]
    hidden = report.unshared_items
    if hidden:
        names = ", ".join(item.title.lower() for item in hidden)
        amount = _cell(sum((item.faceless for item in hidden), ZERO))
        rows.append(
            [
                "Чего нет в этом отчёте",
                "строки отчёта о реализации за период",
                "-",
                f"За этот период Wildberries не разнёс по товарам: {names}. "
                f"Всего {amount} ₽. Колонок под эти статьи в листе «Прибыль по "
                "артикулам» нет, а деньги учтены в колонке «Расходы без "
                "артикула, ₽» и расшифрованы в листе "
                f"«{PROBLEMS_SHEET}».",
            ]
        )
    allocation = report.tax_by_article
    if allocation is not None and allocation.negative:
        # Строка появляется только тогда, когда минус в книге действительно
        # есть: постоянное предупреждение про случай, которого нет, читать
        # перестанут, и вместе с ним перестанут читать остальное.
        rows.append(
            [
                "Налог с минусом",
                "выручка артикула за вычетом возвратов",
                "ставка * (выручка - возвраты)",
                TAX_NEGATIVE_METHOD.format(
                    names=_article_list(allocation.negative),
                    amount=_cell(allocation.negative_total),
                ),
            ]
        )
    unknown = report.unknown_doc_types
    if unknown:
        # Тот же разговор, что в сообщении, но здесь он останется с книгой:
        # селлер открывает её через месяц и уже не помнит текста.
        rows.append(
            [
                "Незнакомый тип документа",
                "docTypeName строки отчёта о реализации",
                "-",
                "За этот период Wildberries прислал строки с типом документа, "
                f"которого бот не знает: {'; '.join(unknown)}. Деньги из них в "
                "раскладку вошли, а проданные штуки нет: продажа это или нет, "
                "по типу не понять, а угадать значило бы наврать в "
                "себестоимости и в маржинальности.",
            ]
        )
    return rows


def excel_sheets(report: ProfitReport) -> list:
    """Четыре листа: полная таблица, проблемный блок, налог и методология."""
    from core.xlsx import Sheet

    hidden = _hidden_columns(report)
    columns = article_columns(report)
    # Раскладка налога считается один раз на книгу: она про весь период, а не
    # про строку, и пересчитывать её у каждого товара незачем.
    allocation = report.tax_by_article

    def tax_cells(item: ArticleProfit) -> list[Any]:
        """Клетки налога товара. Режим не выбран - клеток нет вовсе.

        Налог известен и у товара без посчитанной прибыли: он считается от
        выручки, а она есть всегда. Прибыли после налога у такого товара не
        бывает, и там стоит «нет данных», а не ноль: ноль прочитался бы как
        «отработали в ноль».
        """
        if allocation is None:
            return []
        amount = allocation.of(item.nm_id)
        return [_cell(amount), _cell(report.after_tax_of(item, amount))]

    articles = [
        _visible(
            [
                item.nm_id,
                item.vendor_code,
                item.display_name,
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
            + tax_cells(item),
            columns,
            hidden,
        )
        for item in report.articles
    ]
    if report.articles:
        articles.append(_visible(_totals_values(report, allocation), columns, hidden))
        # Под итогом остались слова, а не суммы: суммы стоят в колонках. Без
        # выбранного режима колонок налога нет, и строка говорит, что прибыль
        # показана до налога.
        articles += [_visible(row, columns, hidden) for row in tax_total_rows(report)]

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
    # Компенсации. Блок стоит перед обезличкой по той же причине, по которой
    # она вообще здесь: это деньги, которые в основной расчёт не вошли, и
    # селлер должен найти их по имени, а не обнаружить, что товар исчез.
    problems += [
        [
            COMPENSATION_BLOCK,
            item.nm_id,
            item.vendor_code,
            _cell(item.compensation),
            _compensation_note(item),
        ]
        for item in report.compensations
    ]
    if report.unallocated != ZERO:
        problems.append(
            [
                "обезличка",
                None,
                "",
                _cell(report.unallocated),
                "Расходы из строк отчёта без артикула. Разнесены по артикулам "
                "пропорционально выручке; не разнесённый остаток "
                f"{_cell(report.unallocated_left)} ₽ в прибыль не вошёл. "
                "Из чего она складывается, написано в строках ниже.",
            ]
        )
        # Расшифровка. Сумма этих строк равна самой обезличке: селлер видит,
        # что хранение посчитано, а не забыто.
        problems += [
            [
                f"обезличка: {item.title.lower()}",
                None,
                "",
                _cell(item.faceless),
                _faceless_note(item),
            ]
            for item in report.faceless_parts
        ]

    return [
        Sheet(ARTICLES_SHEET, article_headers(report), articles),
        Sheet(PROBLEMS_SHEET, PROBLEM_HEADERS, problems),
        Sheet(TAX_SHEET, TAX_HEADERS, tax_rows(report)),
        Sheet(METHOD_SHEET, METHOD_HEADERS, method_rows(report)),
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
    # За названиями идём уже зная, какие артикулы в отчёте: спрашивать WB о
    # каталоге до расчёта незачем, а на цифры названия не влияют.
    names = await collect_names(
        client_id, [item.nm_id for item in report.articles], path=path
    )
    report = with_names(report, names)
    if _sender is None:
        raise RuntimeError("некому отправить отчёт о прибыли: доставка не подключена")
    result = _sender(client_id, report, excel_bytes(report))
    if hasattr(result, "__await__"):
        await result
    return report


def register_jobs() -> None:
    """Связывает вид задачи с обработчиком. Зовёт сборка бота, не импорт."""
    queue.register(TASK_KIND, report_task, title="прибыль по артикулам")
