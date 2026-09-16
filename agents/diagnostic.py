"""Бесплатная диагностика: разбор прошлой недели и три утечки в рублях.

Это витрина всего сервиса. Человек подключил кабинет, ничего не заплатил и
решает, стоит ли. Поэтому здесь нет ни одной новой формулы: утечки считает
тот же сторож скрытых расходов, что и в платном модуле (`agents.watchdog`),
а недели берутся из базы после агента 1. Своего расчёта тут быть не должно -
иначе бесплатный разбор показывал бы одни цифры, а купленный модуль другие.

Три решения, которые видно прямо здесь.

**Диагностика привязана к кабинету WB, а не к Telegram-аккаунту.** Ключ это
`seller_id` (поле `sid` токена), и он же первичный ключ таблицы `diagnostics`.
Второй Telegram-аккаунт, подключивший тот же кабинет, получает отказ с
объяснением, а не второй бесплатный разбор. Отсюда же следует, что без
подключённого кабинета диагностики нет вовсе: `sid` неоткуда взять.

**Мало истории - не повод молчать и не повод выдумывать.** Утечка это
отклонение от среднего за предыдущие недели; если недель мало, показываются
самые крупные статьи расходов недели, и текст честно говорит, что это не
отклонение. Пустой ответ на витрине хуже честной оговорки.

**Результат сохраняется фактами, а не готовым сообщением.** В `summary`
ложится JSON: чей это разбор, какие утечки и на сколько рублей. Повторный
вызов тем же клиентом собирает текст заново из этих фактов и ничего не
считает по свежим данным: обещано «тот же результат», а не «новый».
"""

from __future__ import annotations

import json
import logging
import functools
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Sequence

from agents import finance, watchdog
from core import config, db, queue, scheduler

logger = logging.getLogger(__name__)

__all__ = [
    "TASK_KIND",
    "PERIOD",
    "TOP",
    "CATEGORY",
    "OK",
    "NOT_CONNECTED",
    "NO_CATEGORY",
    "TAKEN",
    "REPEAT",
    "NO_DATA",
    "GONE",
    "DONE_MARK",
    "SETTINGS_KEY",
    "COST_TITLES",
    "Leak",
    "ModuleLine",
    "Diagnostic",
    "seller_of",
    "has_category",
    "availability",
    "module_lines",
    "leaks_of",
    "biggest_costs",
    "previous",
    "mark_of",
    "done_at",
    "run",
    "request",
    "set_delivery",
    "diagnostic_task",
    "register_jobs",
]

# Вид фоновой задачи. Диагностика идёт через очередь, потому что сначала надо
# собрать неделю у Wildberries, а из хендлера в WB не ходят.
TASK_KIND = "diagnostic"

# Период сбора и сравнения. Месяц это пять недель: одна свежая и четыре в
# базу сравнения, ровно столько просит сторож.
PERIOD = "month"

# Сколько утечек показываем. Три - из ТЗ дословно.
TOP = 3

# Без этой категории токена финансовых данных нет, и считать нечего.
CATEGORY = "finance"

# Исходы. Отказ это тоже исход, и у каждого своя причина: текст объяснения
# собирает поверхность бота, а не агент.
OK = "ok"
NOT_CONNECTED = "not_connected"
NO_CATEGORY = "no_category"
TAKEN = "taken"
REPEAT = "repeat"
NO_DATA = "no_data"
# Разбор был, но данные, по которым он считался, клиент уже удалил.
GONE = "gone"

# Статьи расходов недели на случай, когда сравнивать ещё не с чем. Имена
# полей те же, что у `finance.Amounts`: второго справочника расходов в
# проекте нет.
COST_TITLES: dict[str, str] = {
    "commission": "комиссия площадки",
    "logistics": "логистика",
    "storage": "хранение",
    "acquiring": "эквайринг",
    "penalties": "штрафы",
    "deductions": "удержания",
    "acceptance": "приёмка",
}

ZERO = Decimal("0")


# --- что показываем ----------------------------------------------------------


@dataclass(frozen=True)
class Leak:
    """Одна утечка: во что обошлась и откуда взялась.

    `deviation` различает два разных утверждения. True - это отклонение от
    среднего за предыдущие недели, и рубли это цена отклонения. False - это
    просто крупная статья расходов, и никакого «выросло» в ней нет.
    """

    title: str
    rubles: Decimal
    metric: str = ""
    was: Decimal | None = None
    now: Decimal | None = None
    deviation: bool = True


@dataclass(frozen=True)
class ModuleLine:
    """Строка «что найдёт этот модуль». Текст берётся из конфига, не отсюда."""

    module: str
    title: str
    line: str


@dataclass(frozen=True)
class Diagnostic:
    """Итог диагностики. Отказ тоже итог, и он объясним."""

    client_id: int
    seller_id: str
    reason: str = OK
    # Какая именно неделя показана. Ноль значит «недели нет», а с ней и утечек.
    report_id: int = 0
    date_from: str = ""
    date_to: str = ""
    revenue: Decimal = ZERO
    leaks: tuple[Leak, ...] = ()
    # Утечки это отклонения от среднего, а не просто крупнейшие статьи.
    deviation: bool = True
    have: int = 0
    needed: int = 0
    lines: tuple[ModuleLine, ...] = ()
    # Прошлый результат, показанный заново. Пересчёта не было.
    repeat: bool = False
    done_at: str = ""

    @property
    def ok(self) -> bool:
        return self.reason in (OK, REPEAT)

    @property
    def enough(self) -> bool:
        """Недель хватило на сравнение со средним."""
        return self.needed > 0 and self.have >= self.needed


# --- кабинет -----------------------------------------------------------------


def seller_of(client_id: int, *, path: str | Path | None = None) -> str:
    """ID кабинета WB у клиента. Пусто значит «кабинет не подключён»."""
    row = db.admin_repo(path).client(client_id)
    return str((row["seller_id"] if row else "") or "").strip()


def has_category(client_id: int, *, path: str | Path | None = None) -> bool:
    """Есть ли у токена категория «Финансы». Читается из базы, не из WB."""
    row = db.repo(client_id, path).one("wb_tokens")
    if row is None:
        return False
    scopes = str(row["scopes"] or "")
    return CATEGORY in {item.strip() for item in scopes.split(",")}


def availability(client_id: int, *, path: str | Path | None = None) -> str:
    """Можно ли выдать диагностику этому клиенту и почему нет.

    Порядок проверок не случайный: сначала кабинет (без него нет `sid`),
    потом категория токена, и только потом «этот кабинет уже разбирали».
    """
    seller_id = seller_of(client_id, path=path)
    if not seller_id:
        return NOT_CONNECTED
    if not has_category(client_id, path=path):
        return NO_CATEGORY
    if db.admin_repo(path).diagnostic(seller_id) is None:
        return OK
    mine = mark_of(client_id, path=path).get("seller_id") == seller_id
    return REPEAT if mine else TAKEN


# --- утечки ------------------------------------------------------------------


def leaks_of(alerts: Sequence[watchdog.Alert]) -> tuple[Leak, ...]:
    """Самые дорогие отклонения недели. Рубли считает сторож, не мы."""
    ordered = sorted(alerts, key=lambda alert: alert.rubles, reverse=True)
    return tuple(
        Leak(
            title=alert.title,
            rubles=alert.rubles,
            metric=alert.metric,
            was=alert.was,
            now=alert.now,
            deviation=True,
        )
        for alert in ordered[:TOP]
    )


def biggest_costs(week: finance.Week) -> tuple[Leak, ...]:
    """Крупнейшие статьи расходов недели. Это не отклонение, и так и помечено.

    Нужно ровно тогда, когда сравнивать не с чем: недель мало или ничего не
    выросло. Молчать на витрине нельзя, а выдумывать отклонение тем более.
    """
    amounts = week.amounts
    found = [
        Leak(title=title, rubles=Decimal(getattr(amounts, name, ZERO)), metric=name, deviation=False)
        for name, title in COST_TITLES.items()
    ]
    positive = [leak for leak in found if leak.rubles > ZERO]
    positive.sort(key=lambda leak: leak.rubles, reverse=True)
    return tuple(positive[:TOP])


def module_lines() -> tuple[ModuleLine, ...]:
    """C3a: по строке на каждый видимый модуль. Тексты из конфига.

    Модуль без строки в конфиге пропускается: пустая строка в витрине хуже,
    чем её отсутствие.
    """
    out: list[ModuleLine] = []
    for name, info in config.visible_modules().items():
        line = str(info.diagnostic_line or "").strip()
        if line:
            out.append(ModuleLine(module=name, title=info.title or name, line=line))
    return tuple(out)


# --- хранение результата -----------------------------------------------------
#
# Здесь два разных факта, и лежат они нарочно в разных местах.
#
# «Этот кабинет уже разбирали» - факт про кабинет, он переживает и смену
# Telegram-аккаунта, и удаление клиента. Он лежит в `diagnostics` строкой без
# единой цифры: ни денег, ни процентов, ни `client_id`. Иначе финансовые цифры
# клиента остались бы в базе после `/disconnect` (R32) и после удаления по
# сроку хранения (R71): `diagnostics` ключуется по кабинету и в клиентские
# таблицы не входит, а значит не удаляется вместе с клиентом.
#
# «Разбор получил вот этот клиент, и вот по какой неделе» - факт про клиента,
# он лежит в его собственной строке (`clients.settings`) и удаляется вместе с
# ней. Отсюда же берётся повтор: неделя закреплена, а цифры собираются заново
# из `fin_weeks`. Данных больше нет - повтор честно об этом говорит, а не
# показывает сохранённую копию удалённых цифр.

# Что лежит в `diagnostics.summary`. Слово, а не данные: колонку из схемы
# убрать нельзя, но класть в неё нечего.
DONE_MARK = "выдан"

# Ключ в `clients.settings`. Рядом живут чужие ключи тасков 05 и 14: читаем
# весь словарь и пишем весь словарь.
SETTINGS_KEY = "diagnostic"


def _settings(client_id: int, path: str | Path | None) -> dict:
    """Весь словарь настроек клиента. Испорченный JSON это пустой словарь."""
    row = db.admin_repo(path).client(client_id)
    if row is None:
        return {}
    try:
        data = json.loads(row["settings"] or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def mark_of(client_id: int, *, path: str | Path | None = None) -> dict:
    """Отметка клиента о полученном разборе: кабинет и закреплённая неделя."""
    mark = _settings(client_id, path).get(SETTINGS_KEY)
    return dict(mark) if isinstance(mark, dict) else {}


def _remember(client_id: int, result: Diagnostic, *, path: str | Path | None) -> None:
    """Запоминает у клиента, что разбор выдан и по какой неделе он был."""
    data = _settings(client_id, path)
    data[SETTINGS_KEY] = {
        "seller_id": result.seller_id,
        "report_id": int(result.report_id),
        "date_to": result.date_to,
    }
    db.admin_repo(path).set_client_fields(
        client_id, settings=json.dumps(data, ensure_ascii=False)
    )


def done_at(seller_id: str, *, path: str | Path | None = None) -> str:
    """Когда этот кабинет разбирали. Пусто значит «не разбирали»."""
    row = db.admin_repo(path).diagnostic(str(seller_id))
    return "" if row is None else str(row["done_at"] or "")


def previous(client_id: int, *, path: str | Path | None = None) -> Diagnostic | None:
    """Прошлый разбор этого клиента, собранный по закреплённой неделе.

    Пересчёта «по-новому» тут нет: неделя та же самая, значит и цифры те же.
    Данных больше нет - возвращается None, и поверхность бота говорит правду.
    """
    mark = mark_of(client_id, path=path)
    pinned = str(mark.get("date_to") or "")
    seller_id = str(mark.get("seller_id") or "")
    if not pinned or not seller_id:
        return None
    try:
        moment = date.fromisoformat(pinned)
    except ValueError:
        return None

    result = _measure(client_id, seller_id, today=moment, path=path)
    if result.reason != OK or result.date_to != pinned:
        return None
    return replace(
        result, reason=REPEAT, repeat=True, done_at=done_at(seller_id, path=path)
    )


# --- разбор ------------------------------------------------------------------


def _measure(
    client_id: int,
    seller_id: str,
    *,
    today: date | None = None,
    path: str | Path | None = None,
) -> Diagnostic:
    """Счётная часть разбора: неделя, три утечки, строки модулей.

    Ничего не сохраняет и ничего не запрещает. В Wildberries отсюда не ходят:
    недели уже лежат в базе после агента 1.
    """
    watch = watchdog.check(client_id, today=today, path=path)
    report = finance.build(client_id, PERIOD, today=today, path=path)
    if not report.weeks:
        # Данных нет вовсе. Разбор не выдан, значит и не потрачен.
        return Diagnostic(
            client_id=client_id,
            seller_id=seller_id,
            reason=NO_DATA,
            have=watch.have,
            needed=watch.needed,
            lines=module_lines(),
        )

    if watch.enough and watch.alerts:
        # Неделя берётся у сторожа: обрезанные выгрузки он в сравнение не
        # берёт, и подписать его цифры чужой неделей значило бы соврать.
        shown, leaks, deviation = watch.week, leaks_of(watch.alerts), True
        revenue = shown.revenue
    else:
        week = report.weeks[-1]
        shown, leaks, deviation = week, biggest_costs(week), False
        revenue = week.amounts.revenue

    return Diagnostic(
        client_id=client_id,
        seller_id=seller_id,
        reason=OK,
        report_id=int(shown.report_id),
        date_from=shown.date_from,
        date_to=shown.date_to,
        revenue=revenue,
        leaks=leaks,
        deviation=deviation,
        have=watch.have,
        needed=watch.needed,
        lines=module_lines(),
    )


def run(
    seller_id: str,
    client_id: int,
    *,
    today: date | None = None,
    path: str | Path | None = None,
) -> Diagnostic:
    """Разбор прошлой недели для кабинета `seller_id`. Один раз на кабинет."""
    seller_id = str(seller_id)
    admin = db.admin_repo(path)
    if admin.diagnostic(seller_id) is not None:
        if mark_of(client_id, path=path).get("seller_id") == seller_id:
            # Тот же клиент: показываем прошлую неделю, а не считаем свежую.
            return previous(client_id, path=path) or Diagnostic(
                client_id=client_id,
                seller_id=seller_id,
                reason=GONE,
                done_at=done_at(seller_id, path=path),
                lines=module_lines(),
            )
        # Тот же кабинет с другого Telegram-аккаунта: разбор уже выдан.
        return Diagnostic(
            client_id=client_id,
            seller_id=seller_id,
            reason=TAKEN,
            done_at=done_at(seller_id, path=path),
            lines=module_lines(),
        )

    result = _measure(client_id, seller_id, today=today, path=path)
    if result.reason != OK:
        return result
    admin.mark_diagnostic(seller_id, DONE_MARK)
    _remember(client_id, result, path=path)
    return result


# --- фоновая задача ----------------------------------------------------------

_delivery: Callable[[int, Diagnostic], Any] | None = None


def set_delivery(fn: Callable[[int, Diagnostic], Any] | None) -> None:
    """Чем уходит готовый разбор: `fn(client_id, diagnostic)`. Текст не здесь."""
    global _delivery
    _delivery = fn


def request(client_id: int, *, path: str | Path | None = None) -> int:
    """Ставит разбор в очередь. «Принято, пришлю» говорит сама очередь.

    Через очередь, а не сразу: сначала надо выгрузить неделю у Wildberries,
    а повтор при недоступности WB живёт только здесь.
    """
    return queue.enqueue(client_id, TASK_KIND, {}, path=path)


async def diagnostic_task(
    task: Any,
    *,
    path: str | Path | None = None,
    http: Any = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], Any] | None = None,
    today: date | None = None,
) -> Diagnostic:
    """Собрать неделю у Wildberries и отдать разбор тому, кто пишет клиенту.

    Отказ тоже отдаётся: человек нажал команду и ждёт ответа, а молчание в
    витрине это худший из возможных ответов.
    """
    client_id = int(task.client_id)
    moment = today or datetime.now(scheduler.tz()).date()
    seller_id = seller_of(client_id, path=path)
    reason = availability(client_id, path=path)

    if reason == OK:
        date_from, date_to = finance.period_bounds(PERIOD, moment)
        await finance.collect(
            client_id,
            date_from,
            date_to,
            http=http,
            path=path,
            clock=clock,
            sleep=sleep,
        )
        result = run(seller_id, client_id, today=moment, path=path)
    elif reason == REPEAT:
        result = previous(client_id, path=path) or Diagnostic(
            client_id=client_id,
            seller_id=seller_id,
            reason=GONE,
            done_at=done_at(seller_id, path=path),
            lines=module_lines(),
        )
    else:
        result = Diagnostic(
            client_id=client_id, seller_id=seller_id, reason=reason, lines=module_lines()
        )

    if _delivery is None:
        raise RuntimeError("некому отправить диагностику: доставка не подключена")
    sent = _delivery(client_id, result)
    if hasattr(sent, "__await__"):
        await sent
    return result


def register_jobs(*, path: str | Path | None = None) -> None:
    """Связывает вид задачи с обработчиком. Зовёт сборка бота, не импорт."""
    queue.register(TASK_KIND, functools.partial(diagnostic_task, path=path))
