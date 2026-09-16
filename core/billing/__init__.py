"""Счета: выставление, статусы, оплата.

Что тут есть и чего тут нет.

Есть: номер счёта, сумма из конфига, срок в банковских днях, четыре статуса
и одна кнопка «оплачен», которая зовёт `core.access.grant_access`.

Нет: ни одного реквизита, ни одной формулировки и ни одной цены. Реквизиты
ИП живут в переменных окружения, наименование услуги и строка про НДС - в
конфиге, цены - в `core.config`. Пока реквизиты пусты, счёт не формируется:
правдоподобный, но выдуманный ИНН в PDF хуже пустого места, потому что
пустое место исправят, а выдуманное отгрузят клиенту.

Доступ этот модуль не включает. Он готовит `payment_ref` (номер счёта) и
отдаёт его в `grant_access`, у которой и живёт правило «повторный платёж
ничего не продлевает».
"""

from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from core import access, audit, config, db
from core.billing import bankdays
from core.billing.acts import (
    ACTS_HEADERS,
    ACTS_SHEET,
    BadPeriod,
    acts_book,
    acts_file_name,
    acts_rows,
    in_period,
    month_bounds,
    month_key,
    month_title,
    previous_month_bounds,
)
from core.billing.counterparty import Counterparty, inn_is_valid, lookup, normalize

logger = logging.getLogger(__name__)

__all__ = [
    "Invoice",
    "Paid",
    "DetailsMissing",
    "ISSUED",
    "PAID",
    "OVERDUE",
    "CANCELLED",
    "STATUS_WORDS",
    "Counterparty",
    "inn_is_valid",
    "lookup",
    "lookup_inn",
    "normalize",
    "create_invoice",
    "mark_paid",
    "cancel",
    "invoice",
    "invoices_of",
    "paid_invoices",
    "acts_xlsx",
    "acts_book",
    "acts_rows",
    "acts_file_name",
    "ACTS_HEADERS",
    "ACTS_SHEET",
    "BadPeriod",
    "month_bounds",
    "previous_month_bounds",
    "month_title",
    "month_key",
    "in_period",
    "expire_overdue",
    "months_to_days",
    "months_words",
    "service_line",
    "vat_note",
    "vat_note_missing",
    "offer_url",
    "owner_contact",
    "seller_block",
    "missing_details",
]

# Статусы из ТЗ: выставлен, оплачен, просрочен, отменён.
ISSUED = "issued"
PAID = "paid"
OVERDUE = "overdue"
CANCELLED = "cancelled"

STATUS_WORDS = {
    ISSUED: "выставлен",
    PAID: "оплачен",
    OVERDUE: "просрочен",
    CANCELLED: "отменён",
}

_STAMP = "%Y-%m-%d %H:%M:%S"
_DAY = "%Y-%m-%d"

# Способ оплаты, который уходит в журнал доступа вместе с номером счёта.
METHOD = "invoice"


class DetailsMissing(Exception):
    """Реквизитов ИП нет, счёт собрать не из чего.

    Несёт список ровно тех переменных окружения, которых не хватает: владельцу
    надо показать, что именно заполнить, а не общее «настройте бота».
    """

    def __init__(self, missing: Iterable[str]) -> None:
        self.missing: tuple[str, ...] = tuple(missing)
        super().__init__("не заданы реквизиты: " + ", ".join(self.missing))


@dataclass(frozen=True)
class Invoice:
    """Один счёт. Деньги внутри в копейках, наружу отдаются рублями."""

    number: str
    client_id: int
    module: str
    period_months: int
    amount_kop: int
    status: str = ISSUED
    inn: str = ""
    org_name: str = ""
    org_address: str = ""
    issued_at: datetime | None = None
    due_at: date | None = None
    paid_at: datetime | None = None

    @property
    def amount(self) -> Decimal:
        """Сумма в рублях."""
        return db.from_kop(self.amount_kop)

    @property
    def status_word(self) -> str:
        return STATUS_WORDS.get(self.status, self.status)

    @property
    def is_open(self) -> bool:
        """Счёт ещё ждёт оплаты."""
        return self.status in (ISSUED, OVERDUE)

    @property
    def customer(self) -> str:
        """Кого писать в счёте: название, если нашлось, иначе ИНН."""
        return self.org_name or (f"ИНН {self.inn}" if self.inn else "")

    def payment_purpose(self) -> str:
        """Назначение платежа. Номер счёта тут обязателен: по нему ищут оплату.

        Про НДС тут нет ни слова, пока владелец не заполнил `invoice.vat_note`.
        Налоговый режим ИП это факт о продавце, а не догадка бота: назначение
        платежа уходит и клиенту, и в банк, и «Без НДС» от себя было бы
        утверждением о чужой системе налогообложения. Пустое место исправят,
        выдуманное отгрузят.
        """
        base = f"Оплата по счёту {self.number} от {local_day(self.issued_at)}."
        note = vat_note()
        return f"{base} {note}" if note else base


@dataclass(frozen=True)
class Paid:
    """Что получилось от нажатия «Оплачен»."""

    invoice: Invoice
    granted: access.Access | None = None
    duplicate: bool = False
    already_paid: bool = False


# --- время и календарь ---


def _now(now: datetime | None = None) -> datetime:
    return now or datetime.now(timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(_STAMP)


def _parse_stamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:19], _STAMP).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_day(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def local_day(moment: datetime | None) -> str:
    """Дата для человека. Пояс проекта один: core.scheduler.tz()."""
    if moment is None:
        return "-"
    from core import scheduler

    return moment.astimezone(scheduler.tz()).strftime("%d.%m.%Y")


def _add_months(start: date, months: int) -> date:
    """Та же дата через N месяцев, с поправкой на короткий месяц."""
    total = start.month - 1 + int(months)
    year = start.year + total // 12
    month = total % 12 + 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def months_to_days(months: int, start: date | None = None) -> int:
    """Сколько суток доступа в N месяцах, по календарю.

    Тридцать дней в месяце тут не годятся: за год они украли бы у клиента
    пять суток. `grant_access` принимает дни, поэтому месяцы переводятся
    честно, от даты начала.
    """
    begin = start or date.today()
    return (_add_months(begin, int(months)) - begin).days


def months_words(months: int) -> str:
    """Период словами: 1 месяц, 3 месяца, 12 месяцев."""
    value = int(months)
    tail = value % 100
    if 11 <= tail <= 14:
        return f"{value} месяцев"
    last = value % 10
    if last == 1:
        return f"{value} месяц"
    if last in (2, 3, 4):
        return f"{value} месяца"
    return f"{value} месяцев"


# --- конфиг и реквизиты. Своих строк тут нет ---


def _invoice_settings() -> dict[str, Any]:
    return config.settings().get("invoice", {}) or {}


def vat_note() -> str:
    """Строка про налоговый режим. По умолчанию пуста, и это не ошибка."""
    return str(_invoice_settings().get("vat_note", "") or "").strip()


def vat_note_missing() -> bool:
    """Нужно ли напомнить владельцу про налоговый режим."""
    return not vat_note()


def service_line(module: str, months: int) -> str:
    """Наименование услуги для строки счёта. Шаблон живёт в конфиге."""
    template = str(_invoice_settings().get("service_name", "") or "").strip()
    info = config.modules().get(module)
    title = (info.title if info and info.title else module) or module
    if not template:
        return f"Доступ к сервису WBРентген, модуль {title}, {months_words(months)}"
    return template.format(module=title, period=months_words(months))


def offer_url() -> str:
    """Ссылка на оферту. Пусто - значит оферта ещё готовится."""
    return config.env("OFFER_URL")


def owner_contact() -> str:
    """Контакт владельца для /paysupport и оплаты картой."""
    return config.env("OWNER_CONTACT")


def missing_details() -> tuple[str, ...]:
    """Каких переменных не хватает для счёта. Пусто - можно выставлять."""
    return config.missing_seller_details()


def seller_block() -> dict[str, str]:
    """Реквизиты ИП как есть. Ни одного значения в коде нет."""
    return config.seller_details()


# --- чтение ---


def _row_to_invoice(row: Any) -> Invoice:
    return Invoice(
        number=str(row["number"]),
        client_id=int(row["client_id"]),
        module=str(row["module"]),
        period_months=int(row["period_months"]),
        amount_kop=int(row["amount_kop"]),
        status=str(row["status"]),
        inn=str(row["inn"] or ""),
        org_name=str(row["org_name"] or ""),
        org_address=str(row["org_address"] or ""),
        issued_at=_parse_stamp(row["issued_at"]),
        due_at=_parse_day(row["due_at"]),
        paid_at=_parse_stamp(row["paid_at"]),
    )


def invoice(number: str, *, path: str | Path | None = None) -> Invoice | None:
    """Счёт по номеру. Путь владельца: он знает номер, но не знает клиента.

    Обход клиентов, а не запрос по всей таблице, выбран не случайно: счета это
    данные клиента, и `core.db` намеренно не выставляет наружу произвольный
    SQL, чтобы правило «к данным клиента только через repo(client_id)»
    держалось конструкцией, а не дисциплиной. Счетов у одного клиента единицы,
    цена обхода нулевая.
    """
    wanted = str(number).strip()
    if not wanted:
        return None
    for row in db.admin_repo(path).all_clients():
        found = db.repo(int(row["id"]), path).one("invoices", number=wanted)
        if found is not None:
            return _row_to_invoice(found)
    return None


def invoices_of(
    client_id: int, *, status: str | None = None, path: str | Path | None = None
) -> list[Invoice]:
    """Счета одного клиента, новые сверху."""
    where: dict[str, Any] = {"status": status} if status else {}
    rows = db.repo(client_id, path).rows("invoices", order_by="issued_at DESC", **where)
    return [_row_to_invoice(row) for row in rows]


def paid_invoices(
    start: datetime, end: datetime, *, path: str | Path | None = None
) -> list[Invoice]:
    """Оплаченные счета за период по всем клиентам, старые сверху.

    Обходом клиентов, а не сквозным запросом по таблице: счета это данные
    клиента, и правило изоляции у реестра владельца исключений не получает.
    Тот же приём, что у поиска счёта по номеру.
    """
    found: list[Invoice] = []
    for row in db.admin_repo(path).all_clients():
        for item in invoices_of(int(row["id"]), status=PAID, path=path):
            if in_period(item.paid_at, start, end):
                found.append(item)
    found.sort(key=lambda item: (item.paid_at or start, item.number))
    return found


def acts_xlsx(
    period: Any = None,
    *,
    today: date | None = None,
    path: str | Path | None = None,
) -> bytes:
    """Реестр оплаченных счетов за месяц одним вызовом: «ГГГГ-ММ» или текущий.

    Состав колонок из ТЗ: номер, дата, ИНН, название клиента, модуль, период,
    сумма. Реестр единственное место, где ИНН и название организации выходят
    наружу: бухгалтерии они нужны по закону, всё остальное у владельца видит
    клиента внутренним id.
    """
    start, end = month_bounds(period, today=today)
    return acts_book(paid_invoices(start, end, path=path), path=path)


# --- выставление ---


def create_invoice(
    client_id: int,
    module: str,
    months: int,
    *,
    inn: str = "",
    org_name: str = "",
    org_address: str = "",
    now: datetime | None = None,
    path: str | Path | None = None,
) -> Invoice:
    """Выставляет счёт и возвращает его.

    Порядок проверок выбран намеренно: сначала реквизиты, потом номер. Номер
    сквозной в пределах года, и тратить его на счёт, который всё равно не
    соберётся, нельзя - в реестре появилась бы дырка, которую потом никто не
    объяснит.

    Пустые SELLER_* поднимают DetailsMissing со списком ровно недостающих
    переменных. Это не авария, а честный ответ: данных нет, и придумывать их
    модуль не станет.
    """
    info = config.modules().get(module)
    if info is None:
        raise KeyError(f"нет такого модуля: {module}")
    if not info.visible:
        raise KeyError(f"модуль {module} не продаётся")
    months = int(months)
    if months not in config.periods():
        raise ValueError(f"нет такого периода: {months}")

    absent = missing_details()
    if absent:
        raise DetailsMissing(absent)

    moment = _now(now)
    issued_day = moment.astimezone(timezone.utc).date()
    amount_kop = db.to_kop(config.price_decimal(module, months))
    due = bankdays.due_date(issued_day)

    # Номер выдаёт слой данных, он же и сериализует параллельные вызовы.
    # Своего замка здесь нет намеренно: второй он ничего не чинит, а читателю
    # обещает проблему, которой нет.
    number = db.admin_repo(path).next_invoice_number(issued_day.year)
    db.repo(client_id, path).insert(
        "invoices",
        number=number,
        inn=normalize(inn),
        org_name=str(org_name or "").strip(),
        org_address=str(org_address or "").strip(),
        module=module,
        period_months=months,
        amount_kop=amount_kop,
        status=ISSUED,
        issued_at=_stamp(moment),
        due_at=due.strftime(_DAY),
    )
    audit.log(
        "invoice.created",
        client_id,
        f"счёт {number}: модуль {module}, {months_words(months)}, "
        f"{db.from_kop(amount_kop)} руб., оплатить до {due.strftime(_DAY)}",
        path=path,
    )
    return Invoice(
        number=number,
        client_id=int(client_id),
        module=module,
        period_months=months,
        amount_kop=amount_kop,
        status=ISSUED,
        inn=normalize(inn),
        org_name=str(org_name or "").strip(),
        org_address=str(org_address or "").strip(),
        issued_at=moment,
        due_at=due,
    )


# --- смена статуса ---


def _set_status(
    item: Invoice,
    status: str,
    *,
    paid_at: str | None = None,
    path: str | Path | None = None,
) -> None:
    values: dict[str, Any] = {"status": status}
    if paid_at is not None:
        values["paid_at"] = paid_at
    db.repo(item.client_id, path).update("invoices", {"number": item.number}, **values)


def mark_paid(
    number: str,
    *,
    actor: str = "owner",
    now: datetime | None = None,
    path: str | Path | None = None,
) -> Paid:
    """Кнопка владельца «Оплачен»: включает доступ по номеру счёта.

    Доступ включается только через `grant_access`, и `payment_ref` это номер
    счёта. Отсюда идемпотентность: второе нажатие приходит с тем же номером,
    дверь узнаёт дубль и ничего не продлевает. Своей проверки «уже оплачен»
    для этого не нужно, но статус счёта мы всё равно не переписываем дважды,
    чтобы дата оплаты осталась настоящей.
    """
    item = invoice(number, path=path)
    if item is None:
        raise KeyError(f"нет счёта {number}")

    moment = _now(now)
    granted = access.grant_access(
        item.client_id,
        item.module,
        months_to_days(item.period_months, moment.date()),
        item.number,
        method=METHOD,
        actor=actor,
        now=moment,
        path=path,
    )
    if granted.duplicate:
        audit.log(
            "invoice.duplicate",
            item.client_id,
            f"счёт {item.number} уже проводили, доступ не продлён",
            path=path,
        )
        return Paid(
            invoice=item,
            granted=granted,
            duplicate=True,
            already_paid=item.status == PAID,
        )

    _set_status(item, PAID, paid_at=_stamp(moment), path=path)
    audit.log(
        "invoice.paid",
        item.client_id,
        f"счёт {item.number} оплачен, модуль {item.module} включён",
        path=path,
    )
    return Paid(
        invoice=invoice(item.number, path=path) or item,
        granted=granted,
        duplicate=False,
    )


def cancel(
    number: str, *, path: str | Path | None = None, reason: str = ""
) -> Invoice | None:
    """Отменяет счёт. Оплаченный не отменяется: деньги уже пришли."""
    item = invoice(number, path=path)
    if item is None or item.status == PAID:
        return item
    _set_status(item, CANCELLED, path=path)
    audit.log(
        "invoice.cancelled",
        item.client_id,
        f"счёт {item.number} отменён" + (f": {reason}" if reason else ""),
        path=path,
    )
    return invoice(number, path=path)


def expire_overdue(
    *, now: datetime | None = None, path: str | Path | None = None
) -> list[Invoice]:
    """Переводит выставленные счета с истёкшим сроком в «просрочен».

    Возвращает ровно те счета, которые сменили статус: по этому списку
    рассылаются напоминания, и повторно они уже не придут.
    """
    today = _now(now).astimezone(timezone.utc).date()
    changed: list[Invoice] = []
    for client in db.admin_repo(path).all_clients():
        rows = db.repo(int(client["id"]), path).rows("invoices", status=ISSUED)
        overdue = [_row_to_invoice(row) for row in rows]
        overdue = [x for x in overdue if x.due_at is not None and x.due_at < today]
        for item in overdue:
            _set_status(item, OVERDUE, path=path)
            audit.log(
                "invoice.overdue",
                item.client_id,
                f"счёт {item.number} просрочен, срок был {item.due_at}",
                path=path,
            )
            changed.append(item)
    # Перечитываем: наружу должен уйти счёт с новым статусом, а не тот, что
    # лежал в базе до обновления.
    return [invoice(item.number, path=path) or item for item in changed]


async def lookup_inn(
    inn: str, *, http: Any = None, path: str | Path | None = None
) -> Counterparty | None:
    """Имя в спецификации. Тело живёт в counterparty.lookup."""
    return await lookup(inn, http=http, path=path)
