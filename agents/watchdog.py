"""Агент 2, сторож скрытых расходов: что поехало и во сколько это обошлось.

Считает пять показателей недели и сравнивает их со средним за предыдущие
недели. Порог пройден - клиент узнаёт не про пункты, а про рубли: «эквайринг
вырос с 1,4% до 2,1%, при выручке 840 000 ₽ это 5 880 ₽ за неделю».

Три решения, которые видно прямо здесь.

**В Wildberries отсюда не ходят вообще, ни одного запроса.** Всё уже лежит в
`fin_weeks` и `fin_rows` после агента 1, читается через `agents.finance`. Это
требование ТЗ дословно, и оно проверяется тестом: подставной транспорт WB не
получает ни одного вызова.

**Фактическая комиссия считается из денег, а не из тарифа.** У строки WB есть
готовое поле `commissionPercent` - это тариф категории. Реально удержали
`vw`, и процент берётся как удержанные рубли к выручке. Тариф и удержание
расходятся ровно тогда, когда селлеру важно об этом узнать. Так же считаются
эквайринг, доля логистики и доля хранения.

Отсюда следует расхождение, и оно осознанное: в `/finance` агент 1 показывает
проценты, которые отдал сам Wildberries, здесь - удержанную долю. Одна и та
же неделя даёт две цифры, поэтому текст в `bot/handlers/dynamics.py` называет
разницу прямо, а не оставляет селлера гадать. Прятать её, взяв готовое поле,
значило бы выбросить единственный признак утечки: удержали не по тарифу.

**СПП - исключение, и осознанное.** Это не доля выручки, а процент скидки
Wildberries, посчитать его из денег нельзя. Берётся готовое поле WB,
средневзвешенное по неделе, которое уже собрал агент 1.

Пороги живут в конфиге, секция `[alerts]`. В коде их нет ни одного: подмена
конфига меняет поведение, и это тоже проверяется тестом.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Sequence

from agents import finance
from core import access, config, db, scheduler

logger = logging.getLogger(__name__)

__all__ = [
    "MODULE",
    "TASK_KIND",
    "METRICS",
    "TITLES",
    "WeekMetrics",
    "Alert",
    "Watch",
    "Dynamics",
    "thresholds",
    "baseline_weeks",
    "share",
    "metrics_of",
    "compare",
    "history",
    "check",
    "dynamics",
    "latest_report",
    "set_delivery",
    "alerts_task",
    "register_jobs",
]

# Сторож живёт внутри модуля «Финансы»: отдельной подписки на него нет.
MODULE = finance.MODULE

# Вид недельной работы. Ставится расписанием, когда у клиента появился новый
# финансовый отчёт, а не по дню недели.
TASK_KIND = "watchdog_alerts"

# Имена показателей совпадают с ключами секции `[alerts]` конфига. Совпадение
# не случайное: порог ищется по имени показателя, и лишнего справочника
# «показатель -> ключ конфига» в проекте нет.
METRICS: tuple[str, ...] = (
    "commission",
    "acquiring",
    "spp",
    "logistics_share",
    "storage_share",
)

TITLES: dict[str, str] = {
    "commission": "комиссия площадки",
    "acquiring": "эквайринг",
    "spp": "СПП",
    "logistics_share": "доля логистики в выручке",
    "storage_share": "доля хранения в выручке",
}

ZERO = Decimal("0")
HUNDRED = Decimal("100")


# --- конфиг ------------------------------------------------------------------


def _alerts_conf() -> dict[str, Any]:
    try:
        return dict(config.settings().get("alerts", {}))
    except Exception:  # noqa: BLE001 - сторож не роняет бота из-за конфига
        logger.exception("секцию [alerts] не прочитать")
        return {}


def thresholds() -> dict[str, Decimal]:
    """Пороги в пунктах, из секции `[alerts]`.

    Своих значений здесь нет намеренно. Ключа в конфиге нет - показатель не
    проверяется: молчание честнее, чем порог, о котором владелец не знает.
    """
    found: dict[str, Decimal] = {}
    for metric in METRICS:
        raw = _alerts_conf().get(metric)
        if raw is None:
            continue
        try:
            found[metric] = Decimal(str(raw))
        except (ArithmeticError, ValueError):
            logger.warning("порог %s в конфиге не разобрать: %r", metric, raw)
    return found


def baseline_weeks() -> int:
    """Сколько предыдущих недель усредняется. Из конфига, ключ baseline_weeks."""
    raw = _alerts_conf().get("baseline_weeks")
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        # Не порог, а размер окна сравнения: без него сравнивать не с чем.
        logger.warning("baseline_weeks в конфиге не разобрать: %r, беру 4", raw)
        return 4


# --- показатели --------------------------------------------------------------


def share(part: Decimal, whole: Decimal) -> Decimal | None:
    """Доля в процентах. Нулевая выручка это «показателя нет», а не ноль."""
    if whole is None or whole <= ZERO:
        return None
    return (Decimal(part) / Decimal(whole)) * HUNDRED


@dataclass(frozen=True)
class WeekMetrics:
    """Пять показателей одной недели плюс выручка, из которой они считаны."""

    report_id: int
    date_from: str
    date_to: str
    revenue: Decimal = ZERO
    commission: Decimal | None = None
    acquiring: Decimal | None = None
    spp: Decimal | None = None
    logistics_share: Decimal | None = None
    storage_share: Decimal | None = None
    # Выгрузка недели не обрывалась на потолке страниц.
    complete: bool = True
    # Неделя сверена с агрегатом Wildberries и сошлась.
    trustworthy: bool = False

    def value(self, metric: str) -> Decimal | None:
        return getattr(self, metric, None)


def metrics_of(week: finance.Week) -> WeekMetrics:
    """Неделя агента 1 в пять показателей. Комиссия - удержанная, не тарифная."""
    amounts = week.amounts
    revenue = amounts.revenue
    return WeekMetrics(
        report_id=week.report_id,
        date_from=week.date_from,
        date_to=week.date_to,
        revenue=revenue,
        commission=share(amounts.commission, revenue),
        acquiring=share(amounts.acquiring, revenue),
        spp=week.spp,
        logistics_share=share(amounts.logistics, revenue),
        storage_share=share(amounts.storage, revenue),
        complete=week.complete,
        trustworthy=week.trustworthy,
    )


def average(weeks: Sequence[WeekMetrics], metric: str) -> Decimal | None:
    """Среднее показателя по неделям. Недели без показателя не занижают его."""
    values = [week.value(metric) for week in weeks]
    known = [value for value in values if value is not None]
    if not known:
        return None
    return sum(known, ZERO) / Decimal(len(known))


# --- алерты ------------------------------------------------------------------


@dataclass(frozen=True)
class Alert:
    """Один выросший расход: с чего на что, на сколько и почём."""

    metric: str
    title: str
    was: Decimal
    now: Decimal
    delta: Decimal
    threshold: Decimal
    revenue: Decimal
    rubles: Decimal

    @property
    def grew(self) -> bool:
        return self.delta > ZERO


def compare(current: WeekMetrics, baseline: Sequence[WeekMetrics]) -> tuple[Alert, ...]:
    """Показатели недели против среднего за предыдущие. Чистая функция.

    Знак порога решает, в какую сторону смотреть: положительный порог ловит
    рост расхода, отрицательный - падение СПП. Отдельного списка «этот
    показатель падает» в коде нет, иначе конфиг перестал бы быть главным.
    """
    found: list[Alert] = []
    limits = thresholds()
    for metric in METRICS:
        threshold = limits.get(metric)
        now = current.value(metric)
        was = average(baseline, metric)
        if threshold is None or now is None or was is None:
            continue
        delta = now - was
        crossed = delta >= threshold if threshold > ZERO else delta <= threshold
        if not crossed:
            continue
        found.append(
            Alert(
                metric=metric,
                title=TITLES.get(metric, metric),
                was=was,
                now=now,
                delta=delta,
                threshold=threshold,
                revenue=current.revenue,
                # Рубли, а не пункты: пункт превращается в деньги той самой
                # выручкой, на которой он и случился.
                rubles=abs(delta) / HUNDRED * current.revenue,
            )
        )
    return tuple(found)


# --- чтение истории ----------------------------------------------------------


def _period_for(weeks_needed: int) -> str:
    """Самый короткий период агента 1, в который влезет столько недель."""
    days = weeks_needed * 7 + 7
    for name in ("month", "quarter", "year"):
        if finance.PERIODS[name] >= days:
            return name
    return "year"


def history(
    client_id: int,
    period: str | None = None,
    *,
    today: date | None = None,
    path: str | Path | None = None,
) -> tuple[WeekMetrics, ...]:
    """Показатели по неделям из базы, от старой к новой.

    Читается через `agents.finance.build`: та же выгрузка, тот же расчёт
    процентов, никакого второго чтения `fin_weeks` в проекте.
    """
    name = period or _period_for(baseline_weeks() + 1)
    report = finance.build(client_id, name, today=today, path=path)
    return tuple(metrics_of(week) for week in report.weeks)


@dataclass(frozen=True)
class Watch:
    """Итог осмотра недели. Пусто тоже результат, и он объясним."""

    client_id: int
    week: WeekMetrics | None = None
    baseline: tuple[WeekMetrics, ...] = ()
    alerts: tuple[Alert, ...] = ()
    have: int = 0
    needed: int = 0
    # Неделя обрезана или не сошлась со сверкой: цифру показываем с оговоркой.
    doubtful: bool = False

    @property
    def enough(self) -> bool:
        """Недель хватило на сравнение."""
        return self.week is not None and len(self.baseline) >= self.needed - 1

    @property
    def missing(self) -> int:
        return max(0, self.needed - self.have)

    def __iter__(self):
        # Граница модуля в спецификации обещает список алертов. Обещание
        # держится: list(check(...)) отдаёт ровно его, а поля рядом
        # объясняют, почему список пуст.
        return iter(self.alerts)

    def __len__(self) -> int:
        return len(self.alerts)


def check(
    client_id: int,
    *,
    today: date | None = None,
    path: str | Path | None = None,
) -> Watch:
    """Осмотр последней недели против среднего за предыдущие.

    В WB не ходит: всё берётся из базы. Недель не хватило - алертов нет, и
    `Watch` честно говорит, сколько ещё нужно.
    """
    window = baseline_weeks()
    needed = window + 1
    # Обрезанная выгрузка в сравнение не идёт ни как текущая неделя, ни как
    # база: у неё занижена выручка, и алерт показал бы падение, которого не
    # было.
    weeks = tuple(item for item in history(client_id, today=today, path=path) if item.complete)
    if len(weeks) < needed:
        return Watch(client_id=client_id, have=len(weeks), needed=needed)

    current = weeks[-1]
    baseline = weeks[-needed:-1]
    return Watch(
        client_id=client_id,
        week=current,
        baseline=baseline,
        alerts=compare(current, baseline),
        have=len(weeks),
        needed=needed,
        doubtful=not current.trustworthy,
    )


@dataclass(frozen=True)
class Dynamics:
    """Таблица показателей по неделям за выбранный период."""

    client_id: int
    period: str
    weeks: tuple[WeekMetrics, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.weeks

    @property
    def title(self) -> str:
        return finance.PERIOD_TITLES.get(self.period, self.period)


def dynamics(
    client_id: int,
    period: str = "month",
    *,
    today: date | None = None,
    path: str | Path | None = None,
) -> Dynamics:
    """`/dynamics`: показатели по неделям за период. Тоже только из базы."""
    if period not in finance.PERIODS:
        raise ValueError(f"неизвестный период: {period}")
    return Dynamics(
        client_id=client_id,
        period=period,
        weeks=history(client_id, period, today=today, path=path),
    )


# --- еженедельная рассылка ---------------------------------------------------

_delivery: Callable[[int, Watch], Any] | None = None


def set_delivery(fn: Callable[[int, Watch], Any] | None) -> None:
    """Чем уходит недельный осмотр: `fn(client_id, watch)`. Текст не здесь."""
    global _delivery
    _delivery = fn


def latest_report(client_id: int, *, path: str | Path | None = None) -> int | None:
    """Номер последнего финотчёта клиента. Признак новой недели, без WB.

    Расписание спрашивает об этом каждые несколько часов. Отчёт появляется в
    `fin_weeks` после работы агента 1, и именно это событие запускает осмотр.
    """
    rows = db.repo(client_id, path).rows("fin_weeks", order_by="report_id")
    if not rows:
        return None
    return int(rows[-1]["report_id"])


async def alerts_task(task: Any, *, path: str | Path | None = None) -> Watch | None:
    """Недельная работа: осмотреть последнюю неделю и отдать результат.

    Без доступа к модулю рассылка молчит: это платная часть «Финансов», а не
    служебное уведомление.
    """
    client_id = int(task.client_id)
    if not access.has_access(client_id, MODULE, path=path):
        return None
    watch = check(client_id, today=datetime.now(scheduler.tz()).date(), path=path)
    if _delivery is None:
        raise RuntimeError("некому отправить недельный осмотр: доставка не подключена")
    result = _delivery(client_id, watch)
    if hasattr(result, "__await__"):
        await result
    return watch


def register_jobs(*, path: str | Path | None = None) -> None:
    """Связывает недельную работу с расписанием. Зовёт сборка бота, не импорт.

    Пробу нового отчёта здесь больше не ставим: она одна на весь планировщик,
    и её владелец один, agents.lifecycle. Знание о последнем отчёте остаётся
    тут, в latest_report, и проба зовёт именно его.
    """
    scheduler.register_weekly(TASK_KIND, alerts_task)
