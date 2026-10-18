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

Названия товаров приходят вместе с деньгами: в отчёте о реализации есть поле
`title`, и у каждой строки с артикулом оно заполнено (проверено на живом
кабинете, 28 968 строк, пустое название встречается только в строках без
артикула, то есть в служебных). Поэтому название берётся из той же строки,
что и комиссия, и за ним никуда не ходят.

Справочник карточек (`POST /content/v2/get/cards/list`, категория токена
«Контент», таблица `card_names`) остался запасным путём, а не привычкой: в
WB за ним идут только тогда, когда артикул остался безымянным. Так бывает у
строк, записанных прежней версией бота без `raw`. Срок жизни названия
(`config.toml`, секция `[cards]`) при этом остался при справочнике: он решает,
когда переспросить каталог про те артикулы, чьё имя известно только из него.
Товар, переименованный в кабинете, доезжает до отчёта сам, со следующей
неделей отчёта о реализации.

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

**Реклама приходит двумя путями и вычитается один раз.** Счёт рекламного
кабинета Wildberries удерживает из отчёта о реализации, а статистика кампаний
отдаёт тот же расход по товарам и по дням. Счёт выставлен одной неделей, показы
были в другие дни, поэтому на годе два числа почти сходятся, а на коротком
периоде расходятся в разы. Счёт заменяется расходом по товарам ровно в той
части, которую этот расход объяснил (`ad_replaced_by_stats`), остаток счёта
остаётся расходом и разносится вместе с обезличкой. Так рекламных денег в
расходах всегда ровно столько, сколько их в большем из двух источников: ни
двойного счёта, ни подаренной прибыли, ни отрицательных расходов без артикула.

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

# Второе исключение из правила «агент не знает про агента», и оно названо,
# как и первое. Хранение и приёмку по товарам Wildberries отдаёт отдельными
# отчётами, собирает их агент 8 (`agents/storage.py`), и колонки этих отчётов
# живут только здесь: отдельной команды у них нет. Брать их больше негде, а
# копия сборщика в этом файле означала бы второй поход в WB за теми же
# деньгами.
from agents import storage as storage_module
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
    "AD_DEDUCTIONS_KEY",
    "COST_ITEMS",
    "NOT_SUMMABLE",
    "TOTAL_LABEL",
    "TAX_NOTE_LABEL",
    "TAX_OFF_LABEL",
    "TAX_UNPRICED_LABEL",
    "BEFORE_TAX",
    "COMPENSATION_BLOCK",
    "AD_DEDUCTION_BLOCK",
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
    "ReportSpend",
    "ProfitReport",
    "Totals",
    "margin",
    "share",
    "spread",
    "ad_totals",
    "ad_replaced_by_stats",
    "replaced_by_report",
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
    # Хранение и приёмка по отдельным отчётам Wildberries (агент 8). `None`
    # значит «этих суток мы не собирали», а не «расхода не было»: нулём мы
    # приписали бы селлеру прибыль, которой у него может и не быть. Когда
    # число есть, в прибыль идёт оно, а статья из отчёта о реализации
    # вычитается уже не здесь, см. `storage_counted`.
    storage_report: Decimal | None = None
    acceptance_report: Decimal | None = None
    # Статья из отчёта о реализации вычтена сама, как раньше. Ложь ровно
    # тогда, когда у статьи есть второй источник по товарам: тогда её деньги
    # разобраны на заменённую часть (она вычтена колонкой второго отчёта) и
    # остаток (он ушёл в обезличку), и дважды не вычитается ничего.
    storage_counted: bool = True
    acceptance_counted: bool = True
    penalties: Decimal = ZERO
    # Удержания без рекламной части: счёт рекламного кабинета вычитается не
    # здесь, а рядом. Почему так - в `METHODOLOGY`.
    deductions: Decimal = ZERO
    # Сколько Wildberries удержал по этому артикулу за рекламу. В прибыль эта
    # сумма идёт только тогда, когда расхода по товарам нет вовсе
    # (`ad_deductions_counted`): иначе одни деньги вычлись бы дважды.
    ad_deductions: Decimal = ZERO
    ad_deductions_counted: bool = False
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
        """Всё, что Wildberries забрал по этому артикулу и вошло в прибыль.

        Неизвестная комиссия делает неизвестной и сумму: складывать её с
        нулём значит занизить расходы и завысить прибыль.

        Рекламное удержание прибавляется только тогда, когда оно и вычтено:
        при известном расходе по товарам реклама стоит в `ad_spend`, и второй
        раз её здесь быть не должно. Сумма этого свойства и есть то, что
        вычиталось, иначе проверять ей было бы нечего.

        Хранение и приёмка живут по тому же правилу: пока второго источника
        нет, они стоят здесь, а как только появился, их место занимают
        `storage_report` и `acceptance_report`.
        """
        if self.commission is None:
            return None
        return (
            self.commission
            + self.acquiring
            + self.logistics
            + (self.storage if self.storage_counted else ZERO)
            + (self.acceptance if self.acceptance_counted else ZERO)
            + self.penalties
            + self.deductions
            + (self.ad_deductions if self.ad_deductions_counted else ZERO)
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


# Ключ рекламного удержания. Назван константой, потому что по нему статья
# отличается от остальных в двух местах: в обезличку она входит не целиком,
# когда часть счёта уже вычтена расходом по товарам.
AD_DEDUCTIONS_KEY = "ad_deductions"


def replaced_by_report(billed: Decimal, by_article: Decimal | None) -> Decimal:
    """Сколько статьи из отчёта о реализации заменил второй отчёт по товарам.

    Правило одно на все такие статьи, и живёт оно здесь. Деньги приходят в
    отчёт двумя путями: отчётом о реализации (документ взаиморасчётов, часто
    без артикула) и отдельным отчётом Wildberries, где та же сумма разнесена
    по товарам и суткам. Вычесть оба раза значит отнять у селлера прибыль,
    которой он не терял; вычесть только второй значит выбросить из расходов
    настоящие деньги в той части, которую второй отчёт не объяснил.

    Поэтому заменённым считается наименьшее из двух: больше, чем сам второй
    отчёт, он объяснить не может, а больше, чем выставлено, заменять нечего.
    Остаток остаётся расходом и назван в отчёте словами. Так в расходах
    всегда ровно столько денег, сколько их в большем из двух источников: один
    раз и не меньше одного раза.

    `None` во втором источнике это «его нет вовсе»: заменять нечем.
    """
    if by_article is None or billed <= ZERO or by_article <= ZERO:
        return ZERO
    return min(billed, by_article)


def ad_replaced_by_stats(
    ad_deductions: Decimal, ad_spend: Decimal | None
) -> Decimal:
    """Сколько рекламного счёта заменила статистика кампаний.

    Счёт и показы живут в разных днях: Wildberries выставляет счёт своим
    числом и удерживает его той неделей, которой он дойдёт до отчёта о
    реализации, а статистика считает отработавшую рекламу по дням. На годе
    два числа почти сходятся, на коротком периоде расходятся в разы, и в
    какой именно день какого счёта отработал показ, Wildberries не говорит
    нигде.

    Поэтому заменённым считается наименьшее из двух: больше, чем сама
    статистика, она объяснить не может, а больше, чем выставлено, заменять
    нечего. Остаток счёта остаётся расходом и назван в отчёте словами. Любая
    пропорция здесь была бы числом, собранным из другого поля, то есть
    уверенным голосом сказанной неправдой.

    `None` в расходе по товарам это «статистики нет вовсе»: заменять нечем, и
    счёт вычитается сам.

    Само правило общее и живёт в `replaced_by_report`: у хранения и приёмки
    та же развилка, и вторая её копия разошлась бы с первой молча.
    """
    return replaced_by_report(ad_deductions, ad_spend)

# Статьи расходов Wildberries. Ключ это поле или свойство `finance.Amounts`,
# название то же, что в заголовке колонки. Порядок тот же, что в книге.
#
# Удержания разделены на две статьи нарочно. Счёт рекламного кабинета приходит
# в отчёт о реализации удержанием, и те же деньги приходят расходом по товарам
# из статистики кампаний: в одной статье с арендой приёмки и транзитной
# доставкой они бы вычлись дважды. Поэтому «прочие удержания» считаются по
# `other_deductions`, а реклама стоит своей строкой.
COST_ITEMS: tuple[tuple[str, str], ...] = (
    ("commission", "Комиссия WB"),
    ("acquiring", "Эквайринг"),
    ("logistics", "Логистика"),
    ("storage", "Хранение"),
    ("acceptance", "Приёмка"),
    ("penalties", "Штрафы"),
    (AD_DEDUCTIONS_KEY, "Реклама, удержано Wildberries"),
    ("other_deductions", "Прочие удержания"),
)


@dataclass(frozen=True)
class CostItem:
    """Статья расходов: сколько пришло по товарам, а сколько без них."""

    key: str
    title: str
    by_article: Decimal = ZERO
    faceless: Decimal = ZERO
    # Чем статья объяснена со второй стороны, словами: «отчёт WB о платном
    # хранении», «отчёт WB о приёмке». Пусто у статей без второго источника, и
    # тогда разговора о замене нет вовсе.
    source: str = ""
    # Сколько из безартикульной части статьи вошло в обезличку, то есть
    # разносится по товарам. `None` значит «вся», и так у всех статей, кроме
    # одной: у рекламного удержания в обезличке остаётся только та часть
    # счёта, которую не заменила статистика кампаний. Пересказывать `faceless`
    # в каждой статье смысла нет, поэтому здесь `None`, а не копия числа.
    pooled: Decimal | None = None

    @property
    def in_pool(self) -> Decimal:
        """Часть статьи, которая разносится по товарам из обезлички.

        Сумма этого свойства по всем статьям равна самой обезличке, и именно
        это число печатается в расшифровке: покажи там полную статью, и
        сложение строк разошлось бы с колонкой «Расходы без артикула, ₽».
        """
        return self.faceless if self.pooled is None else self.pooled

    @property
    def in_unallocated(self) -> bool:
        """Статья участвует в обезличке хоть какой-то частью.

        У рекламного удержания это ложь ровно тогда, когда статистика
        кампаний объяснила счёт целиком: деньги статьи настоящие, но в
        обезличке их нет, и расшифровка обязана про неё молчать.
        """
        return self.in_pool != ZERO

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
class ReportSpend:
    """Статья, у которой есть второй источник: отдельный отчёт WB по товарам.

    Сейчас таких две: хранение и приёмка. В отчёте о реализации они приходят
    без артикула (хранение всегда, и это проверено живым кабинетом), а в своих
    отчётах Wildberries разносит их по товарам и суткам.

    Два числа под одним названием это главная опасность такой пары, поэтому
    здесь лежат оба: `billed` это статья отчёта о реализации, `by_article` это
    сумма второго отчёта по тем товарам, которые в прибыль и вошли. Разницу
    между ними отчёт называет вслух, а не прячет: именно она показывает
    перерасчёты, которых иначе не видно.
    """

    # Ключ статьи `finance.Amounts` (он же ключ колонки в книге) и источник
    # второго отчёта (`agents.storage`). Имена разные нарочно: первое это
    # наша раскладка, второе это отчёт Wildberries.
    key: str
    kind: str
    spend: Any
    billed: Decimal = ZERO
    by_article: Decimal = ZERO

    @property
    def available(self) -> bool:
        """Второй источник есть хоть за часть суток периода."""
        return bool(self.spend is not None and self.spend.available)

    @property
    def title(self) -> str:
        """Чем статья названа в книге: «Хранение», «Приёмка»."""
        return dict(COST_ITEMS).get(self.key, self.key)

    @property
    def source(self) -> str:
        return storage_module.SOURCE_TITLES.get(self.kind, self.kind)

    @property
    def column(self) -> str:
        return storage_module.COLUMN_TITLES.get(self.kind, self.kind)

    @property
    def column_key(self) -> str:
        """Ключ колонки книги, в которой стоит число второго отчёта."""
        return f"{self.key}_report"

    @property
    def counted(self) -> bool:
        """Статья отчёта о реализации вычитается сама, как до второго отчёта."""
        return not self.available

    @property
    def replaced(self) -> Decimal:
        """Часть статьи, которую объяснил второй отчёт. Правило общее на проект."""
        return replaced_by_report(
            self.billed, self.by_article if self.available else None
        )

    @property
    def uncovered(self) -> Decimal:
        """Остаток статьи, который остался расходом и ушёл в обезличку.

        Второго источника нет вовсе - ноль: тогда статья и так вычтена
        строками отчёта, и прибавлять её второй раз нельзя.
        """
        return ZERO if not self.available else self.billed - self.replaced

    @property
    def total(self) -> Decimal | None:
        """Сумма по товарам из второго отчёта или None, если его нет."""
        return self.by_article if self.available else None

    @property
    def gap(self) -> Decimal:
        """Насколько второй отчёт разошёлся с отчётом о реализации."""
        return self.by_article - self.billed

    @property
    def matches(self) -> bool:
        """Разница в пределах рубля: тот же порог, что в сверке `/finance`."""
        return abs(self.gap) <= finance.TOLERANCE

    @property
    def notable(self) -> bool:
        """Есть ли о чём говорить: хоть в одном из двух источников есть деньги.

        У кабинета владельца приёмка нулевая и там, и там. Абзац про ноль
        рублей в сводке, которую читают с телефона, стоит дороже самого нуля:
        колонка в книге остаётся (настоящий ноль это ответ, а не пробел), а
        разговор о разнице двух нулей не нужен никому.
        """
        return self.billed != ZERO or self.by_article != ZERO

    @property
    def days(self) -> int:
        return 0 if self.spend is None else int(self.spend.days)

    @property
    def days_total(self) -> int:
        return 0 if self.spend is None else int(self.spend.days_total)

    @property
    def complete(self) -> bool:
        """Второй отчёт собран за все сутки периода."""
        return bool(self.spend is not None and self.spend.complete)

    @property
    def collected(self) -> Decimal:
        """Весь расход собранных суток, включая строки без артикула."""
        return ZERO if self.spend is None else self.spend.collected

    def of(self, nm_id: int | None) -> Decimal | None:
        return None if self.spend is None else self.spend.of(nm_id)


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
    # Итоги колонок второго отчёта. `None` значит «этих суток не собирали»:
    # в клетке итога будет «нет данных», а не ноль.
    storage_report: Decimal | None = None
    acceptance_report: Decimal | None = None
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
    # Сколько Wildberries удержал за рекламу за период. Полем, а не свойством
    # по неделям: в `fin_weeks` колонки под эту статью нет, а новых миграций в
    # проекте не пишут. Поэтому число считается по строкам `fin_rows` в
    # `build`, и на уже выгруженных неделях оно верное сразу, а не «приедет со
    # следующей выгрузкой». Тем же способом сюда попадает комиссия: она тоже
    # поднимается из строк, а не из агрегата недели.
    ad_deductions: Decimal = ZERO
    # Из чего состоят расходы периода: по каждой статье отдельно то, что
    # Wildberries разнёс по товарам, и то, что отдал общей строкой.
    costs_breakdown: tuple[CostItem, ...] = ()
    # Статьи, у которых есть второй источник по товарам: хранение и приёмка.
    # Пусто значит «в этом отчёте их нет вовсе», и тогда всё считается ровно
    # как раньше.
    reports: tuple[ReportSpend, ...] = ()
    ads: AdSpend = None  # type: ignore[assignment]
    weeks: tuple[Any, ...] = ()
    # Период считан по датам операций, а не целыми неделями Wildberries.
    # Прибыль за названный месяц сводят с бухгалтерией, и способ счёта обязан
    # быть виден в отчёте, а не только в кнопке.
    exact: bool = False
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
        """Расшифровка обезлички: сумма их `in_pool` сходится с `unallocated`."""
        return tuple(
            item
            for item in self.costs_breakdown
            if item.faceless != ZERO and item.in_unallocated
        )

    @property
    def unshared_items(self) -> tuple[CostItem, ...]:
        """Статьи, которых в разрезе по товарам нет вовсе.

        Статья, не попавшая в обезличку, сюда не входит: о ней книга говорит
        своими словами, а не фразой «деньги в колонке расходов без артикула».
        """
        return tuple(
            item
            for item in self.costs_breakdown
            if item.only_faceless and item.in_unallocated
        )

    @property
    def ad_deductions_counted(self) -> bool:
        """Рекламное удержание вычитается само, а не расходом по товарам.

        Так бывает ровно в одном случае: расхода рекламы по товарам нет вовсе
        (нет категории «Продвижение», метод не ответил, за рекламой не
        ходили). Тогда счёт рекламного кабинета это единственное, что про
        рекламу известно, и выбросить его значило бы потерять настоящий
        расход. Когда расход по товарам есть, вычитается он: он привязан к
        товару, а удержание приходит общей суммой по кабинету.

        «Вычитается само» не значит «вычитается целиком одним путём»: при
        известной статистике часть счёта всё равно остаётся расходом, см.
        `ad_uncovered`.
        """
        return not self.ads.available

    @property
    def ad_replaced(self) -> Decimal:
        """Часть рекламного счёта, которую заменил расход по товарам.

        Ровно эта часть из расходов и убрана: иначе одни деньги вычлись бы
        дважды. Правило одно на весь проект и живёт в
        `ad_replaced_by_stats`, здесь оно только применяется к периоду.
        """
        return ad_replaced_by_stats(self.ad_deductions, self.total_ad_spend)

    @property
    def report_sources(self) -> tuple[ReportSpend, ...]:
        """Статьи со вторым источником, у которых он правда есть.

        По ним и строятся колонки книги, блок в листе проблем и разговор о
        разнице двух чисел. Источник, которого нет (сутки не собирали, нет
        категории токена «Аналитика»), в этом списке не стоит: колонка, в
        которой нечему появиться, хуже отсутствующей.
        """
        return tuple(item for item in self.reports if item.available)

    @property
    def partial_sources(self) -> tuple[ReportSpend, ...]:
        """Источники, собранные не за все сутки периода.

        Такой столбец занижен по сравнению с отчётом о реализации ровно на
        несобранные сутки, и это надо сказать числом, а не оставить селлеру
        на догадки.
        """
        return tuple(item for item in self.report_sources if not item.complete)

    @property
    def ad_uncovered(self) -> Decimal:
        """Часть рекламного счёта, которая осталась расходом как удержание.

        На годе это обычно ноль: статистика объясняет счёт целиком. На
        коротком периоде счёт больше отработавшей в нём рекламы, и остаток
        обязан остаться расходом, иначе отчёт подарил бы селлеру прибыль,
        которой у него нет, а расходы без артикула ушли бы в минус.
        """
        return self.ad_deductions - self.ad_replaced


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
            storage_report=self.report_total(storage_module.STORAGE),
            acceptance_report=self.report_total(storage_module.ACCEPTANCE),
            penalties=money("penalties"),
            deductions=money("deductions"),
            ad_spend=self.total_ad_spend,
            unallocated=money("unallocated"),
            compensation=money("compensation"),
            compensation_units=sum(item.compensation_units for item in self.articles),
            profit=profit_total,
            margin=margin(profit_total, counted_revenue),
        )

    def report_of(self, kind: str) -> ReportSpend | None:
        """Статья со вторым источником по его имени или None."""
        for item in self.reports:
            if item.kind == kind:
                return item
        return None

    def report_total(self, kind: str) -> Decimal | None:
        """Итог колонки второго отчёта или None, если источника нет.

        Сумма берётся та же, что вычтена по товарам: расход товара, которого
        в периоде не продавали, в прибыли не стоит нигде, и в итог колонки он
        тоже не идёт.
        """
        found = self.report_of(kind)
        return None if found is None else found.total

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
                # Та же двойная трата, что была в прибыли, и лечится она тем
                # же способом: удержания Wildberries уже включают выставленный
                # счёт за рекламу, а строкой ниже прибавляется расход из
                # статистики кампаний. Дважды принятый расход занижает базу и
                # сам налог, то есть обещает селлеру налог меньше настоящего,
                # а это хуже, чем завысить.
                #
                # Вычитается не весь счёт, а только заменённая его часть. Счёт
                # и дни показов расходятся по времени, и на коротком периоде
                # счёт больше отработавшей рекламы: вычти его целиком, и в
                # расходах не стало бы денег, которые Wildberries правда
                # удержал. Когда статистики нет вовсе, заменять нечем, и в
                # базу идёт сам счёт.
                #
                # Число берётся у отчёта, а не у `amounts`: агрегат недели
                # лежит в `fin_weeks`, колонки под эту статью там нет, и
                # считается она по строкам отчёта о реализации. Ровно так же
                # здесь появляется комиссия по артикулам.
                - self.ad_replaced
                + totals.cost
                + (ad_spend if ad_spend is not None else ZERO)
            )
            # Та же развилка у хранения и приёмки, и ошибка была бы та же:
            # удержанное Wildberries уже стоит в `amounts.costs`, а колонка
            # второго отчёта прибавляется рядом. Вычитается не вся статья, а
            # только заменённая её часть, то есть в базе остаётся большее из
            # двух чисел. Дважды принятый расход занижает базу и обещает
            # селлеру налог меньше настоящего, а это хуже, чем завысить.
            #
            # Несобранные сутки базу не занижают: пока второй отчёт меньше
            # удержанного, он вычитается ровно на себя же, и в базе остаётся
            # вся статья отчёта о реализации. Поэтому оговорки о полноте
            # второго отчёта здесь нет: оговаривать нечего.
            for source in self.report_sources:
                expenses = expenses - source.replaced + (source.total or ZERO)
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
    def cabinet_profit(self) -> Decimal | None:
        """Прибыль по кабинету для итога и для сообщения, или None.

        Прибыль складывается только по тем артикулам, у которых она посчитана,
        поэтому ноль из пустого множества это не «не заработали», а «не
        посчитано ни по одному». Книга и сообщение берут это число отсюда, а
        не считают каждое своё: разойдясь, они дали бы два ответа на один
        вопрос.
        """
        return self.total_profit if self.totals.priced else None

    @property
    def profit_after_tax(self) -> Decimal | None:
        """Прибыль за вычетом налога кабинета целиком. Лист «Налог».

        Это расчёт от общей базы: из прибыли вычтена вся оценка, включая налог
        тех артикулов, прибыль по которым не посчитана. Число честное, но
        неоднородное, поэтому стоит оно только на листе «Налог» рядом с
        оговоркой. В сообщении и в итоге листа артикулов стоит
        `cabinet_after_tax`: там налог и прибыль взяты по одним и тем же
        товарам.
        """
        estimate = self.tax
        return None if estimate is None else estimate.after(self.total_profit)

    @property
    def tax_in_profit(self) -> Decimal | None:
        """Налог, который из прибыли вычтен: налог артикулов с прибылью.

        Сумма столбца налога минус налог тех артикулов, у которых прибыли
        после налога нет. Именно это число вычитается из прибыли и в итоге
        книги, и в сообщении, поэтому оно и названо: по нему селлер проверяет
        вычитание, и других слагаемых в нём нет.
        """
        allocation = self.tax_by_article
        if allocation is None:
            return None
        rest = allocation.total - (self.tax_without_profit or ZERO)
        return rest.quantize(CENT, rounding=ROUND_HALF_UP)

    @property
    def cabinet_after_tax(self) -> Decimal | None:
        """Прибыль после налога по кабинету: одно число на книгу и сообщение.

        Сумма столбца «прибыль после налога», то есть прибыль минус налог тех
        же самых товаров. Селлер читает сводку с телефона, потом открывает
        файл, и два разных числа под одним названием стоят ему доверия к
        обоим, поэтому источник у них один: и итоговая клетка книги, и строка
        сообщения берутся отсюда.

        Прибыль не посчитана ни по одному артикулу - здесь None, а не минус
        налог: вычитать его не из чего.
        """
        allocation = self.tax_by_article
        if allocation is None or not self.totals.priced:
            return None
        return sum(
            (
                value
                for value in (
                    self.after_tax_of(item, allocation.of(item.nm_id))
                    for item in self.articles
                )
                if value is not None
            ),
            ZERO,
        )

    @property
    def tax_outside_profit(self) -> Decimal | None:
        """Налог, из прибыли не вычтенный, если он больше копеек округления.

        Это налог артикулов без посчитанной прибыли и налог той части базы, у
        которой артикула нет вовсе. Из прибыли он не вычитается: вычитать его
        не из чего, а подставить ноль прибыли значило бы соврать.

        Копеечная разница здесь None нарочно. Столбец округляется построчно, и
        расхождение в пределах `Allocation.rounding_limit` это только
        округление: оговорка о двух копейках в сводке, которую читают с
        телефона, стоит дороже самих двух копеек. Книга называет их на листе
        «Методология», где место есть.
        """
        estimate = self.tax
        taken = self.tax_in_profit
        if estimate is None or taken is None:
            return None
        rest = (estimate.amount - taken).quantize(CENT, rounding=ROUND_HALF_UP)
        allocation = self.tax_by_article
        return None if abs(rest) <= allocation.rounding_limit else rest

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
        if self.exact:
            return finance.period_title(self.date_from, self.date_to)
        return finance.PERIOD_TITLES.get(self.period, self.period)

    @property
    def incomplete(self) -> tuple[Any, ...]:
        """Недели, выгрузка которых у агента 1 упёрлась в потолок страниц."""
        return tuple(week for week in self.weeks if not week.complete)

    @property
    def unverified(self) -> tuple[Any, ...]:
        """Недели, которые агент 1 не сверил с итогами Wildberries.

        Обрезанные периодом недели сюда не идут: про них говорится отдельно.
        «Итоги не отданы» лечится повторным запросом, «неделя вошла не
        целиком» не лечится вовсе, и одна подсказка на двоих врала бы.
        """
        return tuple(
            week
            for week in self.weeks
            if not week.verified and not getattr(week, "clipped", False)
        )

    @property
    def clipped(self) -> tuple[Any, ...]:
        """Недели, вошедшие в период не целиком: сверить их нечем."""
        return tuple(week for week in self.weeks if getattr(week, "clipped", False))

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
    window: Any = None,
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
    for week in sorted(weeks, key=lambda item: item.report_id):
        for row in db.repo(client_id, path).rows("fin_rows", report_id=week.report_id):
            # Точный период смотрит только на свои строки: артикул, у которого
            # строка без `raw` лежит в августе, в сентябрьской прибыли ни при
            # чём, и прятать его прибыль из-за неё было бы напрасно.
            if not finance.row_taken(row["rr_dt"], week, window):
                continue
            if row["nm_id"] is not None and not row["raw"]:
                found.add(int(row["nm_id"]))
    return found



# --- сборка отчёта -----------------------------------------------------------


def _source_available(sources: Sequence[ReportSpend], key: str) -> bool:
    """Есть ли у статьи второй источник. Отвечает на вопрос одной строки."""
    return any(source.key == key and source.available for source in sources)


def build(
    client_id: int,
    period: "str | finance.Period" = "week",
    *,
    today: date | None = None,
    ads: AdSpend | None = None,
    path: str | Path | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
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
    window = finance.period_of(period, today, date_from=date_from, date_to=date_to)
    date_from, date_to = window.bounds
    # Точный период (названный месяц и свои даты) считается по датам операций
    # и ровно так же, как в /finance: одно определение периода на оба отчёта,
    # иначе сентябрь в прибыли разошёлся бы с сентябрём в деньгах.
    exact = window if window.exact else None
    weeks = (
        finance.exact_weeks(client_id, window, path=path)
        if window.exact
        else finance.weeks_of(client_id, date_from, date_to, path=path)
    )
    # Разрез по артикулам один на проект, и живёт он у агента 1. Своей копии
    # здесь нет намеренно: два места, где решается, что такое «выручка
    # артикула» и «возврат», разошлись бы молча. Комиссия там уже считается по
    # `vw`, то есть та же, что в /finance.
    pieces = finance.articles_of(client_id, weeks, path=path, window=exact)
    unknown = _articles_without_raw(client_id, weeks, path=path, window=exact)
    ads = ads or AdSpend({}, available=False, reason=ADS_NOT_REQUESTED)

    named = [item for item in pieces if item.nm_id is not None]
    faceless = [item for item in pieces if item.nm_id is None]

    # Реклама вычитается один раз, и здесь решается, каким путём. Wildberries
    # выставляет за неё счёт и удерживает его из отчёта о реализации строкой
    # «Удержание», а те же деньги приходят в отчёт вторым путём: расходом по
    # товарам из статистики кампаний. Пока удержание лежало в общей куче
    # расходов без артикула, одни и те же рубли вычитались дважды, и на живом
    # кабинете это было полмиллиона в год.
    #
    # Когда расход по товарам известен, вычитается он: реклама привязана к
    # товару, а удержание приходит общей суммой по кабинету и разносилось бы
    # по всем поровну, то есть и на те товары, которые не рекламировали.
    # Когда расхода нет вовсе, вычитается удержание: это единственное, что
    # про рекламу известно, и выбросить его значило бы потерять настоящие
    # деньги. Обе дороги названы на листе «Методология».
    #
    # Заменяется при этом не весь счёт, а только та его часть, которую
    # статистика объяснила. Счёт выставлен одной неделей, а показы были в
    # другие дни, и на коротком периоде счёт больше отработавшей в нём
    # рекламы: вычтешь его целиком - и расходы без артикула уйдут в минус,
    # а минус в колонке расходов прочитается как прибавка к прибыли. На живом
    # кабинете это было 20 тысяч минуса за квартал.
    ads_counted = not ads.available
    # По всем строкам периода, и с артикулом, и без него: счёт приходит
    # обезличенным, но у другого кабинета Wildberries может разнести его по
    # товарам, и тогда он тоже обязан попасть в это число.
    ad_deductions = sum((item.amounts.ad_deductions for item in pieces), ZERO)
    # Расход рекламы, который правда вошёл в прибыль: сумма по артикулам
    # отчёта, а не весь ответ WB. Расход товара, которого в периоде не
    # продавали, в прибыли не стоит нигде, и заменять им счёт нельзя: иначе
    # эти деньги исчезли бы из расходов совсем.
    ad_spend = (
        sum((ads.of(item.nm_id) or ZERO for item in named), ZERO)
        if ads.available
        else None
    )
    # Готовый отчёт считает то же число свойством `ad_replaced`: это второй
    # вызов того же правила, а не вторая формула, поэтому разойтись им негде.
    ad_replaced = ad_replaced_by_stats(ad_deductions, ad_spend)
    # Остаток счёта, который статистика не объяснила. Товара за ним не стоит:
    # это деньги за дни, которые в период не попали, поэтому он идёт в
    # обезличку и разносится по тому же правилу, что хранение и приёмка.
    # Когда статистики нет вовсе, счёт и так остался в расходах строк, и
    # прибавлять его второй раз нельзя.
    ad_uncovered = ZERO if ads_counted else ad_deductions - ad_replaced

    # Хранение и приёмка приходят в отчёт тем же двойным путём, что реклама, и
    # разбираются тем же правилом. В отчёте о реализации они лежат статьёй
    # (у хранения всегда без артикула), а в своих отчётах Wildberries разносит
    # их по товарам и суткам. Собранное лежит в базе, в WB отсюда не ходят.
    sources = tuple(
        ReportSpend(
            key=key,
            kind=kind,
            spend=storage_module.spend_of(client_id, kind, date_from, date_to, path=path),
            billed=sum((getattr(item.amounts, key) for item in pieces), ZERO),
        )
        for kind, key in (
            (storage_module.STORAGE, "storage"),
            (storage_module.ACCEPTANCE, "acceptance"),
        )
    )
    # Сумма по товарам считается только по артикулам отчёта, как и у рекламы:
    # расход товара, которого в периоде не продавали, в прибыли не стоит
    # нигде, и заменять им статью нельзя, иначе эти деньги исчезли бы из
    # расходов совсем.
    sources = tuple(
        replace(
            source,
            by_article=sum((source.of(item.nm_id) or ZERO for item in named), ZERO),
        )
        for source in sources
    )

    def spent(amounts: Any) -> Decimal:
        """Расходы Wildberries, которые идут в прибыль по этим строкам."""
        total = amounts.costs - (ZERO if ads_counted else amounts.ad_deductions)
        for source in sources:
            # У статьи есть второй источник: её деньги разобраны отдельно, на
            # заменённую часть и остаток, и в расходах строки им места нет.
            if source.available:
                total -= getattr(amounts, source.key)
        return total

    # Обезличка: расходы строк без артикула. Разносим пропорционально выручке,
    # потому что другого честного признака у этих строк нет. Правило названо
    # на листе «Методология», а не спрятано здесь.
    # Неразобранные остатки статей со вторым источником ложатся в обезличку
    # ровно так же, как остаток рекламного счёта: эти деньги Wildberries
    # правда удержал, а товара за ними не стоит.
    unallocated = (
        sum((spent(item.amounts) for item in faceless), ZERO)
        + ad_uncovered
        + sum((source.uncovered for source in sources), ZERO)
    )
    weights = {
        item.nm_id: item.amounts.revenue - item.amounts.returns_amount for item in named
    }
    parts = spread(unallocated, {k: v for k, v in weights.items() if v > ZERO})
    left = unallocated.quantize(CENT, rounding=ROUND_HALF_UP) - sum(parts.values(), ZERO)

    known = costs_module.costs_for(client_id, nm_ids=list(weights), path=path)
    # Названия приходят вместе с деньгами: в отчёте о реализации есть поле
    # `title`, и у каждой строки с артикулом оно заполнено. Справочник
    # карточек остаётся запасным путём и читается здесь же, из базы: он
    # нужен строкам, записанным прежней версией бота без `raw`, и кабинету,
    # у которого Wildberries названия в отчёте не прислал. В WB из расчёта
    # не ходят вовсе.
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
        # Хранение и приёмка по товару из своих отчётов Wildberries. `None`
        # значит «источника нет», и тогда статья вычтена в `spent`, как
        # раньше: двух путей у одних денег не остаётся ни в одном случае.
        by_report = {source.key: source.of(item.nm_id) for source in sources}
        value = None
        if cost is not None and known_commission:
            value = (
                net_revenue
                - cost
                - spent(amounts)
                - (ad or ZERO)
                - sum((value or ZERO for value in by_report.values()), ZERO)
                - piece
            ).quantize(CENT, rounding=ROUND_HALF_UP)
        rows.append(
            ArticleProfit(
                nm_id=item.nm_id,
                vendor_code=item.vendor_code,
                subject=item.subject,
                # Название из отчёта о реализации сильнее справочника: оно
                # приехало вместе с деньгами этого самого периода, а в
                # справочнике лежит имя на день последнего похода в каталог.
                title=item.title or titles.get(item.nm_id, ""),
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
                storage_report=by_report.get("storage"),
                acceptance_report=by_report.get("acceptance"),
                storage_counted=not _source_available(sources, "storage"),
                acceptance_counted=not _source_available(sources, "acceptance"),
                penalties=amounts.penalties,
                deductions=amounts.other_deductions,
                ad_deductions=amounts.ad_deductions,
                ad_deductions_counted=ads_counted,
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
    #
    # У статей с двумя источниками в обезличку идёт не вся статья, а только
    # неразобранный её остаток: заменённая часть вычтена колонкой второго
    # отчёта. Покажи здесь всю статью, и расшифровка обещала бы селлеру
    # деньги, которых в колонке «Расходы без артикула, ₽» нет, и перестала бы
    # с ней сходиться.
    pooled_parts: dict[str, Decimal] = {
        source.key: source.uncovered for source in sources if source.available
    }
    if not ads_counted:
        pooled_parts[AD_DEDUCTIONS_KEY] = ad_uncovered
    sources_by_key = {source.key: source.source for source in sources if source.available}
    breakdown = tuple(
        CostItem(
            key=key,
            title=title,
            by_article=sum((getattr(item.amounts, key) for item in named), ZERO),
            faceless=sum((getattr(item.amounts, key) for item in faceless), ZERO),
            # Чем статья объяснена со второй стороны. Пусто у тех статей, у
            # которых второго источника нет вовсе.
            source=sources_by_key.get(key, ""),
            pooled=pooled_parts.get(key),
        )
        for key, title in COST_ITEMS
    )
    return ProfitReport(
        client_id=client_id,
        period=window.key,
        date_from=date_from,
        date_to=date_to,
        exact=window.exact,
        articles=articles,
        unallocated=unallocated,
        unallocated_left=left,
        ad_deductions=ad_deductions,
        costs_breakdown=breakdown,
        reports=sources,
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

# Подпись блока рекламного счёта. Деньги настоящие и крупные, а в расходах их
# нет, потому что они уже вычтены другим путём: без названного блока селлер
# решит, что отчёт их потерял.
AD_DEDUCTION_BLOCK = "реклама, удержано WB"

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
    # Колонка второго отчёта стоит рядом со статьёй отчёта о реализации, а не
    # вместо неё: в книге всегда видна ровно одна из двух (см.
    # `_hidden_columns`), и читается она там, где селлер её и ищет.
    (storage_module.COLUMN_TITLES[storage_module.STORAGE], "storage_report"),
    ("Приёмка, ₽", "acceptance"),
    (storage_module.COLUMN_TITLES[storage_module.ACCEPTANCE], "acceptance_report"),
    ("Штрафы, ₽", "penalties"),
    ("Прочие удержания, ₽", "other_deductions"),
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
        "поле title строки отчёта о реализации",
        "название из самой поздней строки артикула",
        "Название приходит вместе с деньгами, той же строкой отчёта. Если "
        "товар переименовали, в отчёте за длинный период лежат оба имени, и в "
        "книгу идёт то, под которым товар продавался последним: именно его "
        "селлер видит в кабинете сейчас. Справочник карточек товаров "
        "(категория токена «Контент») остался запасным путём: в Wildberries за "
        "названием бот идёт только тогда, когда в строках отчёта его не "
        "оказалось. Если названия нет нигде, в колонке остаётся артикул WB: "
        "выдуманное имя было бы хуже.",
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
        "в колонке «Расходы без артикула, ₽». У хранения и приёмки есть "
        "второй путь: отдельные отчёты Wildberries, где та же сумма стоит по "
        "товарам и по суткам. Когда он собран, колонка берётся оттуда и "
        "названа своим источником, а в пропорции от выручки остаётся только "
        "неразобранный остаток статьи.",
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
        "«нет данных», а не ноль, и прибыль считается без рекламы. Это "
        "статистический расход кампаний: сколько реклама отработала. Сколько "
        "за неё выставили счётом, стоит в строке «Реклама, удержано "
        "Wildberries» и совпадает не до копейки, потому что счёт выставляется "
        "своим числом и попадает в отчёт о реализации своей неделей.",
    ),
    (
        "Реклама, удержано Wildberries",
        "обоснование bonusTypeName строки отчёта о реализации",
        "сумма deduction по строкам с обоснованием «WB Продвижение»",
        "Счёт рекламного кабинета Wildberries удерживает из отчёта о "
        "реализации: строка приходит удержанием, а за что удержали, написано в "
        "обосновании словами самого Wildberries. Эти же деньги приходят в "
        "отчёт вторым путём, расходом по товарам из статистики кампаний, "
        "поэтому в прибыль они идут один раз. Заменяется счёт не целиком, а "
        "на столько, сколько дал расход по товарам: счёт выставлен одной "
        "неделей, а показы были в другие дни, и на коротком периоде счёт "
        "больше отработавшей в нём рекламы. Заменённая часть вычтена в "
        "колонке «Реклама, ₽» и здесь стоит справкой, а неразобранный остаток "
        "счёта остаётся расходом и входит в «Расходы без артикула, ₽»: эти "
        "деньги Wildberries правда удержал. Больше, чем сама статистика, "
        "заменить нельзя, поэтому рекламных денег в расходах всегда ровно "
        "столько, сколько их в большем из двух источников. Любая пропорция "
        "была бы числом, которого у Wildberries нет. Если расхода по товарам "
        "нет вовсе, вычитается весь счёт: это всё, что про рекламу известно, "
        "и выбросить его значило бы подарить селлеру прибыль, которой у него "
        "нет. Удержания, обоснованные чем-то другим, остаются в «Прочих "
        "удержаниях» целиком: незнакомое удержание расходом быть не "
        "перестаёт.",
    ),
    (
        "Расходы без артикула, ₽",
        "строки отчёта о реализации без nmId",
        "разносятся пропорционально выручке артикула за вычетом возвратов",
        "Это правило, а не факт от Wildberries. Артикулы без выручки доли не "
        "получают. Если выручки нет вовсе, расходы остаются в блоке "
        "«обезличка» и в прибыль не входят. Из рекламного счёта здесь только "
        "тот остаток, который не заменил расход по товарам: почему, написано "
        "в строке «Реклама, удержано Wildberries».",
    ),
    (
        "Чистая прибыль, ₽",
        "все источники выше",
        "выручка - возвраты - себестоимость - расходы WB - реклама - "
        "расходы без артикула",
        "Расходы WB это комиссия, эквайринг, логистика, хранение, приёмка, "
        "штрафы и прочие удержания. Заменённой расходом по товарам части "
        "рекламного счёта среди них нет: эта реклама вычитается строкой "
        "«Реклама, ₽», и вычитается один раз. Неразобранный остаток счёта "
        "стоит в «Расходах без артикула, ₽», и вычитается тоже один раз. То же "
        "правило у хранения и приёмки, когда собран их собственный отчёт: "
        "вычитается колонка этого отчёта плюс неразобранный остаток статьи, то "
        "есть ровно столько, сколько денег в большем из двух источников.",
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
        "и без выставленного счёта за рекламу + реклама",
        "Только на УСН «доходы минус расходы». Штрафы Wildberries в базу не "
        "идут: такие расходы обычно не принимаются. Выставленный счёт за "
        "рекламу вычтен из удержаний в той части, которую заменил расход по "
        "товарам, потому что реклама прибавляется строкой рядом: дважды "
        "принятый расход занизил бы базу и обещал бы налог меньше настоящего. "
        "Остаток счёта в базе остаётся: эти деньги Wildberries удержал, а дни "
        "показов за них в период не попали. Если расхода рекламы по товарам "
        "нет, в базу идёт весь счёт. Себестоимость принимается "
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
    """Статьи, под которые колонки в книге не будет.

    Три повода, и все про одно: колонка, которая никогда не заполнится, хуже
    отсутствующей. Статью, которую Wildberries по товарам не разнёс, убирает
    правило `CostItem.only_faceless`. Колонку второго отчёта убирает
    отсутствие самого отчёта: сутки не собирали, или у токена нет категории
    «Аналитика». А когда второй отчёт есть, уходит колонка той же статьи из
    отчёта о реализации: две колонки под одно хранение читались бы как два
    разных расхода, и сложение строки перестало бы сходиться с прибылью.
    """
    hidden = {item.key for item in report.unshared_items}
    hidden.update({"storage_report", "acceptance_report"})
    for source in report.report_sources:
        hidden.discard(source.column_key)
        hidden.add(source.key)
    return frozenset(hidden)


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

    Сумму столбца прибыли после налога эта функция не считает сама:
    `report.cabinet_after_tax` отдаёт то же число и сообщению бота. Своим
    расчётом здесь книга и сводка однажды уже разошлись.
    """
    if allocation is None:
        return []
    return [
        _cell(allocation.total),
        _cell(report.cabinet_after_tax),
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
        _cell(totals.storage_report),
        _cell(totals.acceptance),
        _cell(totals.acceptance_report),
        _cell(totals.penalties),
        _cell(totals.deductions),
        _cell(totals.ad_spend),
        _cell(totals.unallocated),
        _cell(report.cabinet_profit),
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


def _ad_deduction_note(report: ProfitReport) -> str:
    """Куда делся рекламный счёт. Главный вопрос здесь «почему не вычли».

    Пустая строка в блоке была бы хуже её отсутствия: деньги большие, и
    селлер, не найдя их в расходах, решит, что отчёт их потерял. Поэтому
    рядом стоит и расход из статистики кампаний, и сказано, какая часть счёта
    заменена расходом по товарам, а какая осталась расходом сама.
    """
    text = (
        "Столько Wildberries удержал за рекламу: выставил счёт рекламного "
        "кабинета и забрал его из отчёта о реализации. В колонке «Реклама, ₽» "
        "стоит не этот счёт, а расход из статистики кампаний: только она "
        "знает, какой именно товар рекламировали, а счёт приходит общей "
        "суммой по кабинету и разошёлся бы поровну по всем, включая те, что "
        "никто не рекламировал."
    )
    spend = report.total_ad_spend
    if spend is not None:
        text += (
            f" Статистика кампаний за период даёт {_cell(spend)} ₽. Числа и не "
            "обязаны совпасть: статистика считает отработавшую рекламу по дням "
            "периода, а счёт Wildberries выставляет своим числом и удерживает "
            "той неделей, которой дойдёт до отчёта. Ту же разницу показывает "
            "/ads, двумя цифрами расхода."
        )
        rest = report.ad_uncovered.quantize(CENT, rounding=ROUND_HALF_UP)
        if rest > ZERO:
            text += (
                f" Поэтому из счёта вычтено по товарам {_cell(report.ad_replaced)} ₽, "
                f"а остаток {_cell(rest)} ₽ остался расходом и разнесён вместе с "
                "обезличкой: эти деньги Wildberries правда удержал, а дни "
                "показов за них в период не попали. Дважды реклама не "
                "вычитается нигде."
            )
        elif rest < ZERO:
            text += (
                " Счёт в этом периоде ушёл в минус (Wildberries вернул больше, "
                "чем удержал), поэтому в расходах он стоит возвратом, а реклама "
                "вычтена расходом по товарам."
            )
        else:
            text += (
                " Статистика объяснила счёт целиком, поэтому второй раз он в "
                "расходы не идёт: реклама вычтена расходом по товарам, и "
                "вычтена один раз."
            )
    text += (
        " Удержания с другим обоснованием из расходов никуда не делись, они в "
        "колонке «Прочие удержания, ₽»."
    )
    return text


def _report_note(source: ReportSpend) -> str:
    """Что означает блок второго отчёта. Главный вопрос здесь «какому верить».

    Поэтому в строке стоят оба числа и разница между ними, а не одно число с
    обещанием, что оно сойдётся. Wildberries сойтись не обещал: причины
    расходиться у него в документации названы прямо, и здесь они названы
    селлеру его словами.
    """
    text = (
        f"Столько {source.title.lower()} насчитал {source.source}: там эта "
        "сумма стоит по каждому товару и по каждым суткам. Ровно эти числа и "
        f"стоят в колонке «{source.column}». В отчёте о реализации та же "
        f"статья за период это {_cell(source.billed)} ₽, и приходит она общей "
        "суммой по кабинету, без артикула."
    )
    if not source.matches:
        text += (
            f" Разница {_cell(source.gap)} ₽. Два отчёта и не обязаны сойтись: "
            "Wildberries этого нигде не обещает, а причины расходиться назвал "
            "сам. Перерасчёты задним числом (сутки, за которые начислено, и "
            "сутки первоначального расчёта это разные поля), сутки против "
            "недель отчёта о реализации, скидка программы лояльности отдельным "
            "полем и разные сервисы Wildberries за двумя отчётами. Ту же "
            "разницу бот показывает в сверке /finance и в двух цифрах расхода "
            "/ads."
        )
    else:
        text += " Разница в пределах рубля, то есть два отчёта сошлись."
    if not source.complete:
        text += (
            f" Суток периода собрано {source.days} из {source.days_total}: "
            "окно одного запроса у этого отчёта короткое, и история копится "
            "ночным сбором. Поэтому сумма по товарам за несобранные сутки "
            "меньше, и разница выше включает их."
        )
    if source.collected != source.by_article:
        text += (
            f" По всем строкам собранных суток вышло {_cell(source.collected)} ₽, "
            f"а по товарам отчёта {_cell(source.by_article)} ₽: остальное это "
            "строки без артикула и товары, которых в этом периоде не "
            "продавали. В прибыль по товарам идёт только второе: расход "
            "товара, которого в периоде нет, вычитать не из чего."
        )
    rest = source.uncovered.quantize(CENT, rounding=ROUND_HALF_UP)
    if rest > ZERO:
        text += (
            f" Из статьи отчёта о реализации по товарам вычтено "
            f"{_cell(source.replaced)} ₽, а остаток {_cell(rest)} ₽ остался "
            "расходом и разнесён вместе с обезличкой: эти деньги Wildberries "
            "правда удержал. Дважды не вычитается ничего."
        )
    elif rest < ZERO:
        text += (
            " В отчёте о реализации эта статья за период ушла в минус "
            "(Wildberries вернул больше, чем удержал), поэтому в расходах она "
            "стоит возвратом, а по товарам вычтен второй отчёт."
        )
    else:
        text += (
            " Второй отчёт объяснил статью целиком, поэтому второй раз она в "
            "расходы не идёт."
        )
    text += (
        " В отчёте /finance ничего не подменено: там стоит сумма из отчёта о "
        "реализации, это документ взаиморасчётов, и сверка с итогами "
        "Wildberries стоит на нём."
    )
    return text


def _faceless_note(item: CostItem) -> str:
    """Что означает строка расшифровки. Читает это селлер, а не программист."""
    if item.key == AD_DEDUCTIONS_KEY and item.in_pool != item.faceless:
        # Единственная статья, которая попадает в обезличку не целиком. Без
        # этой фразы селлер сложил бы строку расшифровки с колонкой «Реклама,
        # ₽» и решил бы, что рекламу посчитали дважды.
        return (
            f"{item.title}: в этих деньгах только та часть счёта рекламного "
            "кабинета, которую статистика кампаний не объяснила. Остальное "
            "вычтено по товарам в колонке «Реклама, ₽», а здесь остался "
            "остаток: счёт выставлен одной неделей, а показы были в другие "
            "дни. Весь счёт целиком назван выше, в блоке "
            f"«{AD_DEDUCTION_BLOCK}»."
        )
    if item.source and item.in_pool != item.faceless:
        # Статья с двумя источниками: в обезличке остался только тот её
        # остаток, который второй отчёт не объяснил. Без этой фразы селлер
        # сложил бы строку расшифровки с колонкой второго отчёта и решил бы,
        # что одни деньги посчитаны дважды.
        return (
            f"{item.title}: в отчёте о реализации эта статья приходит общей "
            f"суммой по кабинету, а по товарам её разносит {item.source}. В "
            "этих деньгах остался только тот остаток, который второй отчёт не "
            "объяснил: разобранная им часть вычтена по товарам в своей "
            "колонке. Почему два числа про одни деньги не обязаны сойтись, "
            f"написано в листе «{METHOD_SHEET}»."
        )
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
    if report.exact:
        # Первое, что спросит бухгалтер: сходится ли это с кабинетом WB. У
        # точного периода ответ «не весь», и сказать это надо в книге, а не
        # только в сообщении: книгу открывают через месяц.
        clipped = len(report.clipped)
        tail = (
            ""
            if not clipped
            else f" Недель, вошедших в период не целиком: {clipped}. Сверить "
            "их с итогами Wildberries нечем: готовых сумм на часть недели он "
            "не отдаёт."
        )
        rows.append(
            [
                "Как посчитан период",
                "rrDate строки отчёта о реализации",
                f"строки с датой операции от {report.date_from.isoformat()} "
                f"до {report.date_to.isoformat()} включительно",
                "Период посчитан точно по датам операций, а не целыми "
                "неделями Wildberries: в него вошло ровно запрошенное, ни дня "
                "соседнего месяца." + tail,
            ]
        )
    for source in report.report_sources:
        # Строка появляется только у того источника, который в этом отчёте
        # правда есть: правило про колонку, которой нет, читать незачем.
        rows.append(
            [
                source.column,
                f"{source.source} (категория токена «Аналитика»)",
                "сумма по артикулу за сутки периода",
                f"Это ВТОРОЙ источник тех же денег. В отчёте о реализации "
                f"{source.title.lower()} приходит общей суммой по кабинету "
                "и по товарам не разносится вовсе, поэтому колонка по товарам "
                "иначе была бы нашей пропорцией от выручки, а хранение от "
                "выручки не зависит: оно зависит от объёма товара и времени на "
                "складе, и крупный медленный товар получал бы копейки. "
                "Wildberries нигде не обещает, что суммы двух отчётов "
                "совпадут, и причины расходиться назвал сам: перерасчёты "
                "задним числом, сутки против недель, скидка программы "
                "лояльности отдельным полем, разные сервисы. Поэтому колонка "
                "названа своим источником, а оба числа и разница между ними "
                f"стоят в листе «{PROBLEMS_SHEET}», блоком "
                f"«{storage_module.BLOCK_TITLES.get(source.kind, source.kind)}». "
                "В прибыль деньги идут один раз: статья отчёта о реализации "
                "заменена на столько, сколько объяснил второй отчёт, а "
                "неразобранный остаток остался расходом и разнесён вместе с "
                "обезличкой. В отчёте /finance не подменено ничего.",
            ]
        )
    hidden = report.unshared_items
    if hidden:
        names = ", ".join(item.title.lower() for item in hidden)
        # Сумма по тому, что правда попало в обезличку: у рекламного счёта это
        # его неразобранный остаток, а не весь счёт, и строка обещает деньги
        # ровно в той колонке, где они лежат.
        amount = sum((item.in_pool for item in hidden), ZERO)
        note = (
            f"За этот период Wildberries не разнёс по товарам: {names}. "
            f"Всего {_cell(amount)} ₽. Колонок под эти статьи в листе «Прибыль по "
            "артикулам» нет, а деньги учтены в колонке «Расходы без "
            "артикула, ₽» и расшифрованы в листе "
            f"«{PROBLEMS_SHEET}»."
        )
        # Колонка редко равна этой сумме, и молчать об этом нельзя: селлер
        # складывает числа и не сходится. В обезличку попадают и части тех
        # статей, у которых своя колонка есть (комиссия приходит и строками
        # без артикула), а они бывают отрицательными, и тогда колонка меньше
        # названной суммы. Сказать это числом дешевле, чем заставлять искать
        # разницу по листу проблем.
        if report.unallocated != amount:
            note += (
                f" В самой колонке стоит {_cell(report.unallocated)} ₽: туда же "
                "идут части статей, у которых своя колонка есть, и Wildberries "
                "отдал их строками без артикула. Такая часть бывает "
                "отрицательной, и тогда колонка меньше суммы выше. Полный "
                f"состав обезлички разобран в листе «{PROBLEMS_SHEET}»."
            )
        rows.append(
            [
                "Чего нет в этом отчёте",
                "строки отчёта о реализации за период",
                "-",
                note,
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
                _cell(item.storage_report),
                _cell(item.acceptance),
                _cell(item.acceptance_report),
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
    # Хранение и приёмка по своим отчётам. Блок стоит перед обезличкой по той
    # же причине, что рекламный счёт: в колонке книги стоит число одного
    # отчёта, в /finance число другого, и селлер обязан найти оба по имени, а
    # не обнаружить расхождение сам.
    problems += [
        [
            storage_module.BLOCK_TITLES.get(source.kind, source.kind),
            None,
            "",
            _cell(source.total),
            _report_note(source),
        ]
        for source in report.report_sources
        if source.notable
    ]
    # Рекламный счёт. Стоит перед обезличкой, потому что раньше он лежал
    # внутри неё и вычитался вторым разом: деньги большие, и селлер обязан
    # найти их по имени, а не считать, что они пропали из расходов.
    if report.ad_deductions != ZERO and not report.ad_deductions_counted:
        problems.append(
            [
                AD_DEDUCTION_BLOCK,
                None,
                "",
                _cell(report.ad_deductions),
                _ad_deduction_note(report),
            ]
        )
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
                _cell(item.in_pool),
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
    client_id: int,
    period: "str | finance.Period" = "week",
    *,
    path: str | Path | None = None,
) -> queue.TaskId:
    """Ставит отчёт в очередь. «Принято» клиенту говорит сама очередь.

    Из хендлера в WB не ходят: за рекламой идёт обработчик задачи, и повтор
    при недоступности Wildberries живёт только в очереди.
    """
    return queue.enqueue(
        client_id, TASK_KIND, finance.period_of(period).payload, path=path
    )


def _request_missing(
    client_id: int, window: Any, *, path: str | Path | None = None
) -> list[Any]:
    """Просит собрать те сутки периода, которых во втором отчёте нет.

    Окно просьбы ограничено сверху: собирать всё недостающее одной работой
    значило бы занять кабинет на часы. Сколько окон за раз, решает владелец
    (`config.toml`, секция `[storage]`), и то же число ночной сбор берёт на
    догон истории: два места, где это решается, разошлись бы молча.
    """
    asked: list[Any] = []
    limit = storage_module.backfill_windows()
    if limit <= 0:
        return asked
    for kind in storage_module.KINDS:
        gaps = storage_module.missing_days(
            client_id, kind, window.date_from, window.date_to, path=path
        )
        if not gaps:
            continue
        windows = storage_module.windows_for(kind, min(gaps), max(gaps))[:limit]
        if not windows:
            continue
        try:
            asked.append(
                storage_module.request_collect(
                    client_id,
                    windows[0][0],
                    windows[-1][1],
                    kind=kind,
                    path=path,
                )
            )
        except Exception:  # noqa: BLE001 - отчёт не обязан падать из-за добора
            logger.exception(
                "не удалось поставить сбор %s клиента %s", kind, client_id
            )
    return asked


async def report_task(task: Any, *, path: str | Path | None = None) -> ProfitReport:
    """Обработчик задачи: сходить за рекламой, собрать отчёт, отдать клиенту."""
    client_id = int(task.client_id)
    window = finance.period_of_task(task)
    ads = await collect_ads(client_id, window.date_from, window.date_to, path=path)
    report = build(client_id, window, ads=ads, path=path)
    # В каталог карточек идём только за тем, что осталось безымянным. Отчёт о
    # реализации называет товар сам (поле `title`), и у кабинета владельца
    # безымянных артикулов не остаётся вовсе: поход в WB за названиями
    # пропадает целиком. Справочник остаётся запасным путём, а не привычкой.
    names = await collect_names(
        client_id,
        [item.nm_id for item in report.articles if not item.title],
        path=path,
    )
    report = with_names(report, names)
    # Дыру в истории хранения и приёмки закрывает отдельная работа, а не этот
    # отчёт. Собрать год задним числом это 46 заданий по дорожке «1 запрос в
    # минуту», больше часа, и всё это время у кабинета занято его единственное
    # место в очереди: селлер, попросивший /finance, ждал бы этот час. Поэтому
    # отчёт уходит с тем, что собрано, говорит об этом вслух, а дыра начинает
    # закрываться сама. Повтор той же просьбы ничего не удваивает: одинаковую
    # задачу очередь дважды не ставит.
    _request_missing(client_id, window, path=path)
    if _sender is None:
        raise RuntimeError("некому отправить отчёт о прибыли: доставка не подключена")
    result = _sender(client_id, report, excel_bytes(report))
    if hasattr(result, "__await__"):
        await result
    return report


def register_jobs() -> None:
    """Связывает вид задачи с обработчиком. Зовёт сборка бота, не импорт."""
    queue.register(TASK_KIND, report_task, title="прибыль по артикулам")
