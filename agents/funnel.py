"""Агент 7. Воронка: путь покупателя по карточке и шаг, где он теряется.

**Этапов четыре у всех и пять у тех, кому Wildberries их даёт.**

Четыре универсальных: заход в карточку, корзина, заказ, выкуп. Все четыре
приходят готовыми числами и работают у любого селлера. Показов среди них нет,
и это не урезанная версия: слова «показы» нет ни в одном поле раздела
«Воронка продаж». Первое, что Wildberries знает о покупателе, это что карточку
уже открыли.

Пятый этап, видимость в поиске, появляется только у кабинетов с подпиской
Джем. Это **не количество показов**, а вероятность в процентах, что покупатель
увидит карточку; Wildberries считает её по средней позиции. Подменять ею
показы нельзя, поэтому она и названа своим именем, и меряется своей меркой:
у неё есть готовая динамика против прошлого периода в процентах, а не в
процентных пунктах, как у трёх конверсий. Отсутствие Джема это **нормальное
состояние кабинета**, а не сбой: без него воронка живёт четырьмя этапами и
ничего не теряет, кроме пятого.

**Источник цифр один и он наш собственный: `nm_daily`.**

Суточную воронку Wildberries отдаёт максимум за последнюю неделю, поэтому её
каждый день забирает сбор плана-факта (`agents/rnp.py`) у всех подключённых
кабинетов, независимо от подписки. Второго сбора здесь нет и быть не должно:
это были бы лишние запросы к Wildberries у каждого клиента каждый день. Отчёт
только читает базу и считает; в Wildberries отсюда ходит ровно один
необязательный сборщик, и только за видимостью.

**Дыры в истории.** Сколько артикулов забирать суточной воронкой, решает
`config.toml`, секция `[funnel]`, `daily_articles`. У кабинета, где артикулов
больше предела, собираются самые оборотистые, и состав меняется день ото дня:
у медленного товара в истории будут пропущенные сутки. Меньше собранных суток
это меньше заходов и заказов, а не упавшая конверсия, поэтому товар с
неполной историей показывается, но в поиск просадки не идёт и в свод кабинета
не складывается. Сколько таких товаров и почему, отчёт говорит вслух.

**Конверсии.** У Wildberries они готовые, но только за сутки и целыми
процентами. На периоде из тридцати суток среднее из тридцати округлённых
целых разошлось бы с кабинетом сильнее, чем честное деление, поэтому на
периоде конверсия считается делением по тому же определению, каким её
определяет сам Wildberries: доля зашедших, положивших в корзину, заказавших.

Деньги здесь есть только в выручке заказов, и она, как везде, лежит целыми
копейками, а считается в `Decimal`.

Тексты для селлера живут в `bot/handlers/funnel.py`. Здесь остались только
строки, которые едут в книгу Excel: названия этапов, подсказки и лист
«Методология». Второй их копии в хендлере нет: подсказка в сообщении и
подсказка в книге обязаны быть одной и той же строкой.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from core import config, db, queue, scheduler, wbapi

logger = logging.getLogger(__name__)

__all__ = [
    "MODULE",
    "TASK_KIND",
    "CLEANUP",
    "PERIODS",
    "PERIOD_TITLES",
    "TOP_SIZE",
    "OK",
    "NO_CATEGORY",
    "NO_JEM",
    "UNAVAILABLE",
    "NO_CABINET",
    "VISIBILITY",
    "TO_CART",
    "TO_ORDER",
    "TO_BUYOUT",
    "STAGES",
    "STAGE_TITLES",
    "ADVICE",
    "Counts",
    "DayLine",
    "Drop",
    "Article",
    "FunnelReport",
    "Visibility",
    "conversion",
    "threshold",
    "min_orders",
    "coverage",
    "daily_articles",
    "history_days",
    "history_since",
    "history_trouble",
    "cleanup",
    "windows",
    "visibility_rows",
    "collect_visibility",
    "build",
    "excel_bytes",
    "file_name",
    "request_report",
    "report_task",
    "set_sender",
    "register_jobs",
    "METHODOLOGY",
]

MODULE = "funnel"

# Вид задачи один: собрать видимость (если она есть) и отдать отчёт. Суточного
# сбора у этого агента нет намеренно, его делает план-факт.
TASK_KIND = "funnel_report"

# Работа расписания одна и в Wildberries не ходит: убрать суточную историю
# глубже срока хранения. Она живёт здесь, а не у плана-факта, потому что
# глубина нужна именно воронке: план-факт читает текущий месяц, а воронка
# сравнивает квартал с кварталом.
CLEANUP = "funnel_cleanup"

# Сколько суток храним суточную историю, если в config.toml про это молчат.
HISTORY_DAYS_DEFAULT = 400

# Периоды те же по смыслу, что у соседних отчётов. Года здесь нет: суточную
# историю бот копит сам, и год её набирается только у давних кабинетов, а
# кнопка, которая у большинства отвечает «столько ещё не накопилось», хуже
# отсутствующей кнопки.
PERIODS: dict[str, int] = {"week": 7, "month": 30, "quarter": 90}

PERIOD_TITLES: dict[str, str] = {
    "week": "последняя неделя",
    "month": "месяц",
    "quarter": "квартал",
}

# Сколько товаров называем в сообщении. Остальные в файле. Это факт про ширину
# экрана телефона, а не настройка владельца.
TOP_SIZE = 5

# Чем закончился необязательный поход за видимостью.
OK = "ok"
NO_CATEGORY = "no_category"      # у токена нет категории «Аналитика»
NO_JEM = "no_jem"                # категория есть, а подписки Джем нет
UNAVAILABLE = "unavailable"      # Wildberries не ответил
NO_CABINET = "no_cabinet"        # кабинет ещё не подключён, токена нет вовсе

# Этапы. Первый необязательный, три остальных есть у всех.
VISIBILITY = "visibility"
TO_CART = "to_cart"
TO_ORDER = "to_order"
TO_BUYOUT = "to_buyout"

# Три конверсии в порядке пути покупателя. Видимости здесь нет: она меряется
# другой меркой (см. THRESHOLDS) и в общий поиск просадки не входит.
STAGES: tuple[str, ...] = (TO_CART, TO_ORDER, TO_BUYOUT)

STAGE_TITLES: dict[str, str] = {
    VISIBILITY: "видимость в поиске",
    TO_CART: "из карточки в корзину",
    TO_ORDER: "из корзины в заказ",
    TO_BUYOUT: "из заказа в выкуп",
}

# Порог каждого этапа: ключ в config.toml и значение, если конфига нет.
# У трёх конверсий порог в процентных пунктах, у видимости в процентах: это
# разные величины, и складывать их нельзя.
THRESHOLDS: dict[str, tuple[str, str]] = {
    TO_CART: ("drop_to_cart", "2"),
    TO_ORDER: ("drop_to_order", "3"),
    TO_BUYOUT: ("drop_to_buyout", "3"),
    VISIBILITY: ("drop_visibility", "15"),
}

# Подсказка по правилам, привязанная к этапу. Это наблюдение плюс
# предположение, и предположение названо предположением: бот видит числа, а
# не фотографии карточки, цену конкурента и наличие товара на складе.
ADVICE: dict[str, str] = {
    TO_CART: (
        "Люди заходят в карточку, но реже кладут товар в корзину. Смотреть "
        "стоит на то, что человек видит первым: главное фото, цена рядом с "
        "ценой соседей по выдаче, первые строки описания. Предположение, а не "
        "диагноз: бот видит только числа."
    ),
    TO_ORDER: (
        "Товар кладут в корзину, но реже доводят до заказа. Чаще всего это "
        "цена, сроки доставки или пропавший размер, но это предположение: "
        "проверьте, был ли товар в наличии все эти дни и не менялась ли цена."
    ),
    TO_BUYOUT: (
        "Заказов столько же, а выкупают их реже. Обычно так бывает, когда "
        "товар не совпал с ожиданием: размер, цвет, качество. Загляните в "
        "свежие отзывы и в таблицу размеров. Это предположение, бот причины "
        "не знает."
    ),
    VISIBILITY: (
        "Видимость в поиске упала: карточку стали реже показывать. Обычно за "
        "этим стоит позиция в выдаче, а на неё влияют ставка в рекламе, "
        "остатки и скорость доставки. Предположение, а не диагноз."
    ),
}

ZERO = Decimal("0")
CENT = Decimal("0.01")

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


# --- настройки из конфига ----------------------------------------------------


def _section() -> Mapping[str, Any]:
    try:
        return config.settings().get("funnel") or {}
    except (KeyError, TypeError, OSError):
        return {}


def _decimal(name: str, default: str) -> Decimal:
    try:
        return Decimal(str(_section().get(name, default)))
    except Exception:  # noqa: BLE001 - испорченная настройка это «не задана»
        return Decimal(default)


def threshold(stage: str) -> Decimal:
    """Порог просадки этапа. Число живёт в `config.toml`, секция `[funnel]`."""
    key, default = THRESHOLDS.get(stage, ("", "0"))
    return _decimal(key, default) if key else ZERO


def min_orders() -> int:
    """Сколько заказов за период нужно, чтобы конверсии товара вообще сравнивать.

    На двух заказах один отказ покупателя уводит выкуп со ста процентов до
    пятидесяти, и это арифметика малых чисел, а не просадка.
    """
    try:
        return max(0, int(_section().get("min_orders", 5)))
    except (TypeError, ValueError):
        return 5


def min_span_days() -> int:
    """Короче скольких суток период не сжимается вовсе.

    История копится с подключения кабинета, и период укорачивается под то,
    что успело накопиться. Но сжать его до одних суток значит сравнить вчера
    с позавчера: конверсия одного дня против другого скачет сама по себе, и
    порог `min_orders` тут не спасает, пять заказов в сутки набираются легко.
    Меньше этого числа честнее сказать «истории ещё не накопилось».
    """
    try:
        return max(1, int(_section().get("min_span_days", 3)))
    except (TypeError, ValueError):
        return 3


def coverage() -> Decimal:
    """Какая доля дней периода должна быть собрана, чтобы сравнивать."""
    value = _decimal("coverage", "0.7")
    return value if ZERO < value <= Decimal("1") else Decimal("0.7")


def daily_articles() -> int:
    """Предел суточного сбора. То же число, по которому живёт `agents/rnp.py`."""
    try:
        return int(_section().get("daily_articles", 200))
    except (TypeError, ValueError):
        return 200


def history_days() -> int:
    """Срок хранения суточной истории в днях. Число из `config.toml`.

    Секция `[storage]`, общая с рекламой: срок один и тот же, а два числа с
    одним смыслом разошлись бы в первый же раз. Ноль и меньше значат «не
    чистить».
    """
    try:
        section = config.settings().get("storage") or {}
        return int(section.get("daily_history_days", HISTORY_DAYS_DEFAULT))
    except (KeyError, TypeError, ValueError, OSError):
        return HISTORY_DAYS_DEFAULT


# --- чистые расчёты ----------------------------------------------------------


def conversion(part: Any, whole: Any) -> Decimal | None:
    """Доля одного этапа в предыдущем, в процентах.

    Без предыдущего этапа конверсии не существует, и это не ноль: ноль
    прочитался бы как «никто не дошёл», а на самом деле никто и не заходил.
    """
    base = Decimal(str(whole or 0))
    if base <= ZERO:
        return None
    return (Decimal(str(part or 0)) * 100 / base).quantize(CENT, rounding=ROUND_HALF_UP)


def _int(value: Any) -> int:
    try:
        return int(Decimal(str(value or 0)))
    except Exception:  # noqa: BLE001 - чужое поле не должно ронять отчёт
        return 0


def _real(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None


@dataclass(frozen=True)
class Counts:
    """Четыре этапа воронки за период и сколько суток в них собрано."""

    opens: int = 0
    carts: int = 0
    orders: int = 0
    buyouts: int = 0
    order_sum_kop: int = 0
    days: int = 0

    @property
    def revenue(self) -> Decimal:
        return db.from_kop(self.order_sum_kop)

    @property
    def to_cart(self) -> Decimal | None:
        return conversion(self.carts, self.opens)

    @property
    def to_order(self) -> Decimal | None:
        return conversion(self.orders, self.carts)

    @property
    def to_buyout(self) -> Decimal | None:
        return conversion(self.buyouts, self.orders)

    def stage(self, name: str) -> Decimal | None:
        return {
            TO_CART: self.to_cart,
            TO_ORDER: self.to_order,
            TO_BUYOUT: self.to_buyout,
        }.get(name)

    @property
    def empty(self) -> bool:
        return not (self.opens or self.carts or self.orders or self.buyouts)


@dataclass(frozen=True)
class Drop:
    """Просевший этап: сколько было, сколько стало и на сколько упало."""

    stage: str
    now: Decimal
    was: Decimal

    @property
    def points(self) -> Decimal:
        """Насколько упала конверсия, в процентных пунктах."""
        return (self.was - self.now).quantize(CENT, rounding=ROUND_HALF_UP)

    @property
    def title(self) -> str:
        return STAGE_TITLES.get(self.stage, self.stage)

    @property
    def advice(self) -> str:
        return ADVICE.get(self.stage, "")


def worst_drop(now: Counts, past: Counts) -> Drop | None:
    """Этап с самой большой просадкой сверх порога. Нет такого - None.

    Сравниваются только три конверсии: они меряются одной меркой, процентными
    пунктами. Видимость сюда не входит (см. `Visibility.fell`): у неё готовая
    динамика Wildberries в процентах, и складывать её с пунктами значило бы
    сравнить разное.

    Больше упало - важнее. При равном падении вперёд идёт этап, который раньше
    на пути покупателя: потерянного там уже не вернуть дальше.
    """
    found: list[Drop] = []
    for stage in STAGES:
        current, was = now.stage(stage), past.stage(stage)
        if current is None or was is None:
            continue
        item = Drop(stage, current, was)
        if item.points >= threshold(stage):
            found.append(item)
    if not found:
        return None
    return max(found, key=lambda item: (item.points, -STAGES.index(item.stage)))


@dataclass(frozen=True)
class Visibility:
    """Пятый этап: видимость в поиске и её готовая динамика, оба поля WB."""

    nm_id: int
    percent: Decimal | None = None
    dynamics: Decimal | None = None
    open_card: int = 0
    avg_position: Decimal | None = None

    @property
    def fell(self) -> bool:
        """Видимость упала сильнее порога. Порог тут в процентах, не в пунктах."""
        if self.dynamics is None:
            return False
        return -self.dynamics >= threshold(VISIBILITY)


@dataclass(frozen=True)
class Article:
    """Один товар: воронка теперь, воронка раньше и что из этого следует."""

    nm_id: int
    title: str = ""
    now: Counts = Counts()
    past: Counts = Counts()
    expected_days: int = 0
    visibility: Visibility | None = None

    @property
    def enough_days(self) -> bool:
        """Собрано ли достаточно суток в обоих периодах.

        Без этого сравнивать нечего: у товара, который не попал в суточный
        сбор, в истории дыры, и меньше собранных суток это меньше заходов, а
        не упавшая конверсия.
        """
        if self.expected_days <= 0:
            return False
        need = Decimal(self.expected_days) * coverage()
        return Decimal(self.now.days) >= need and Decimal(self.past.days) >= need

    @property
    def enough_orders(self) -> bool:
        return self.now.orders >= min_orders() and self.past.orders >= min_orders()

    @property
    def comparable(self) -> bool:
        return self.enough_days and self.enough_orders

    @property
    def drop(self) -> Drop | None:
        """Просевший этап этого товара. Несравнимый товар просадки не имеет."""
        if not self.comparable:
            return None
        return worst_drop(self.now, self.past)


@dataclass(frozen=True)
class DayLine:
    """Одни сутки по всему кабинету: воронка и сколько товаров в них собрано.

    Число собранных товаров лежит в `counts.days`, и это не описка: слот
    складывается из строк, а строка в `nm_daily` это один товар за одни сутки.
    """

    date: str
    counts: Counts

    @property
    def goods(self) -> int:
        return self.counts.days


@dataclass(frozen=True)
class FunnelReport:
    """Данные отчёта. Текстов для селлера здесь нет, их знает хендлер."""

    client_id: int
    period: str
    date_from: date
    date_to: date
    past_from: date
    past_to: date
    span: int = 0
    articles: tuple[Article, ...] = ()
    # Свод по суткам нынешнего периода, по тем же товарам, что и свод целиком.
    days: tuple[DayLine, ...] = ()
    # Первый день, за который в истории вообще что-то есть.
    since: date | None = None
    # Период пришлось укоротить: истории накоплено меньше, чем просили.
    shortened: bool = False
    jem: bool = False
    trouble: str = ""

    @property
    def compared(self) -> tuple[Article, ...]:
        """Товары, которые правда можно сравнивать. На них стоит весь отчёт."""
        return tuple(item for item in self.articles if item.comparable)

    @property
    def partial(self) -> tuple[Article, ...]:
        """Товары с дырами в истории: показать их можно, сравнивать нельзя."""
        return tuple(item for item in self.articles if not item.enough_days)

    @property
    def quiet(self) -> tuple[Article, ...]:
        """История полная, а заказов слишком мало, чтобы верить процентам."""
        return tuple(
            item for item in self.articles if item.enough_days and not item.enough_orders
        )

    @property
    def empty(self) -> bool:
        return not self.compared

    @property
    def title(self) -> str:
        return PERIOD_TITLES.get(self.period, self.period)

    def _counts(self, field: str) -> Counts:
        rows = [getattr(item, field) for item in self.compared]
        return Counts(
            opens=sum(row.opens for row in rows),
            carts=sum(row.carts for row in rows),
            orders=sum(row.orders for row in rows),
            buyouts=sum(row.buyouts for row in rows),
            order_sum_kop=sum(row.order_sum_kop for row in rows),
            days=max((row.days for row in rows), default=0),
        )

    @property
    def now(self) -> Counts:
        """Свод кабинета: складываются только сравнимые товары.

        Товар с дырами в истории сюда не идёт нарочно: он занизил бы и этот
        период, и прошлый по-разному, и свод кабинета стал бы неправдой.
        """
        return self._counts("now")

    @property
    def past(self) -> Counts:
        return self._counts("past")

    @property
    def drop(self) -> Drop | None:
        """Просевший этап по кабинету целиком.

        Ни дыр, ни малых чисел тут проверять не нужно: свод уже сложен из
        товаров, которые обе проверки прошли.
        """
        now, past = self.now, self.past
        if now.empty or past.empty:
            return None
        return worst_drop(now, past)

    @property
    def troubled(self) -> tuple[Article, ...]:
        """Товары с просадкой, от самой большой вниз."""
        found = [item for item in self.compared if item.drop is not None]
        found.sort(key=lambda item: (-(item.drop.points), item.nm_id))
        return tuple(found)

    @property
    def visibility_fell(self) -> tuple[Article, ...]:
        """Товары, у которых просела видимость. Пусто без подписки Джем."""
        found = [
            item
            for item in self.compared
            if item.visibility is not None and item.visibility.fell
        ]
        found.sort(key=lambda item: (item.visibility.dynamics or ZERO, item.nm_id))
        return tuple(found)


# --- границы периодов --------------------------------------------------------


def _yesterday(today: date | None = None) -> date:
    """Последний день отчёта это вчера.

    Сегодняшние сутки не закончились, а Wildberries к тому же предупреждает,
    что часть заказов и переходов доезжает не сразу: свежий день неточен по
    определению, и ставить его в сравнение значит рисовать просадку из
    воздуха.
    """
    today = today or datetime.now(scheduler.tz()).date()
    return today - timedelta(days=1)


def windows(
    period: str, *, today: date | None = None, since: date | None = None
) -> tuple[date, date, date, date, int, bool]:
    """Границы двух периодов: нынешнего и прошлого, равной длины.

    Возвращает (начало, конец, начало прошлого, конец прошлого, длина, укорочен).

    `since` это первый день накопленной истории, и `None` значит, что её нет
    вовсе. Период укорачивается, если истории меньше, чем просили: сравнить
    месяц с месяцем на десяти собранных сутках нельзя, а сравнить пять суток с
    пятью можно, и это честнее, чем пустой ответ. Длина ноль значит, что
    сравнивать пока нечего.
    """
    span = PERIODS.get(period)
    if span is None:
        raise ValueError(f"неизвестный период: {period}")
    last = _yesterday(today)
    have = 0 if since is None else max(0, (last - since).days + 1)
    span = min(span, have // 2)
    # Сравнивать сутки с сутками бот не берётся: такой «отчёт» показал бы
    # скачок одного дня и назвал бы его просадкой. Порог в конфиге.
    if span < min_span_days():
        span = 0
    shortened = span < PERIODS[period]
    if span <= 0:
        return last, last, last, last, 0, True
    date_from = last - timedelta(days=span - 1)
    past_to = date_from - timedelta(days=1)
    past_from = past_to - timedelta(days=span - 1)
    return date_from, last, past_from, past_to, span, shortened


# --- разбор ответа Wildberries о видимости ------------------------------------


def visibility_rows(items: Iterable[Mapping[str, Any]]) -> dict[int, dict]:
    """Товары поискового отчёта в строки таблицы. Ключ это артикул.

    Берётся только то, ради чего мы туда ходили: сама видимость и её готовая
    динамика. Видимость прошлого периода Wildberries не отдаёт, и вычислять
    её из динамики нельзя: видимость приходит целым процентом, и деление на
    такой округлённой цифре наврало бы больше, чем показало.
    """
    found: dict[int, dict] = {}
    for item in items or ():
        row = item or {}
        nm_id = _int(row.get("nmId"))
        if not nm_id:
            continue
        visibility = _dict(row.get("visibility"))
        found[nm_id] = {
            "visibility": _maybe_float(visibility.get("current")),
            "dynamics": _maybe_float(visibility.get("dynamics")),
            "open_card": _int(_dict(row.get("openCard")).get("current")),
            "avg_position": _maybe_float(_dict(row.get("avgPosition")).get("current")),
        }
    return found


def _dict(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _maybe_float(value: Any) -> float | None:
    found = _real(value)
    return None if found is None else float(found)


# --- необязательный поход за видимостью --------------------------------------


def _now_stamp() -> str:
    """Отметка времени в UTC: в базе время хранится только так."""
    return datetime.now(timezone.utc).strftime(TIME_FORMAT)


async def collect_visibility(
    client_id: int,
    date_from: date,
    date_to: date,
    past: tuple[date, date] | None = None,
    *,
    http: Any = None,
    path: str | Path | None = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], Any] | None = None,
) -> str:
    """Забирает видимость за период и кладёт её в базу. Возвращает причину.

    Единственный поход в Wildberries во всём модуле, и он необязательный.
    Раздел поисковых запросов работает только с подпиской Джем, поэтому отказ
    здесь это обычное дело, а не авария: отчёт остаётся четырёхэтапным.

    Отличить «нет подписки» от «нет категории токена» можно только так:
    спросить у самого токена, есть ли у него категория «Аналитика». Если есть,
    а Wildberries всё равно отказал, дело в подписке. Клиенту это важно: в
    первом случае он идёт в кабинет за категорией, во втором не идёт никуда.
    """
    client = wbapi.get_wb_client(client_id, http=http, path=path, clock=clock, sleep=sleep)
    try:
        items = await client.search_report(date_from, date_to, past=past)
    except wbapi.WBForbiddenError:
        reason = NO_JEM if client.has_category("analytics") else NO_CATEGORY
        logger.info("видимость клиента %s не получена: %s", client_id, reason)
        return reason
    except (wbapi.WBUnavailable, wbapi.WBRateLimited, wbapi.WBApiError) as error:
        logger.warning("видимость клиента %s не получена: %s", client_id, error)
        return UNAVAILABLE

    repo = db.repo(client_id, path)
    stamp = _now_stamp()
    keys = {"date_from": date_from.isoformat(), "date_to": date_to.isoformat()}
    for nm_id, values in visibility_rows(items).items():
        repo.upsert("funnel_visibility", {**keys, "nm_id": nm_id}, updated_at=stamp, **values)
    # Видимость это срез, а не история: Wildberries отдаёт её средней за
    # запрошенный период, и прошлый срез уже ни с чем не сравнивается. Границы
    # периода входят в ключ, а период считается от вчера и каждый день другой,
    # поэтому без уборки каждый /funnel оставлял бы в таблице новый набор строк
    # навсегда. Наборы, кончающиеся раньше нынешнего, уходят; наборы с тем же
    # последним днём остаются, это неделя, месяц и квартал одного дня.
    repo.delete_before("funnel_visibility", "date_to", date_to.isoformat())
    return OK


# --- сборка отчёта -----------------------------------------------------------


def _between(stamp: str, first: date, last: date) -> bool:
    return first.isoformat() <= stamp <= last.isoformat()


def _add(slot: dict[str, int], row: Any) -> None:
    slot["opens"] += int(row["open_card_count"] or 0)
    slot["carts"] += int(row["add_to_cart_count"] or 0)
    slot["orders"] += int(row["orders"] or 0)
    slot["buyouts"] += int(row["buyouts"] or 0)
    slot["order_sum_kop"] += int(row["orders_sum_kop"] or 0)
    slot["days"] += 1


def _empty_slot() -> dict[str, int]:
    return {
        "opens": 0,
        "carts": 0,
        "orders": 0,
        "buyouts": 0,
        "order_sum_kop": 0,
        "days": 0,
    }


def _collected(rows: Iterable[Any]) -> list[Any]:
    """Строки, за которыми правда стоят собранные сутки воронки.

    Признак это непустая колонка `raw`: её заполняет только разбор суточной
    воронки. Строка с одними остатками или одним расходом рекламы про воронку
    не говорит ничего, и считать её собранными сутками значило бы обещать
    сравнение там, где сравнивать нечего.
    """
    return [row for row in rows if row["raw"] is not None]


def _first_collected(repo: Any) -> date | None:
    """Первый день среди собранных суток. Считает база, а не Python.

    Признак собранных суток тот же самый: непустая колонка `raw`. Читать ради
    одной даты всю историю кабинета нельзя, поэтому за наименьшей датой идёт
    отдельный запрос: база проходит по индексу и останавливается на первой
    подходящей строке.
    """
    raw = repo.first_value("nm_daily", "date", not_null="raw")
    try:
        return date.fromisoformat(str(raw)[:10])
    except (TypeError, ValueError):
        return None


def history_since(client_id: int, *, path: str | Path | None = None) -> date | None:
    """Первый день, за который суточная воронка вообще собрана.

    Нужна задаче очереди до сборки отчёта: по этому дню считаются границы
    периода, а по границам спрашивается видимость.
    """
    return _first_collected(db.repo(client_id, path))


def history_trouble(
    client_id: int,
    *,
    http: Any = None,
    path: str | Path | None = None,
) -> str:
    """Появится ли суточная история вообще. Пусто - появится, надо подождать.

    Суточную воронку копит сбор плана-факта, а он требует категории токена
    «Аналитика». Без неё `nm_daily` не наполнится никогда, и обещание «через
    пару дней сравнение появится» это неправда, которую клиент будет слушать
    месяцами. Проверка стоит здесь, а не в хендлере: про категории токена
    знает агент, а хендлер знает слова.

    В Wildberries отсюда не ходят: категории записаны в самом токене, и
    читаются они без сети. Отсутствие токена это не отказ Wildberries, поэтому
    ловится отдельно и модули на паузу не ставит: клиенту просто ещё нечего
    показывать.
    """
    try:
        client = wbapi.get_wb_client(client_id, http=http, path=path)
    except wbapi.WBTokenMissing:
        return NO_CABINET
    except wbapi.WBError:  # токен есть, а разобрать его не вышло
        logger.exception("токен клиента %s не прочитался", client_id)
        return ""
    return "" if client.has_category("analytics") else NO_CATEGORY


def cleanup(task: Any = None, *, path: str | Path | None = None) -> int:
    """Убирает суточную историю глубже срока хранения. Возвращает число строк.

    В Wildberries отсюда не ходят: это работа по базе. Чистятся обе таблицы,
    которые копятся сами: суточная воронка с остатками и наборы видимости,
    если какой-то из них остался от кабинета, который давно не спрашивал
    отчёт.
    """
    days = history_days()
    if days <= 0:
        return 0
    today = _payload_date(getattr(task, "payload", None) or {}) or datetime.now(
        scheduler.tz()
    ).date()
    edge = (today - timedelta(days=days)).isoformat()
    removed = 0
    for row in db.admin_repo(path).all_clients():
        client_id = int(row["id"])
        try:
            repo = db.repo(client_id, path)
            removed += repo.delete_before("nm_daily", "date", edge)
            removed += repo.delete_before("funnel_visibility", "date_to", edge)
        except Exception:  # noqa: BLE001 - один клиент не ломает обход
            logger.exception("не удалось почистить историю клиента %s", client_id)
    return removed


def build(
    client_id: int,
    period: str = "month",
    *,
    today: date | None = None,
    path: str | Path | None = None,
    trouble: str = "",
) -> FunnelReport:
    """Отчёт по воронке за период. В Wildberries отсюда не ходят ни разу.

    Всё уже лежит в `nm_daily`: суточную воронку каждый день забирает сбор
    плана-факта у всех подключённых кабинетов. Отчёт только читает и считает.
    """
    if period not in PERIODS:
        raise ValueError(f"неизвестный период: {period}")
    repo = db.repo(client_id, path)
    # Границы периода зависят от накопленной истории, поэтому сначала
    # спрашивается её первый день, и только потом читаются строки. Читаются
    # они одним запросом на оба периода сразу, нынешний и прошлый: ни по
    # запросу на каждый день (у квартала это сто восемьдесят запросов ради
    # одного среза), ни всей таблицей (у кабинета за год это десятки тысяч
    # строк, а sqlite3 живёт в одном процессе с ботом).
    since = _first_collected(repo)
    date_from, date_to, past_from, past_to, span, shortened = windows(
        period, today=today, since=since
    )
    rows = _collected(
        repo.rows_between(
            "nm_daily", "date", past_from.isoformat(), date_to.isoformat()
        )
    )

    now_slots: dict[int, dict[str, int]] = {}
    past_slots: dict[int, dict[str, int]] = {}
    by_day: dict[tuple[str, int], dict[str, int]] = {}
    for row in rows:
        stamp = str(row["date"])[:10]
        nm_id = int(row["nm_id"])
        if _between(stamp, date_from, date_to):
            _add(now_slots.setdefault(nm_id, _empty_slot()), row)
            _add(by_day.setdefault((stamp, nm_id), _empty_slot()), row)
        elif _between(stamp, past_from, past_to):
            _add(past_slots.setdefault(nm_id, _empty_slot()), row)

    seen: dict[int, dict] = {}
    for row in repo.rows(
        "funnel_visibility",
        date_from=date_from.isoformat(),
        date_to=date_to.isoformat(),
    ):
        seen[int(row["nm_id"])] = {
            "percent": _real(row["visibility"]),
            "dynamics": _real(row["dynamics"]),
            "open_card": int(row["open_card"] or 0),
            "avg_position": _real(row["avg_position"]),
        }

    names = _titles(client_id, path=path)
    articles = [
        Article(
            nm_id=nm_id,
            title=names.get(nm_id, ""),
            now=Counts(**now_slots.get(nm_id, _empty_slot())),
            past=Counts(**past_slots.get(nm_id, _empty_slot())),
            expected_days=span,
            visibility=(
                Visibility(nm_id=nm_id, **seen[nm_id]) if nm_id in seen else None
            ),
        )
        for nm_id in sorted(set(now_slots) | set(past_slots))
    ]
    articles.sort(key=lambda item: (-item.now.orders, -item.now.opens, item.nm_id))

    # Свод по суткам считается по тем же товарам, что и свод периода: иначе
    # лист «По дням» и сообщение говорили бы разное об одном и том же.
    wanted = {item.nm_id for item in articles if item.comparable}
    day_slots: dict[str, dict[str, int]] = {}
    for (stamp, nm_id), slot in by_day.items():
        if nm_id not in wanted:
            continue
        into = day_slots.setdefault(stamp, _empty_slot())
        for name, value in slot.items():
            into[name] += value
    days = tuple(
        DayLine(date=stamp, counts=Counts(**slot))
        for stamp, slot in sorted(day_slots.items())
    )

    return FunnelReport(
        client_id=client_id,
        period=period,
        date_from=date_from,
        date_to=date_to,
        past_from=past_from,
        past_to=past_to,
        span=span,
        articles=tuple(articles),
        days=days,
        since=since,
        shortened=shortened,
        jem=bool(seen),
        trouble=trouble,
    )


def _titles(client_id: int, *, path: str | Path | None = None) -> dict[int, str]:
    """Названия карточек из базы. В Wildberries отсюда не ходят.

    Разрез по названиям один на проект и живёт он у агента 3: своей копии
    здесь нет намеренно. Названий может не быть вовсе (за ними ходит модуль
    «Финансы», и нужна категория «Контент»), тогда товар назовётся артикулом.
    """
    from agents import profit

    try:
        return profit.names_of(client_id, path=path)
    except Exception:  # noqa: BLE001 - без названий отчёт живёт, без цифр нет
        logger.exception("названия карточек клиента %s не прочитались", client_id)
        return {}


# --- книга Excel -------------------------------------------------------------

ARTICLES_SHEET = "По товарам"
DAYS_SHEET = "По дням"
METHOD_SHEET = "Методология"

NO_DATA = "нет данных"

ARTICLE_HEADERS = (
    "Артикул WB",
    "Товар",
    "Суток в истории",
    "Заходы в карточку",
    "Заходы, было",
    "В корзину",
    "В корзину, было",
    "Заказы, шт",
    "Заказы было, шт",
    "Выкупы, шт",
    "Выкупы было, шт",
    "Из карточки в корзину, %",
    "В корзину было, %",
    "Из корзины в заказ, %",
    "В заказ было, %",
    "Из заказа в выкуп, %",
    "В выкуп было, %",
    "Видимость, %",
    "Видимость, динамика %",
    "Просевший этап",
    "Что можно сделать",
    "Сравнение",
)

DAY_HEADERS = (
    "Дата",
    "Заходы в карточку",
    "В корзину",
    "Заказы, шт",
    "Выкупы, шт",
    "Из карточки в корзину, %",
    "Из корзины в заказ, %",
    "Из заказа в выкуп, %",
    "Товаров собрано",
)

METHOD_HEADERS = ("Показатель", "Источник", "Формула", "Пояснение")

# Каждая строка: показатель, источник, формула, пояснение. По ней цифру можно
# проверить руками, и здесь же названы все оговорки, которые в сообщение не
# помещаются.
METHODOLOGY: tuple[tuple[str, str, str, str], ...] = (
    (
        "Заходы в карточку",
        "POST /api/analytics/v3/sales-funnel/products/history, поле openCount",
        "сумма openCount по суткам периода",
        "Это первый этап воронки и первое, что Wildberries вообще знает о "
        "покупателе. Показов в воронке нет ни в каком виде: слова «показы» нет "
        "ни в одном её поле, поэтому этапов четыре, а не пять.",
    ),
    (
        "В корзину, заказы, выкупы",
        "то же, поля cartCount, orderCount, buyoutCount",
        "суммы по суткам периода",
        "Выкупы, отмены и возвраты Wildberries записывает в день заказа, а не "
        "в день события: строка за вчера завтра может измениться. Поэтому "
        "суточные данные бот перезаписывает окном в неделю, а не дописывает.",
    ),
    (
        "Конверсии, %",
        "-",
        "в корзину = корзина / заходы * 100, в заказ = заказы / корзина * 100, "
        "в выкуп = выкупы / заказы * 100",
        "У Wildberries есть готовые конверсии, но только за сутки и целыми "
        "процентами. На периоде из тридцати суток среднее из тридцати "
        "округлённых целых разошлось бы с кабинетом сильнее, чем это деление, "
        "и определения тут те же самые, что у него.",
    ),
    (
        "Прошлый период",
        "-",
        "столько же суток подряд сразу перед нынешним периодом",
        "Границы считает бот, а не Wildberries: суточную историю он отдаёт "
        "максимум за последнюю неделю, а всё, что глубже, живёт у нас. Если "
        "накопленной истории меньше, чем просили, оба периода укорачиваются "
        "поровну, и об этом написано в сообщении к отчёту.",
    ),
    (
        "Суток в истории",
        "-",
        "сколько суток периода по этому товару правда собрано",
        "Wildberries отдаёт суточную воронку максимум за последнюю неделю и "
        "не больше 20 артикулов за запрос при трёх запросах в минуту, поэтому "
        "бот забирает её каждый день и хранит у себя, а число товаров за один "
        "заход ограничено настройкой. Если товаров больше предела, берутся "
        "самые оборотистые, и состав меняется день ото дня: у медленного "
        "товара в истории будут пропущенные сутки.",
    ),
    (
        "Сравнение",
        "-",
        "-",
        "Товар сравнивается, только если в обоих периодах собрана достаточная "
        "доля суток и заказов хватает на то, чтобы процентам верить. Иначе в "
        "колонке стоит причина. Меньше собранных суток это меньше заходов и "
        "заказов, а не упавшая конверсия, и выдавать одно за другое нельзя.",
    ),
    (
        "Просевший этап",
        "-",
        "конверсия прошлого периода минус конверсия нынешнего, в п.п.",
        "Порог у каждого этапа свой и живёт в настройках бота. Если просело "
        "несколько этапов, назван тот, где упало сильнее; при равном падении "
        "тот, который раньше на пути покупателя.",
    ),
    (
        "Что можно сделать",
        "-",
        "-",
        "Это подсказка по правилам, а не разбор вашей карточки. Бот видит "
        "числа и не видит ни фотографий, ни цены соседей по выдаче, ни того, "
        "был ли товар на складе. Наблюдение здесь наше, а причина это "
        "предположение.",
    ),
    (
        "Видимость, %",
        "POST /api/v2/search-report/report, поле visibility",
        "готовое поле Wildberries",
        "Пятый этап, и он есть не у всех: раздел поисковых запросов работает "
        "только с подпиской Джем. Это НЕ количество показов, а вероятность в "
        "процентах, что покупатель увидит карточку в поиске; Wildberries "
        "считает её по средней позиции. Динамика к прошлому периоду тоже его "
        "готовое поле и меряется в процентах, а не в процентных пунктах.",
    ),
)


def _cell(value: Any) -> Any:
    """«Неизвестно» словами, а не пустой клеткой."""
    if value is None:
        return NO_DATA
    if isinstance(value, Decimal):
        return value.quantize(CENT, rounding=ROUND_HALF_UP)
    return value


def _why_not(item: Article) -> str:
    if item.comparable:
        return "сравнивается"
    if not item.enough_days:
        return "неполная история: собрано не каждые сутки"
    return f"мало заказов: меньше {min_orders()} за период"


def _day_rows(report: FunnelReport) -> list[list[Any]]:
    """Свод кабинета по суткам. Всё уже посчитано при сборке отчёта."""
    return [
        [
            line.date,
            line.counts.opens,
            line.counts.carts,
            line.counts.orders,
            line.counts.buyouts,
            _cell(line.counts.to_cart),
            _cell(line.counts.to_order),
            _cell(line.counts.to_buyout),
            line.goods,
        ]
        for line in report.days
    ]


def excel_sheets(report: FunnelReport) -> list:
    """Три листа книги: товары, сутки и методология."""
    from core.xlsx import Sheet

    articles = []
    for item in report.articles:
        drop = item.drop
        visibility = item.visibility
        articles.append(
            [
                item.nm_id,
                item.title,
                item.now.days,
                item.now.opens,
                item.past.opens,
                item.now.carts,
                item.past.carts,
                item.now.orders,
                item.past.orders,
                item.now.buyouts,
                item.past.buyouts,
                _cell(item.now.to_cart),
                _cell(item.past.to_cart),
                _cell(item.now.to_order),
                _cell(item.past.to_order),
                _cell(item.now.to_buyout),
                _cell(item.past.to_buyout),
                _cell(visibility.percent if visibility else None),
                _cell(visibility.dynamics if visibility else None),
                drop.title if drop else "",
                drop.advice if drop else "",
                _why_not(item),
            ]
        )
    return [
        Sheet(ARTICLES_SHEET, ARTICLE_HEADERS, articles),
        Sheet(DAYS_SHEET, DAY_HEADERS, _day_rows(report)),
        Sheet(METHOD_SHEET, METHOD_HEADERS, [list(row) for row in METHODOLOGY]),
    ]


def excel_bytes(report: FunnelReport) -> bytes:
    """Книга целиком, байтами: её чаще отправляют, чем сохраняют."""
    from core.xlsx import write_book

    return write_book(excel_sheets(report))


def file_name(report: FunnelReport) -> str:
    return f"funnel-{report.date_from.isoformat()}-{report.date_to.isoformat()}.xlsx"


# --- очередь -----------------------------------------------------------------

_sender: Callable[[int, FunnelReport, bytes], Any] | None = None


def set_sender(fn: Callable[[int, FunnelReport, bytes], Any] | None) -> None:
    """Чем отдаётся готовый отчёт: `fn(client_id, report, xlsx)`."""
    global _sender
    _sender = fn


def request_report(
    client_id: int, period: str = "month", *, path: str | Path | None = None
) -> queue.TaskId:
    """Ставит отчёт в очередь. «Принято» клиенту говорит сама очередь.

    Из хендлера в Wildberries не ходят: у поискового отчёта лимит 3 запроса в
    минуту, и повтор при недоступности живёт только в очереди.
    """
    if period not in PERIODS:
        raise ValueError(f"неизвестный период: {period}")
    return queue.enqueue(client_id, TASK_KIND, {"period": period}, path=path)


def _payload_date(payload: Mapping[str, Any]) -> date | None:
    """День, от которого считается период. Пусто - сегодняшний.

    В задаче он нужен ровно затем, зачем и соседям по очереди: чтобы работу
    можно было переиграть на тех же сутках, а не на тех, в которые её взял
    воркер. Непонятная дата это «сегодня», а не падение задачи.
    """
    raw = str((payload or {}).get("date") or "")
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


async def report_task(task: Any, *, path: str | Path | None = None) -> FunnelReport:
    """Обработчик задачи клиента: сходить за видимостью и отдать отчёт.

    Поход за видимостью необязательный и на отчёт не влияет: без подписки
    Джем он отвечает отказом, и воронка остаётся четырёхэтапной. Сами цифры
    воронки уже лежат в базе, за ними в Wildberries отсюда не ходят.
    """
    client_id = int(task.client_id)
    payload = task.payload or {}
    period = str(payload.get("period") or "month")
    if period not in PERIODS:
        period = "month"
    today = _payload_date(payload)
    since = history_since(client_id, path=path)
    date_from, date_to, past_from, past_to, span, _ = windows(
        period, today=today, since=since
    )
    trouble = ""
    if span > 0:
        trouble = await collect_visibility(
            client_id, date_from, date_to, (past_from, past_to), path=path
        )
    else:
        # Истории нет. Она либо ещё не накопилась, либо не накопится никогда:
        # без категории токена «Аналитика» суточную воронку забрать нечем.
        # Разницу клиент обязан узнать сразу, а не ждать вечно.
        trouble = history_trouble(client_id, path=path)
    report = build(
        client_id,
        period,
        today=today,
        path=path,
        trouble="" if trouble == OK else trouble,
    )
    if _sender is None:
        raise RuntimeError("некому отправить отчёт по воронке: доставка не подключена")
    result = _sender(client_id, report, excel_bytes(report))
    if hasattr(result, "__await__"):
        await result
    return report


def register_jobs() -> None:
    """Задача клиента и утренняя чистка. Суточного сбора здесь нет: он у
    плана-факта.

    Второй сбор суточной воронки означал бы вторую пачку запросов к
    Wildberries у каждого клиента каждый день, а данные там ровно те же.
    Чистка это не сбор: она в Wildberries не ходит и работает по базе.
    """
    queue.register(TASK_KIND, report_task, title="разбор воронки")
    scheduler.register_daily(CLEANUP, cleanup)
