"""Агент 1, финансист: недельная раскладка отчёта о реализации.

Сердце модуля `finance`. Забирает детализацию отчёта о реализации, кладёт
сырые строки в `fin_rows`, агрегаты недели в `fin_weeks` и отдаёт короткую
сводку плюс книгу Excel на четыре листа.

Три решения, которые видно прямо здесь.

**Метод из ТЗ отключён Wildberries 15.07.2026.** Используется замена,
`POST /api/finance/v1/sales-reports/detailed` на `finance-api`, категория
токена «Финансы». Имена полей camelCase, старых snake_case больше нет.
Обёртка живёт в `core.wbapi`, своих запросов к WB здесь нет ни одного.

**Проценты не пересчитываются.** У Wildberries уже есть готовые поля
`commissionPercent`, `kvw`, `acquiringPercent` и `spp`, а к перечислению -
`forPay`. Свой пересчёт гарантированно разошёлся бы с личным кабинетом,
а требование R165 говорит ровно обратное. По неделе показывается
средневзвешенное готового поля, вес - `retailPriceWithDisc`.

**Штуки считаются только у продаж и возвратов.** Тип документа заполнен
у Wildberries только в этих двух видах строк; логистика, хранение, приёмка и
удержания приходят с пустым `docTypeName`, а `quantity` в них при этом бывает
проставлено. Служебная строка отдаёт свои деньги и не отдаёт штуки, иначе
себестоимость в `/profit` считается по числу, которое ничего не значит.
Продажа с нулевой выручкой тоже не продажа: так приходит возмещение за
выбывший товар, и у него свой вид строки (`COMPENSATION`).

**Неделя сверяется с агрегатом.** У `sales-reports/list` есть готовые суммы
по отчёту (`forPaySum`, `retailAmountSum` и прочие). Считаем неделю по
строкам и сверяем; расхождение показывается честно, а не прячется.

Публичный интерфейс этого агента - не только функции. Таблицы `fin_weeks` и
`fin_rows` заполняет он, а агенты 2 (сторож расходов) и 3 (прибыльность)
читают их и в WB не ходят вообще. Единицы описаны в `interfaces.md`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from core import db, queue, wbapi

logger = logging.getLogger(__name__)

__all__ = [
    "MODULE",
    "TASK_KIND",
    "COLLECT_KIND",
    "PERIODS",
    "PERIOD_TITLES",
    "SALE",
    "RETURN",
    "COMPENSATION",
    "SERVICE",
    "UNKNOWN",
    "doc_kind",
    "row_kind",
    "Amounts",
    "Week",
    "Month",
    "Article",
    "Check",
    "Collected",
    "FinanceReport",
    "period_bounds",
    "collect",
    "save_rows",
    "save_week",
    "aggregate",
    "build",
    "weeks_of",
    "articles_of",
    "months_of",
    "request_report",
    "request_collect",
    "report_task",
    "collect_task",
    "deliver",
    "set_sender",
    "register_jobs",
    "excel_bytes",
    "file_name",
]

MODULE = "finance"

# Виды фоновых задач. Регистрируются в одном явном месте, при сборке бота:
# `bot.handlers.finance.register(app)`.
#
# Их два, и это не украшение. `finance_report` это просьба клиента: собрать и
# сразу отдать. `finance_collect` это то же самое наполнение `fin_weeks` и
# `fin_rows`, но молча: его ставит расписание (`agents/lifecycle.py`), чтобы
# новые недели появлялись у всех, а не только у тех, кто позвал `/finance`.
# На появление нового report_id срабатывает сторож скрытых расходов.
TASK_KIND = "finance_report"
COLLECT_KIND = "finance_collect"

# Сколько дней назад смотрим для каждой кнопки. Год выгружается страницами и
# идёт долго: лимит отчёта о реализации 1 запрос в минуту, самый жёсткий в
# проекте, поэтому работа всегда через очередь.
PERIODS: dict[str, int] = {"week": 7, "month": 31, "quarter": 92, "year": 365}

PERIOD_TITLES: dict[str, str] = {
    "week": "последняя неделя",
    "month": "месяц",
    "quarter": "квартал",
    "year": "год",
}

# Данные отчёта о реализации начинаются 29 января 2024 года, раньше просить
# нечего.
DATA_SINCE = date(2024, 1, 29)

# Размер страницы и потолок числа страниц. Заданы здесь, а не взяты по
# умолчанию в клиенте: по ним считается, упёрлась ли выгрузка в потолок.
PAGE_LIMIT = 1000
MAX_PAGES = 500

# Поля ответа WB, на которых стоит раскладка. Это справочник для «Методологии»
# и `CLAUDE.md`, а НЕ список `fields` в запросе: параметр `fields` мы не шлём
# намеренно. Колонки `fin_rows` (`srid`, `barcode`, `techSize`, даты
# документов) и обещание агентам 10 и 11 «в `raw` лежит вся строка WB» иначе
# оказались бы пустыми, а следующий агент пошёл бы за полем в пустое место.
USED_FIELDS: tuple[str, ...] = (
    "reportId",
    "rrdId",
    "dateFrom",
    "dateTo",
    "rrDate",
    "nmId",
    "vendorCode",
    "subjectName",
    "docTypeName",
    "sellerOperName",
    "quantity",
    "retailPrice",
    "retailAmount",
    "retailPriceWithDisc",
    "salePercent",
    "spp",
    "commissionPercent",
    "kvwBase",
    "kvw",
    "vw",
    "vwNds",
    "ppvzSalesCommission",
    "ppvzReward",
    "forPay",
    "acquiringFee",
    "acquiringPercent",
    "acquiringBank",
    "deliveryAmount",
    "returnAmount",
    "deliveryService",
    "rebillLogisticCost",
    "paidStorage",
    "paidAcceptance",
    "penalty",
    "additionalPayment",
    "deduction",
)

ZERO = Decimal("0")

# Расхождение с агрегатом WB меньше рубля это округление, а не ошибка.
TOLERANCE = Decimal("1")


# --- разбор значений ---------------------------------------------------------


def money(value: Any) -> Decimal:
    """Сумма из строки ответа WB в `Decimal`. `float` для денег не бывает."""
    if value is None or value == "":
        return ZERO
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).replace(" ", "").replace(",", "."))
    except (InvalidOperation, ValueError):
        return ZERO


def _kop(value: Any) -> int:
    return db.to_kop(money(value))


def _int(value: Any) -> int:
    try:
        return int(Decimal(str(value or 0)))
    except (InvalidOperation, ValueError):
        return 0


def _real(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _day(value: Any) -> str:
    """Дата WB в вид ГГГГ-ММ-ДД. В базе всё без часовых поясов и в UTC."""
    text = str(value or "")
    return text[:10]


def vw_of(row: Any) -> Decimal:
    """Комиссия одной строки `fin_rows`: поле `vw` из сохранённого `raw`.

    Отдельной колонки под `vw` в схеме нет, а менять схему нельзя, поэтому
    значение берётся из `raw` - той самой строки WB, которую мы сохранили
    целиком. Основание одно и то же и здесь, и у агента 3 (прибыльность
    артикулов): иначе один артикул за одну неделю покажет в `/finance` одну
    комиссию, а в `/profit` другую, и селлер решит, что один из отчётов врёт.
    """
    raw = row["raw"] if "raw" in row.keys() else None
    if not raw:
        return ZERO
    try:
        return money(json.loads(raw).get("vw"))
    except (TypeError, ValueError):
        return ZERO


# --- к чему относится строка отчёта ------------------------------------------
#
# У Wildberries тип документа заполнен только у двух видов строк: «Продажа» и
# «Возврат». Служебные строки отчёта (логистика, хранение, приёмка, штрафы,
# удержания, возмещения издержек) приходят с пустым `docTypeName`. Деньги в
# них настоящие, а вот `quantity` проставлено, хотя ничего не продано: на
# живом кабинете это 256 служебных строк на 257 «штук» при 67 настоящих
# проданных.
#
# Поэтому штуки считаются только там, где Wildberries прямо сказал, что это
# продажа или возврат. Правило «не возврат значит продажа» и было ошибкой: к
# 67 проданным штукам прибавлялись 257 служебных, себестоимость выходила
# впятеро больше выручки, а маржинальность - 329 процентов.
#
# Значение сверяется целиком, а не по вхождению слова, и `sellerOperName`
# в сравнении не участвует. Вхождение уже подвело один раз: обоснование
# «Добровольная компенсация при возврате» содержит слово «возврат», хотя
# документ это продажа, и три штуки ушли бы в возвраты.
#
# Но одним типом документа дело не кончилось. Той же «Продажей» Wildberries
# называет возмещение за товар, который покупатель вернул или потерял: деньги
# приходят в `forPay`, `quantity` проставлено, а `retailAmount` ровно ноль,
# потому что покупатель не платил ничего. Товар по такой строке выбыл, а не
# продался, и на остатках его больше нет. Продажа без цены это не продажа
# товара, и это видно по числу, а не по тексту обоснования, поэтому вид строки
# решается типом документа вместе с суммой.
SALE = "sale"
RETURN = "return"
COMPENSATION = "compensation"
SERVICE = "service"
UNKNOWN = "unknown"

# Типы документов, которые бот знает в лицо. Список короткий намеренно:
# угадывать незнакомое значение не на чем.
DOC_TYPES: dict[str, str] = {"продажа": SALE, "возврат": RETURN}


def doc_kind(value: Any, amount: Any) -> str:
    """Вид строки отчёта: продажа, компенсация, возврат, услуга, незнакомое.

    Сумма здесь обязательный аргумент, а не удобство. Отдельный классификатор
    «по типу» рядом с этим завёлся бы сам и разошёлся бы с ним молча, а
    необязательный аргумент забыли бы ровно в том месте, где он и нужен: так
    и появилась компенсация, посчитанная продажей.

    Продажа с нулевым `retailAmount` это компенсация: товар выбыл, а не
    продался, поэтому ни выручки, ни проданных штук он не даёт. Деньги по ней
    настоящие и лежат в `forPay`, они остаются в «к перечислению» и показаны
    отдельной статьёй.

    Возврат с нулевой суммой компенсацией не становится: правило написано про
    продажу, и расширять его на вид строки, которого у Wildberries не видели,
    значит угадывать.

    Пустой тип это служебная строка: она отдаёт свои деньги и не отдаёт штуки.
    Незнакомый тип продажей молча не становится - штуки с него не берутся, а
    само значение уходит в `Week.unknown_doc_types` и в журнал, чтобы владелец
    решил, что это, а не узнал об этом из завышенной себестоимости.
    """
    name = str(value or "").strip().lower()
    if not name:
        return SERVICE
    kind = DOC_TYPES.get(name, UNKNOWN)
    if kind == SALE and money(amount) == ZERO:
        return COMPENSATION
    return kind


def row_kind(row: dict) -> str:
    """То же самое для строки ответа WB."""
    return doc_kind(row.get("docTypeName"), row.get("retailAmount"))


# --- результат ---------------------------------------------------------------


@dataclass(frozen=True)
class Amounts:
    """Статьи раскладки в рублях. Ровно то, что просит E2."""

    revenue: Decimal = ZERO
    returns_amount: Decimal = ZERO
    for_pay: Decimal = ZERO
    commission: Decimal = ZERO
    acquiring: Decimal = ZERO
    logistics: Decimal = ZERO
    storage: Decimal = ZERO
    acceptance: Decimal = ZERO
    penalties: Decimal = ZERO
    deductions: Decimal = ZERO
    additional_payment: Decimal = ZERO
    # Возмещение за выбывший товар: сумма `forPay` по строкам-компенсациям и
    # сколько товара по ним выбыло. В `for_pay` эти же деньги уже сидят, здесь
    # они названы отдельно, а не прибавлены второй раз: сверка с `forPaySum`
    # Wildberries должна сходиться, а селлеру нужно знать, откуда поступление
    # при нулевой выручке.
    compensation: Decimal = ZERO
    compensation_units: int = 0
    sales_count: int = 0
    returns_count: int = 0

    def __add__(self, other: "Amounts") -> "Amounts":
        return Amounts(
            revenue=self.revenue + other.revenue,
            returns_amount=self.returns_amount + other.returns_amount,
            for_pay=self.for_pay + other.for_pay,
            commission=self.commission + other.commission,
            acquiring=self.acquiring + other.acquiring,
            logistics=self.logistics + other.logistics,
            storage=self.storage + other.storage,
            acceptance=self.acceptance + other.acceptance,
            penalties=self.penalties + other.penalties,
            deductions=self.deductions + other.deductions,
            additional_payment=self.additional_payment + other.additional_payment,
            compensation=self.compensation + other.compensation,
            compensation_units=self.compensation_units + other.compensation_units,
            sales_count=self.sales_count + other.sales_count,
            returns_count=self.returns_count + other.returns_count,
        )

    @property
    def costs(self) -> Decimal:
        """Всё, что Wildberries забрал за неделю."""
        return (
            self.commission
            + self.acquiring
            + self.logistics
            + self.storage
            + self.acceptance
            + self.penalties
            + self.deductions
        )


@dataclass(frozen=True)
class Check:
    """Сверка одной величины с агрегатом из `sales-reports/list`."""

    name: str
    ours: Decimal
    theirs: Decimal | None

    @property
    def diff(self) -> Decimal:
        return ZERO if self.theirs is None else self.ours - self.theirs

    @property
    def matches(self) -> bool:
        return self.theirs is None or abs(self.diff) <= TOLERANCE


@dataclass(frozen=True)
class Week:
    """Неделя отчёта: статьи, готовые проценты WB и результат сверки."""

    report_id: int
    date_from: str
    date_to: str
    amounts: Amounts = Amounts()
    commission_percent: Decimal | None = None
    acquiring_percent: Decimal | None = None
    spp: Decimal | None = None
    kvw: Decimal | None = None
    checks: tuple[Check, ...] = ()
    # Агрегат из sales-reports/list получен и сверка правда сделана. Неделя
    # без агрегата это «сверка не выполнена», а не «сошлось».
    verified: bool = False
    # Выгрузка не упёрлась в потолок страниц. Неполная неделя обязана
    # отличаться от полной: иначе половина года выглядит как год.
    complete: bool = True
    # Типы документов, которых бот не знает. Пусто у всех обычных недель.
    # Хранится рядом с признаком полноты, в `control_payload`: незнакомый тип
    # это не ошибка выгрузки, но и не мелочь, о которой можно промолчать.
    unknown_doc_types: tuple[str, ...] = ()

    def __getattr__(self, name: str) -> Any:
        # Статьи читаются и прямо с недели: week.revenue вместо
        # week.amounts.revenue. Дублировать тринадцать полей ради этого не
        # хочется.
        try:
            return getattr(self.__dict__["amounts"], name)
        except KeyError:
            raise AttributeError(name) from None

    @property
    def checked(self) -> bool:
        """Сверка сделана и сошлась. Без агрегата это не «сошлось»."""
        return self.verified and all(check.matches for check in self.checks)

    @property
    def mismatches(self) -> tuple[Check, ...]:
        return tuple(check for check in self.checks if not check.matches)

    @property
    def trustworthy(self) -> bool:
        """Неделю можно показывать как точную: и полная, и сверенная."""
        return self.complete and self.checked


@dataclass(frozen=True)
class Month:
    """Свод недель в месяц. Неделя относится к месяцу своего начала."""

    key: str
    amounts: Amounts = Amounts()
    weeks: int = 0

    def __getattr__(self, name: str) -> Any:
        try:
            return getattr(self.__dict__["amounts"], name)
        except KeyError:
            raise AttributeError(name) from None


@dataclass(frozen=True)
class Article:
    """Строка листа «По артикулам»."""

    nm_id: int | None
    vendor_code: str = ""
    subject: str = ""
    quantity: int = 0
    amounts: Amounts = Amounts()
    # Чем Wildberries обосновал компенсацию (`sellerOperName`), в порядке
    # появления. Вид строки по этому тексту не решается, он свободный; селлеру
    # он нужен затем, чтобы узнать поступление по имени, а не гадать, куда
    # делся товар.
    compensation_reasons: tuple[str, ...] = ()

    def __getattr__(self, name: str) -> Any:
        try:
            return getattr(self.__dict__["amounts"], name)
        except KeyError:
            raise AttributeError(name) from None


@dataclass(frozen=True)
class FinanceReport:
    """То, что уходит клиенту: сводка в сообщении и книга Excel."""

    client_id: int
    period: str
    date_from: date
    date_to: date
    weeks: tuple[Week, ...] = ()
    months: tuple[Month, ...] = ()
    articles: tuple[Article, ...] = ()

    @property
    def totals(self) -> Amounts:
        result = Amounts()
        for week in self.weeks:
            result = result + week.amounts
        return result

    @property
    def empty(self) -> bool:
        return not self.weeks

    @property
    def mismatches(self) -> tuple[Week, ...]:
        """Недели, где наша сумма разошлась с готовой суммой Wildberries."""
        return tuple(week for week in self.weeks if week.verified and not week.checked)

    @property
    def unverified(self) -> tuple[Week, ...]:
        """Недели, для которых агрегат WB не получен: сверка не выполнена."""
        return tuple(week for week in self.weeks if not week.verified)

    @property
    def incomplete(self) -> tuple[Week, ...]:
        """Недели, выгрузка которых упёрлась в потолок страниц."""
        return tuple(week for week in self.weeks if not week.complete)

    @property
    def unknown_doc_types(self) -> tuple[str, ...]:
        """Типы документов WB, которых бот не знает, за весь период.

        Штуки по таким строкам не посчитаны: продажа это или нет, неизвестно,
        а посчитать наугад один раз уже вышло боком. Деньги из этих строк
        в раскладку вошли.
        """
        return tuple(
            dict.fromkeys(
                name for week in self.weeks for name in week.unknown_doc_types
            )
        )

    @property
    def title(self) -> str:
        return PERIOD_TITLES.get(self.period, self.period)


# --- периоды -----------------------------------------------------------------


def period_bounds(period: str, today: date | None = None) -> tuple[date, date]:
    """Границы периода по имени кнопки. Раньше 29.01.2024 данных нет."""
    days = PERIODS.get(period)
    if days is None:
        raise ValueError(f"неизвестный период: {period}")
    end = today or datetime.now(timezone.utc).date()
    start = max(end - timedelta(days=days), DATA_SINCE)
    return start, end


# --- сбор из WB --------------------------------------------------------------


def aggregate(rows: Iterable[dict]) -> dict[int, Week]:
    """Раскладка строк отчёта по неделям. Чистая функция, тестируется прямо.

    Проценты не считаются из денег: берётся средневзвешенное готового поля
    Wildberries, вес - `retailPriceWithDisc` строки.
    """
    totals: dict[int, Amounts] = {}
    bounds: dict[int, tuple[str, str]] = {}
    weights: dict[int, dict[str, list[tuple[Decimal, Decimal]]]] = {}
    unknown: dict[int, dict[str, None]] = {}

    for row in rows:
        report_id = _int(row.get("reportId"))
        base = money(row.get("retailPriceWithDisc")) or money(row.get("retailAmount"))
        kind = row_kind(row)
        amount = money(row.get("retailAmount"))
        quantity = _int(row.get("quantity"))
        if kind == UNKNOWN:
            # Порядок появления сохраняем, повторы гасим: словарь вместо
            # множества ради этого и взят.
            unknown.setdefault(report_id, {})[str(row.get("docTypeName")).strip()] = None

        piece = Amounts(
            revenue=amount if kind == SALE else ZERO,
            returns_amount=amount if kind == RETURN else ZERO,
            for_pay=money(row.get("forPay")),
            commission=money(row.get("vw")),
            acquiring=money(row.get("acquiringFee")),
            logistics=money(row.get("deliveryService")),
            storage=money(row.get("paidStorage")),
            acceptance=money(row.get("paidAcceptance")),
            penalties=money(row.get("penalty")),
            deductions=money(row.get("deduction")),
            additional_payment=money(row.get("additionalPayment")),
            # Компенсация: деньги за выбывший товар. В `for_pay` строкой выше
            # они уже вошли, здесь названы отдельно.
            compensation=money(row.get("forPay")) if kind == COMPENSATION else ZERO,
            compensation_units=quantity if kind == COMPENSATION else 0,
            # Штуки только у продаж и возвратов. У служебной строки, у
            # компенсации и у незнакомого типа их нет, даже если Wildberries
            # проставил `quantity`: деньги в такой строке настоящие, а товар по
            # ней не продавался.
            sales_count=quantity if kind == SALE else 0,
            returns_count=quantity if kind == RETURN else 0,
        )
        totals[report_id] = totals.get(report_id, Amounts()) + piece
        bounds.setdefault(
            report_id, (_day(row.get("dateFrom")), _day(row.get("dateTo")))
        )
        bucket = weights.setdefault(report_id, {})
        for name, source in (
            ("commission_percent", "commissionPercent"),
            ("acquiring_percent", "acquiringPercent"),
            ("spp", "spp"),
            ("kvw", "kvw"),
        ):
            value = row.get(source)
            if value in (None, ""):
                continue
            bucket.setdefault(name, []).append((money(value), base))

    result: dict[int, Week] = {}
    for report_id, amounts in totals.items():
        date_from, date_to = bounds.get(report_id, ("", ""))
        percents = {
            name: _weighted(pairs) for name, pairs in weights.get(report_id, {}).items()
        }
        result[report_id] = Week(
            report_id=report_id,
            date_from=date_from,
            date_to=date_to,
            amounts=amounts,
            unknown_doc_types=tuple(unknown.get(report_id, {})),
            **percents,
        )
    return result


def _weighted(pairs: Sequence[tuple[Decimal, Decimal]]) -> Decimal | None:
    """Средневзвешенное готового процента WB. Вес - сумма строки."""
    if not pairs:
        return None
    total = sum((weight for _, weight in pairs), ZERO)
    if total == ZERO:
        return sum((value for value, _ in pairs), ZERO) / Decimal(len(pairs))
    return sum((value * weight for value, weight in pairs), ZERO) / total


def _row_values(row: dict) -> dict[str, Any]:
    """Строка WB в колонки `fin_rows`. Деньги в копейках, исходник в `raw`."""
    return {
        "report_id": _int(row.get("reportId")),
        "nm_id": _int(row.get("nmId")) or None,
        "sa_name": row.get("vendorCode") or None,
        "ts_name": row.get("techSize") or None,
        "subject_name": row.get("subjectName") or None,
        "barcode": row.get("barcode") or None,
        "doc_type_name": row.get("docTypeName") or None,
        "supplier_oper_name": row.get("sellerOperName") or None,
        "quantity": _int(row.get("quantity")),
        "retail_price_kop": _kop(row.get("retailPrice")),
        "retail_amount_kop": _kop(row.get("retailAmount")),
        "retail_price_withdisc_kop": _kop(row.get("retailPriceWithDisc")),
        "sale_percent": _real(row.get("salePercent")),
        "commission_percent": _real(row.get("commissionPercent")),
        "delivery_amount": _int(row.get("deliveryAmount")),
        "return_amount": _int(row.get("returnAmount")),
        "delivery_kop": _kop(row.get("deliveryService")),
        "ppvz_spp_prc": _real(row.get("spp")),
        "ppvz_kvw_prc_base": _real(row.get("kvwBase")),
        "ppvz_kvw_prc": _real(row.get("kvw")),
        "ppvz_sales_commission_kop": _kop(row.get("ppvzSalesCommission")),
        "ppvz_for_pay_kop": _kop(row.get("forPay")),
        "ppvz_reward_kop": _kop(row.get("ppvzReward")),
        "acquiring_fee_kop": _kop(row.get("acquiringFee")),
        "acquiring_percent": _real(row.get("acquiringPercent")),
        "acquiring_bank": row.get("acquiringBank") or None,
        "penalty_kop": _kop(row.get("penalty")),
        "additional_payment_kop": _kop(row.get("additionalPayment")),
        "storage_fee_kop": _kop(row.get("paidStorage")),
        "deduction_kop": _kop(row.get("deduction")),
        "acceptance_kop": _kop(row.get("paidAcceptance")),
        "rebill_logistic_cost_kop": _kop(row.get("rebillLogisticCost")),
        "bonus_type_name": row.get("bonusTypeName") or None,
        "order_dt": _day(row.get("orderDt")) or None,
        "sale_dt": _day(row.get("saleDt")) or None,
        "rr_dt": _day(row.get("rrDate")) or None,
        "srid": row.get("srid") or None,
        "raw": json.dumps(row, ensure_ascii=False),
    }


def save_rows(client_id: int, rows: Iterable[dict], *, path: str | Path | None = None) -> int:
    """Кладёт строки в `fin_rows`. Ключ `rrdId`: повтор заменяет, а не двоит.

    Это и есть R102 в коде: та же неделя, выгруженная второй раз, занимает
    ровно столько же строк.
    """
    repo = db.repo(client_id, path)
    saved = 0
    for row in rows:
        rrd_id = _int(row.get("rrdId"))
        if not rrd_id:
            continue
        repo.upsert("fin_rows", {"rrd_id": rrd_id}, **_row_values(row))
        saved += 1
    return saved


def save_week(
    client_id: int,
    week: Week,
    control: dict | None = None,
    *,
    complete: bool = True,
    path: str | Path | None = None,
) -> None:
    """Агрегаты недели в `fin_weeks`. Деньги в копейках, ключ `report_id`.

    В `control_payload` кладётся не только агрегат WB: рядом с ним живут
    признак полноты выгрузки, незнакомые типы документов и компенсации. Схему
    базы менять нельзя, а неполная неделя обязана отличаться от полной, иначе
    клиент получит уверенную неправильную цифру. Незнакомый тип документа лежит
    там же по той же причине: отчёт строится по базе, и без записи о нём
    находка сборщика до отчёта не доедет. Компенсации там же и по той же: без
    записи поступление при нулевой выручке осталось бы необъяснённым.
    """
    amounts = week.amounts
    payload = json.dumps(
        {
            "aggregate": control,
            "complete": bool(complete),
            "unknown_doc_types": list(week.unknown_doc_types),
            "compensation_kop": db.to_kop(amounts.compensation),
            "compensation_units": int(amounts.compensation_units),
        },
        ensure_ascii=False,
    )
    db.repo(client_id, path).upsert(
        "fin_weeks",
        {"report_id": week.report_id},
        date_from=week.date_from,
        date_to=week.date_to,
        revenue_kop=db.to_kop(amounts.revenue),
        for_pay_kop=db.to_kop(amounts.for_pay),
        commission_kop=db.to_kop(amounts.commission),
        acquiring_kop=db.to_kop(amounts.acquiring),
        logistics_kop=db.to_kop(amounts.logistics),
        storage_kop=db.to_kop(amounts.storage),
        penalties_kop=db.to_kop(amounts.penalties),
        deductions_kop=db.to_kop(amounts.deductions),
        additional_payment_kop=db.to_kop(amounts.additional_payment),
        acceptance_kop=db.to_kop(amounts.acceptance),
        returns_amount_kop=db.to_kop(amounts.returns_amount),
        sales_count=amounts.sales_count,
        returns_count=amounts.returns_count,
        control_for_pay_kop=(
            None if control is None else db.to_kop(money(control.get("forPaySum")))
        ),
        control_payload=payload,
        loaded_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    )


@dataclass(frozen=True)
class Collected:
    """Чем закончилась выгрузка. Усечение обязано быть видно снаружи."""

    rows: int = 0
    pages: int = 0
    weeks: int = 0
    truncated: bool = False
    verified: bool = False
    # Типы документов, которых бот не знает. Пусто у всех обычных выгрузок.
    unknown_doc_types: tuple[str, ...] = ()

    def __int__(self) -> int:
        return self.rows


async def collect(
    client_id: int,
    date_from: date,
    date_to: date,
    *,
    limit: int | None = None,
    max_pages: int = MAX_PAGES,
    http: Any = None,
    path: str | Path | None = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], Any] | None = None,
) -> Collected:
    """Выгружает период из WB и складывает в базу.

    Зовётся только из обработчика очереди: лимит 1 запрос в минуту, год идёт
    страницами и долго, а повтор при недоступности WB живёт в очереди.

    Возвращает не одно число, а `Collected`: сколько страниц прочитано и
    упёрлась ли выгрузка в потолок. Молча сохранить половину года и записать
    неделю как целую значит нарушить R165 тихо, а не громко.
    """
    client = wbapi.get_wb_client(client_id, http=http, path=path, clock=clock, sleep=sleep)
    page = int(limit) if limit is not None else PAGE_LIMIT
    # Обрезку не вычисляем по длинам: клиент сам говорит, кончились ли
    # страницы по потолку при живом курсоре. Ровно полная последняя страница
    # это штатный конец выгрузки, а не повод пугать клиента.
    downloaded = await client.sales_report_detailed_paged(
        date_from, date_to, period="weekly", limit=page, max_pages=max_pages
    )
    rows = downloaded.rows
    saved = save_rows(client_id, rows, path=path)
    pages = downloaded.pages
    truncated = downloaded.truncated

    control: dict[int, dict] = {}
    try:
        for item in await client.sales_reports_list(date_from, date_to):
            control[_int(item.get("reportId"))] = item
    except (wbapi.WBUnavailable, wbapi.WBRateLimited, wbapi.WBApiError) as error:
        # Гасим ровно то, ради чего ветка написана: недоступность и отказ
        # конкретного метода. Отозванный токен (401) и нехватка категории
        # (403) летят наружу: их разбирают очередь и core.clients, и без них
        # модули не встанут на паузу.
        logger.warning("агрегат sales-reports/list не получен: %s", error)

    weeks = aggregate(rows)
    for report_id, week in weeks.items():
        save_week(
            client_id,
            week,
            control.get(report_id),
            complete=not truncated,
            path=path,
        )
    if truncated:
        logger.warning(
            "выгрузка клиента %s упёрлась в потолок страниц: данные неполные", client_id
        )
    # Незнакомый тип документа сам по себе выгрузку не портит, но штуки с него
    # не взяты. Молчать об этом нельзя: именно так и появляется завышенная
    # себестоимость, о которой никто не догадывается.
    unknown = tuple(
        dict.fromkeys(name for week in weeks.values() for name in week.unknown_doc_types)
    )
    if unknown:
        logger.warning(
            "клиент %s: незнакомый тип документа WB %s, штуки по таким строкам "
            "не считаются",
            client_id,
            ", ".join(unknown),
        )
    return Collected(
        rows=saved,
        pages=pages,
        weeks=len(weeks),
        truncated=truncated,
        verified=bool(control),
        unknown_doc_types=unknown,
    )


# --- чтение из базы ----------------------------------------------------------


def _amounts_of_week(row: Any, payload: _Payload) -> Amounts:
    return Amounts(
        revenue=db.from_kop(row["revenue_kop"]),
        returns_amount=db.from_kop(row["returns_amount_kop"]),
        for_pay=db.from_kop(row["for_pay_kop"]),
        commission=db.from_kop(row["commission_kop"]),
        acquiring=db.from_kop(row["acquiring_kop"]),
        logistics=db.from_kop(row["logistics_kop"]),
        storage=db.from_kop(row["storage_kop"]),
        acceptance=db.from_kop(row["acceptance_kop"]),
        penalties=db.from_kop(row["penalties_kop"]),
        deductions=db.from_kop(row["deductions_kop"]),
        additional_payment=db.from_kop(row["additional_payment_kop"]),
        # Своей колонки под компенсации в схеме нет, новых миграций мы не
        # пишем: число приезжает из `control_payload`, где лежит рядом с
        # полнотой выгрузки.
        compensation=payload.compensation,
        compensation_units=payload.compensation_units,
        sales_count=int(row["sales_count"] or 0),
        returns_count=int(row["returns_count"] or 0),
    )


def _checks(amounts: Amounts, control: dict | None) -> tuple[Check, ...]:
    """Сверка с готовыми суммами отчёта. Показывается честно, а не прячется."""
    if not control:
        return ()
    pairs = (
        ("к перечислению", amounts.for_pay, "forPaySum"),
        ("продажи и возвраты", amounts.revenue + amounts.returns_amount, "retailAmountSum"),
        ("логистика", amounts.logistics, "deliveryServiceSum"),
        ("хранение", amounts.storage, "paidStorageSum"),
        ("приёмка", amounts.acceptance, "paidAcceptanceSum"),
        ("штрафы", amounts.penalties, "penaltySum"),
        ("прочие удержания", amounts.deductions, "deductionSum"),
    )
    result = []
    for title, ours, key in pairs:
        if key not in control:
            continue
        result.append(Check(title, ours, money(control.get(key))))
    return tuple(result)


def weeks_of(
    client_id: int,
    date_from: date,
    date_to: date,
    *,
    path: str | Path | None = None,
) -> tuple[Week, ...]:
    """Недели из `fin_weeks`, пересекающие период. Агент 2 читает так же."""
    start, end = date_from.isoformat(), date_to.isoformat()
    rows = db.repo(client_id, path).rows("fin_weeks", order_by="date_from")
    result = []
    for row in rows:
        if str(row["date_to"]) < start or str(row["date_from"]) > end:
            continue
        payload = _payload_of(row["control_payload"])
        amounts = _amounts_of_week(row, payload)
        result.append(
            Week(
                report_id=int(row["report_id"]),
                date_from=str(row["date_from"]),
                date_to=str(row["date_to"]),
                amounts=amounts,
                checks=_checks(amounts, payload.aggregate),
                verified=bool(payload.aggregate),
                complete=payload.complete,
                unknown_doc_types=payload.unknown_doc_types,
            )
        )
    return tuple(result)


@dataclass(frozen=True)
class _Payload:
    """Что лежит в `control_payload` недели, кроме агрегата WB."""

    aggregate: dict | None = None
    complete: bool = True
    unknown_doc_types: tuple[str, ...] = ()
    compensation: Decimal = ZERO
    compensation_units: int = 0


def _payload_of(raw: Any) -> _Payload:
    """Разбирает `control_payload`: агрегат WB, полнота, типы, компенсации.

    Колонку заполняет только `save_week` этого же модуля, поэтому форма ровно
    одна и распознавать её не по чему. Пустая колонка значит, что неделю
    записали без агрегата: сверка не выполнена, полнота под вопросом не
    ставится. Нет ключа компенсаций - неделю записала прежняя версия бота, и
    это «не знаем», а не «компенсаций не было»; ноль здесь честнее догадки,
    а настоящее число приедет со следующей выгрузкой недели.
    """
    if not raw:
        return _Payload()
    stored = json.loads(raw)
    return _Payload(
        aggregate=stored.get("aggregate"),
        complete=bool(stored.get("complete", True)),
        unknown_doc_types=tuple(
            str(name) for name in (stored.get("unknown_doc_types") or ())
        ),
        compensation=db.from_kop(int(stored.get("compensation_kop") or 0)),
        compensation_units=int(stored.get("compensation_units") or 0),
    )


def _report_ids(weeks: Sequence[Week]) -> set[int]:
    return {week.report_id for week in weeks}


def articles_of(
    client_id: int,
    weeks: Sequence[Week],
    *,
    path: str | Path | None = None,
) -> tuple[Article, ...]:
    """Разрез по артикулам за те же недели. Считается по `fin_rows`."""
    wanted = _report_ids(weeks)
    buckets: dict[int | None, dict[str, Any]] = {}
    for report_id in sorted(wanted):
        for row in db.repo(client_id, path).rows("fin_rows", report_id=report_id):
            nm_id = int(row["nm_id"]) if row["nm_id"] is not None else None
            bucket = buckets.setdefault(
                nm_id,
                {
                    "vendor_code": row["sa_name"] or "",
                    "subject": row["subject_name"] or "",
                    "quantity": 0,
                    "amounts": Amounts(),
                    # Словарь, а не множество: порядок появления сохраняется,
                    # повторы гасятся.
                    "reasons": {},
                },
            )
            amount = db.from_kop(row["retail_amount_kop"])
            kind = doc_kind(row["doc_type_name"], amount)
            quantity = int(row["quantity"] or 0)
            # «Штук» в книге это проданное плюс возвращённое. Служебная строка
            # артикула тоже касается (логистика и хранение приходят по
            # товарам), но штук в ней нет. Компенсация тоже не даёт: товар по
            # ней выбыл, а не продался.
            if kind in (SALE, RETURN):
                bucket["quantity"] += quantity
            if kind == COMPENSATION:
                bucket["reasons"][str(row["supplier_oper_name"] or "").strip()] = None
            bucket["amounts"] = bucket["amounts"] + Amounts(
                revenue=amount if kind == SALE else ZERO,
                returns_amount=amount if kind == RETURN else ZERO,
                for_pay=db.from_kop(row["ppvz_for_pay_kop"]),
                commission=vw_of(row),
                acquiring=db.from_kop(row["acquiring_fee_kop"]),
                logistics=db.from_kop(row["delivery_kop"]),
                storage=db.from_kop(row["storage_fee_kop"]),
                acceptance=db.from_kop(row["acceptance_kop"]),
                penalties=db.from_kop(row["penalty_kop"]),
                deductions=db.from_kop(row["deduction_kop"]),
                additional_payment=db.from_kop(row["additional_payment_kop"]),
                compensation=(
                    db.from_kop(row["ppvz_for_pay_kop"]) if kind == COMPENSATION else ZERO
                ),
                compensation_units=quantity if kind == COMPENSATION else 0,
                sales_count=quantity if kind == SALE else 0,
                returns_count=quantity if kind == RETURN else 0,
            )
    articles = [
        Article(
            nm_id=nm_id,
            vendor_code=bucket["vendor_code"],
            subject=bucket["subject"],
            quantity=bucket["quantity"],
            amounts=bucket["amounts"],
            compensation_reasons=tuple(name for name in bucket["reasons"] if name),
        )
        for nm_id, bucket in buckets.items()
    ]
    articles.sort(key=lambda item: item.amounts.revenue, reverse=True)
    return tuple(articles)


def months_of(weeks: Sequence[Week]) -> tuple[Month, ...]:
    """Свод недель в месяц. Неделя относится к месяцу своего начала."""
    buckets: dict[str, Month] = {}
    for week in weeks:
        key = str(week.date_from)[:7]
        found = buckets.get(key) or Month(key=key)
        buckets[key] = replace(
            found, amounts=found.amounts + week.amounts, weeks=found.weeks + 1
        )
    return tuple(buckets[key] for key in sorted(buckets))


def _percents(
    client_id: int, weeks: Sequence[Week], *, path: str | Path | None = None
) -> tuple[Week, ...]:
    """Готовые проценты WB по строкам недели, средневзвешенные по сумме."""
    result = []
    for week in weeks:
        pairs: dict[str, list[tuple[Decimal, Decimal]]] = {}
        for row in db.repo(client_id, path).rows("fin_rows", report_id=week.report_id):
            base = db.from_kop(row["retail_price_withdisc_kop"]) or db.from_kop(
                row["retail_amount_kop"]
            )
            for name, column in (
                ("commission_percent", "commission_percent"),
                ("acquiring_percent", "acquiring_percent"),
                ("spp", "ppvz_spp_prc"),
                ("kvw", "ppvz_kvw_prc"),
            ):
                value = row[column]
                if value is None:
                    continue
                pairs.setdefault(name, []).append((money(value), base))
        result.append(
            replace(week, **{name: _weighted(items) for name, items in pairs.items()})
        )
    return tuple(result)


def build(
    client_id: int,
    period: str = "week",
    *,
    today: date | None = None,
    path: str | Path | None = None,
) -> FinanceReport:
    """Отчёт за период по тому, что уже лежит в базе. В WB отсюда не ходят."""
    date_from, date_to = period_bounds(period, today)
    weeks = _percents(client_id, weeks_of(client_id, date_from, date_to, path=path), path=path)
    return FinanceReport(
        client_id=client_id,
        period=period,
        date_from=date_from,
        date_to=date_to,
        weeks=weeks,
        months=months_of(weeks),
        articles=articles_of(client_id, weeks, path=path),
    )


# --- книга Excel -------------------------------------------------------------


def excel_bytes(report: FinanceReport) -> bytes:
    """Четыре листа одним вызовом. Работу с openpyxl делает `core.xlsx`."""
    from core import excel

    return excel.finance_book(report)


def file_name(report: FinanceReport) -> str:
    return f"finance-{report.date_from.isoformat()}-{report.date_to.isoformat()}.xlsx"


# --- очередь -----------------------------------------------------------------

_sender: Callable[[int, FinanceReport, bytes], Any] | None = None


def set_sender(fn: Callable[[int, FinanceReport, bytes], Any] | None) -> None:
    """Чем отдаётся готовый отчёт: `fn(client_id, report, xlsx)`."""
    global _sender
    _sender = fn


def _period_of(task: Any) -> str:
    return str((task.payload or {}).get("period") or "week")


def request_report(
    client_id: int, period: str = "week", *, path: str | Path | None = None
) -> queue.TaskId:
    """Ставит выгрузку в очередь. «Принято» клиенту говорит сама очередь."""
    if period not in PERIODS:
        raise ValueError(f"неизвестный период: {period}")
    return queue.enqueue(client_id, TASK_KIND, {"period": period}, path=path)


def request_collect(
    client_id: int, period: str = "week", *, path: str | Path | None = None
) -> queue.TaskId:
    """Ставит в очередь сбор без отправки: расписание, а не просьба клиента.

    `notify=False` здесь не мелочь: клиент этой задачи не просил, и «принято,
    пришлю» на неё было бы сообщением ни о чём.
    """
    if period not in PERIODS:
        raise ValueError(f"неизвестный период: {period}")
    return queue.enqueue(
        client_id, COLLECT_KIND, {"period": period}, notify=False, path=path
    )


async def collect_task(task: Any, *, path: str | Path | None = None) -> Collected:
    """Собрать период в базу и замолчать. Ни сообщения, ни Excel.

    Ради этого вида задача и разведена надвое. Пока `fin_weeks` наполняла
    только команда `/finance`, новых недель у молчащего клиента не появлялось,
    а сторож скрытых расходов (агент 2) срабатывает ровно на появление нового
    `report_id`. Требование R43 говорит «еженедельные после появления нового
    финотчёта WB», и без сбора по расписанию оно не выполнялось вовсе.
    """
    period = _period_of(task)
    date_from, date_to = period_bounds(period)
    return await collect(int(task.client_id), date_from, date_to, path=path)


async def deliver(
    client_id: int, period: str = "week", *, path: str | Path | None = None
) -> FinanceReport:
    """Отдать отчёт по тому, что уже собрано. В WB отсюда не ходят ни разу.

    Вторая половина пары. Повторная отправка не должна тянуть повторную
    выгрузку: она самая дорогая в проекте, один запрос в минуту.
    """
    report = build(client_id, period, path=path)
    if _sender is None:
        raise RuntimeError("некому отправить финансовый отчёт: доставка не подключена")
    result = _sender(client_id, report, excel_bytes(report))
    if hasattr(result, "__await__"):
        await result
    return report


async def report_task(task: Any, *, path: str | Path | None = None) -> FinanceReport:
    """Обработчик задачи клиента: собрать период и сразу отдать собранное."""
    client_id = int(task.client_id)
    period = _period_of(task)
    await collect_task(task, path=path)
    return await deliver(client_id, period, path=path)


def register_jobs() -> None:
    """Связывает виды задач с обработчиками. Зовёт сборка бота, не импорт."""
    queue.register(TASK_KIND, report_task, title="деньги за неделю")
    # Выгрузка отчёта о реализации идёт сама по себе, её никто не просил.
    queue.register(COLLECT_KIND, collect_task, quiet=True)
