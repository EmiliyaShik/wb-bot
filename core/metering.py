"""Учёт вызовов и денег: что бот потратил и что заработал.

Этим пользуется только владелец. Здесь собирается картина целиком: активные
клиенты по модулям, выручка месяца по оплаченным счетам, расходы (вызовы WB
по методам и деньги за нейросеть), маржа по каждому клиенту и открытые счета.

Два правила, по которым здесь всё и устроено.

Первое: общего среза по клиентским таблицам нет. Сводка собирается обходом
`all_clients()` плюс `repo(client_id)` - тем же способом, каким счёт ищут по
номеру. Правило изоляции у сводки владельца исключений не получает.

Второе: клиент здесь это внутренний id и ничего больше. Ни Telegram-аккаунта,
ни имени, ни реквизитов в сводку не попадает. Реестр для актов, где ИНН и
название организации нужны бухгалтерии по закону, живёт не здесь, а в
`core.billing`: он собирается из счетов, а не из учёта вызовов.

Деньги целыми копейками в базе и `Decimal` в расчётах.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from core import access, billing, db, scheduler

STAMP = "%Y-%m-%d %H:%M:%S"

# Сколько строк учёта читать за раз. Месяц работы бота это тысячи вызовов,
# а не сотни, и срез по умолчанию (500) занизил бы расходы молча.
MAX_ROWS = 100000

PERIODS = ("month",)


@dataclass(frozen=True)
class MethodUse:
    """Сколько раз бот сходил в один метод WB и сколько раз получил ошибку."""

    method: str
    count: int
    errors: int = 0


@dataclass(frozen=True)
class ClientMoney:
    """Деньги по одному клиенту за период. Клиент здесь только внутренний id."""

    client_id: int
    revenue: Decimal = Decimal(0)
    cost: Decimal = Decimal(0)
    calls: int = 0
    modules: tuple[str, ...] = ()

    @property
    def margin(self) -> Decimal:
        return self.revenue - self.cost


@dataclass(frozen=True)
class Stats:
    """Бизнес одним взглядом за период."""

    period: str
    title: str
    start: datetime
    end: datetime
    modules: dict[str, int] = field(default_factory=dict)
    active_clients: int = 0
    total_clients: int = 0
    revenue: Decimal = Decimal(0)
    unpaid_count: int = 0
    unpaid_amount: Decimal = Decimal(0)
    calls: tuple[MethodUse, ...] = ()
    ai_cost: Decimal = Decimal(0)
    clients: tuple[ClientMoney, ...] = ()

    @property
    def calls_total(self) -> int:
        return sum(use.count for use in self.calls)

    @property
    def errors_total(self) -> int:
        return sum(use.errors for use in self.calls)

    @property
    def margin(self) -> Decimal:
        return self.revenue - self.ai_cost


# --- запись ---


def record_call(
    client_id: int | None,
    host: str,
    method: str,
    *,
    status: int | None = None,
    duration_ms: int | None = None,
    path: str | Path | None = None,
) -> None:
    """Вызов WB в учёт. За клиента - через его repo, служебный - через общий слой."""
    if client_id is None:
        db.admin_repo(path).add_api_call(
            host=str(host), method=str(method), status=status, duration_ms=duration_ms
        )
        return
    db.repo(int(client_id), path).insert(
        "api_calls",
        host=str(host),
        method=str(method),
        status=status,
        duration_ms=duration_ms,
    )


def record_ai(
    client_id: int | None,
    *,
    provider: str = "",
    model: str = "",
    kind: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cost_kop: int = 0,
    path: str | Path | None = None,
) -> None:
    """Расход нейросети в учёт. Таблица пустует, пока моделей в боте нет."""
    values = dict(
        provider=str(provider),
        model=str(model),
        kind=str(kind),
        prompt_tokens=int(prompt_tokens),
        completion_tokens=int(completion_tokens),
        cost_kop=int(cost_kop),
    )
    if client_id is None:
        db.admin_repo(path).add_ai_call(**values)
        return
    db.repo(int(client_id), path).insert("ai_calls", **values)


# --- границы периода ---

# Месяц считает core.billing: у реестра актов и у сводки владельца отрезок
# должен быть один и тот же, а второго календаря в проекте быть не должно.


def _moment(today: date | None) -> datetime:
    """Момент, на который считается состояние доступа: полдень нужного дня."""
    if today is None:
        return datetime.now(timezone.utc)
    local = datetime(today.year, today.month, today.day, 12, tzinfo=scheduler.tz())
    return local.astimezone(timezone.utc)


def _stamp(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime(STAMP)


def _bounds(period: str, today: date | None) -> tuple[datetime, datetime]:
    if period not in PERIODS:
        raise ValueError(f"неизвестный период: {period}")
    return billing.month_bounds(today=today)


# --- сводка ---


def _rows_in_period(rows: list[Any], start: datetime, end: datetime) -> list[Any]:
    top = _stamp(end)
    return [row for row in rows if str(row["at"] or "") < top]


def stats(
    period: str = "month",
    *,
    today: date | None = None,
    path: str | Path | None = None,
) -> Stats:
    """Вся картина за период: клиенты, выручка, расходы, маржа, открытые счета."""
    start, end = _bounds(period, today)
    moment = _moment(today)
    admin = db.admin_repo(path)

    by_module: dict[str, int] = {}
    revenue_total = Decimal(0)
    unpaid_count = 0
    unpaid_amount = Decimal(0)
    active_clients = 0
    money: dict[int, dict[str, Any]] = {}
    total_clients = 0

    for row in admin.all_clients():
        client_id = int(row["id"])
        total_clients += 1
        working = tuple(
            item.module
            for item in access.status(client_id, now=moment, path=path)
            if item.works
        )
        for name in working:
            by_module[name] = by_module.get(name, 0) + 1
        if working:
            active_clients += 1

        revenue = Decimal(0)
        for item in billing.invoices_of(client_id, path=path):
            if item.status == billing.PAID and billing.in_period(item.paid_at, start, end):
                revenue += item.amount
            elif item.is_open:
                unpaid_count += 1
                unpaid_amount += item.amount
        revenue_total += revenue

        if revenue or working:
            money[client_id] = {
                "revenue": revenue,
                "cost": Decimal(0),
                "calls": 0,
                "modules": working,
            }

    # Вызовы WB и деньги за нейросеть: один проход по учёту, без запроса на
    # каждого клиента. Это учёт бота, а не данные клиента: в api_calls и
    # ai_calls нет ничего, кроме метода, кода ответа и стоимости.
    used: dict[str, list[int]] = {}
    for call in _rows_in_period(admin.api_calls(since=_stamp(start), limit=MAX_ROWS), start, end):
        method = str(call["method"] or "")
        stat = used.setdefault(method, [0, 0])
        stat[0] += 1
        status = call["status"]
        if status is None or int(status) >= 400:
            stat[1] += 1
        owner = call["client_id"]
        if owner is not None and int(owner) in money:
            money[int(owner)]["calls"] += 1

    ai_cost = Decimal(0)
    for call in _rows_in_period(admin.ai_calls(since=_stamp(start), limit=MAX_ROWS), start, end):
        cost = db.from_kop(call["cost_kop"])
        ai_cost += cost
        owner = call["client_id"]
        if owner is not None and int(owner) in money:
            money[int(owner)]["cost"] += cost

    calls = tuple(
        sorted(
            (MethodUse(method, count, errors) for method, (count, errors) in used.items()),
            key=lambda use: (-use.count, use.method),
        )
    )
    clients = tuple(
        sorted(
            (
                ClientMoney(
                    client_id=client_id,
                    revenue=values["revenue"],
                    cost=values["cost"],
                    calls=values["calls"],
                    modules=values["modules"],
                )
                for client_id, values in money.items()
            ),
            key=lambda item: (-item.margin, item.client_id),
        )
    )
    return Stats(
        period=period,
        title=billing.month_title(start),
        start=start,
        end=end,
        modules=by_module,
        active_clients=active_clients,
        total_clients=total_clients,
        revenue=revenue_total,
        unpaid_count=unpaid_count,
        unpaid_amount=unpaid_amount,
        calls=calls,
        ai_cost=ai_cost,
        clients=clients,
    )
