"""Налог селлера: режим, ставка и оценка суммы.

Здесь нет ни одного текста для селлера и ни одной кнопки: это слой данных и
чистый расчёт. Словами про налог говорят `bot/handlers/profit.py` и
`bot/handlers/settings.py`, книгу собирает `agents/profit.py`.

Ради чего написан модуль. Селлер на УСН «доходы» видит поступление от
Wildberries и считает процент с него. Налоговая считает иначе: база это то,
что заплатил покупатель, то есть полная цена продажи вместе с удержанной
комиссией и скидкой площадки. Разница на обороте в миллион это десятки тысяч
рублей, и доначисления по ней настоящие. Оба числа у нас в базе лежат рядом
(`fin_rows.retail_amount_kop` и `fin_rows.ppvz_for_pay_kop`), поэтому бот
показывает базу, поступление и разницу между ними: польза в этой разнице, а
не в самом умножении на ставку.

Что поддерживается. Два режима УСН и только они: ни патента, ни НПД, ни ОСНО.
Решение владельца, а не упрощение по дороге. Ставка у обоих режимов
региональная и льготная, поэтому её выбирает сам селлер, а не код.

Чего бот не делает. Налоговым калькулятором он не становится и налоговых
советов не даёт. Везде это оценка: страховых взносов, вычетов, авансов,
переплат и годового пересчёта бот не видит вовсе.

Налог по товарам (`allocate`) у двух режимов считается принципиально
по-разному, и это не придирка, а то самое правило проекта про уверенный
голос. На «доходах» налог товара это ставка от его собственной выручки за
вычетом возвратов: точная величина, а не доля чего-то общего. На «доходы
минус расходы» база считается по кабинету целиком, и разложить её по товарам
можно только делением, то есть нашей раскладкой. Поэтому `Allocation.exact`
и существует: по нему подписывается колонка в книге, а не по догадке того,
кто её рисует.

Копейки округляются по правилам округления, в каждой строке своей. Под итог
строки не подгоняются, поэтому сумма строк и налог от общей базы расходятся,
и `Allocation` эту разницу разбирает по причинам: подогнанная копейка это
число, которого у товара нет, а названная разница это либо налог с базы, у
которой нет товара, либо копейки округления строк. Эти две причины намеренно
не сливаются в одну: разницу в рубли назвать округлением значило бы уверенным
голосом сказать неправду про настоящую нехватку налога в столбце.

Белый список ставок лежит здесь, а не в экране настроек: `callback_data`
приходит от клиента и подделывается свободно, а нарисованные кнопки ничего
не ограничивают. Один список на проект значит, что проверка и кнопки не
разойдутся.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Mapping

from core import clients, config

__all__ = [
    "USN_INCOME",
    "USN_INCOME_MINUS",
    "MODES",
    "MODE_TITLES",
    "RATE_CHOICES",
    "MIN_TAX_PERCENT",
    "SETTINGS_KEY",
    "MISSING_COST",
    "MISSING_ADS",
    "Rule",
    "Estimate",
    "Allocation",
    "allocate",
    "default_rate",
    "rate_allowed",
    "rule_of",
    "set_rule",
    "clear_rule",
]

ZERO = Decimal("0")
CENT = Decimal("0.01")
# Половина копейки: ровно столько ошибается округление одной строки. Предел
# разницы между суммой строк и налогом от общей базы считается от него, а не
# от числа товаров: товар с нулевой выручкой округлять нечего, и запас на него
# завысил бы предел и позволил назвать округлением то, что им не является.
HALF_CENT = Decimal("0.005")
HUNDRED = Decimal("100")

# Ключи режимов. Это ключи, а не названия: названия ниже и живут только ради
# книги Excel, в сообщения бота они едут через `bot.texts.fill`.
USN_INCOME = "usn_income"
USN_INCOME_MINUS = "usn_income_minus"

MODES: tuple[str, ...] = (USN_INCOME, USN_INCOME_MINUS)

MODE_TITLES: dict[str, str] = {
    USN_INCOME: "УСН «доходы»",
    USN_INCOME_MINUS: "УСН «доходы минус расходы»",
}

# Ставки, из которых селлер выбирает. Это не настройка владельца, а факт про
# налоговый кодекс: на «доходах» регион вправе опустить ставку до одного
# процента, на «доходы минус расходы» до пяти. Поэтому список стоит здесь, а
# не в config.toml, ровно как лимиты методов WB стоят в core/wbapi/limits.py.
# Из этого же списка экран настроек рисует кнопки: два списка разошлись бы
# молча, и кнопка предлагала бы ставку, которую расчёт не принимает.
RATE_CHOICES: dict[str, tuple[int, ...]] = {
    USN_INCOME: (1, 2, 3, 4, 5, 6),
    # Промежуточные региональные ставки бывают любыми целыми от пяти до
    # пятнадцати, но кнопок под каждую нет: разницу между 11 и 12 процентами
    # не заметит никто, а десяток кнопок в экране заметят все. Взяты самые
    # частые льготные и общая.
    USN_INCOME_MINUS: (5, 7, 10, 15),
}

# Ставки по умолчанию, если config.toml до нас не дошёл. Сами числа живут в
# секции `[tax]`: это решение владельца, а не факт про Wildberries.
DEFAULT_RATES: dict[str, Decimal] = {
    USN_INCOME: Decimal("6"),
    USN_INCOME_MINUS: Decimal("15"),
}

# Правило минимального налога: на «доходы минус расходы» за год платится не
# меньше одного процента от доходов. Это норма кодекса, а не порог владельца.
MIN_TAX_PERCENT = Decimal("1")

# Ключ в `clients.settings`. Словарь там общий на весь проект, поэтому читаем
# его целиком и пишем целиком: чужие ключи затирать нельзя.
SETTINGS_KEY = "tax"

# Чего может не хватить в расходах на «доходы минус расходы». Это ключи, а не
# тексты: словами про них говорит хендлер.
MISSING_COST = "cost"
MISSING_ADS = "ads"


def _money(value: Any) -> Decimal:
    """Число из настройки. Мусор это ошибка, а не тихий ноль."""
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value if value is not None else 0))
    except (ArithmeticError, TypeError, ValueError) as error:
        raise ValueError(f"не число: {value!r}") from error


def _cents(value: Decimal) -> Decimal:
    """Округление денег такое же, как у соседей: до копейки, half up."""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


# --- режим и ставка -----------------------------------------------------------


def default_rate(mode: str) -> Decimal:
    """Ставка режима по умолчанию. Число живёт в `config.toml`, секция `[tax]`.

    В коде его нет намеренно: шесть и пятнадцать это общие ставки, и менять их
    владельцу проще правкой конфига, чем правкой расчёта. Личная ставка
    селлера лежит в его настройках и перебивает эту.
    """
    fallback = DEFAULT_RATES.get(mode, ZERO)
    try:
        section = config.settings().get("tax") or {}
        value = _money(section.get(mode, fallback))
    except (KeyError, TypeError, ValueError, OSError):
        return fallback
    return value if rate_allowed(mode, value) else fallback


def rate_allowed(mode: str, rate: Any) -> bool:
    """Есть ли такая ставка в белом списке режима.

    Отрицательная ставка, двести процентов и слово вместо числа сюда не
    проходят: проверка стоит на списке, а не на границах, потому что список
    и рисуется кнопками.
    """
    if mode not in RATE_CHOICES:
        return False
    try:
        value = _money(rate)
    except ValueError:
        return False
    return any(value == Decimal(choice) for choice in RATE_CHOICES[mode])


@dataclass(frozen=True)
class Rule:
    """Что селлер выбрал. Пустое правило значит «режим не выбран».

    Не выбран это не «ноль процентов»: молчаливый ноль приписал бы селлеру
    прибыль, которой у него нет, и был бы хуже отсутствия строки.
    """

    mode: str | None = None
    rate: Decimal | None = None

    @property
    def known(self) -> bool:
        return self.mode in MODES and self.rate is not None

    @property
    def title(self) -> str:
        return MODE_TITLES.get(self.mode or "", "")

    @property
    def rate_text(self) -> str:
        """Ставка числом, без хвостовых нулей: 6, а не 6.00."""
        if self.rate is None:
            return ""
        text = f"{self.rate:f}"
        return text.rstrip("0").rstrip(".") if "." in text else text

    @property
    def with_expenses(self) -> bool:
        """Режим, у которого в базе участвуют расходы."""
        return self.mode == USN_INCOME_MINUS


def rule_of(client_id: int, *, path: str | Path | None = None) -> Rule:
    """Режим и ставка этого селлера. Не выбран - пустое правило.

    Испорченная настройка тоже читается как «не выбран». Подставить вместо неё
    ставку по умолчанию значило бы посчитать налог по числу, которого селлер
    не выбирал, и назвать это его налогом.
    """
    raw = clients.settings_of(client_id, path=path).get(SETTINGS_KEY)
    raw = raw if isinstance(raw, dict) else {}
    mode = raw.get("mode")
    if mode not in MODES:
        return Rule()
    stored = raw.get("rate")
    if stored is None:
        # Режим выбран, ставки нет: селлер согласился с той, что ему показали.
        return Rule(mode=mode, rate=default_rate(mode))
    if not rate_allowed(mode, stored):
        return Rule()
    return Rule(mode=mode, rate=_money(stored))


def set_rule(
    client_id: int,
    mode: Any,
    rate: Any = None,
    *,
    path: str | Path | None = None,
) -> Rule:
    """Ставит режим и ставку селлера. Чужого режима и чужой ставки не бывает.

    `callback_data` приходит от клиента и подделывается свободно, поэтому
    проверка стоит здесь, у самой записи, а не только у кнопки: до настроек
    кабинета доходит лишь то, что есть в белом списке.
    """
    if mode not in MODES:
        raise ValueError(f"неизвестный режим налогообложения: {mode!r}")
    value = default_rate(mode) if rate is None else _money(rate)
    if not rate_allowed(mode, value):
        raise ValueError(f"ставка {rate!r} не из списка режима {mode}")
    data = clients.settings_of(client_id, path=path)
    data[SETTINGS_KEY] = {"mode": mode, "rate": str(value)}
    clients.save_settings(client_id, data, path=path)
    return rule_of(client_id, path=path)


def clear_rule(client_id: int, *, path: str | Path | None = None) -> Rule:
    """Убирает режим: налог снова не считается, и отчёт об этом говорит."""
    data = clients.settings_of(client_id, path=path)
    data.pop(SETTINGS_KEY, None)
    clients.save_settings(client_id, data, path=path)
    return rule_of(client_id, path=path)


# --- оценка -------------------------------------------------------------------


@dataclass(frozen=True)
class Estimate:
    """Оценка налога за период. Не «налог к уплате», и так и называется.

    `income` это база «доходов»: то, что заплатил покупатель, за вычетом
    возвратов. `transferred` рядом не для красоты: селлер сверяет её с
    поступлением на счёт и видит, что налог считается не с неё.

    `expenses` есть только у «доходы минус расходы». `missing` говорит, чего в
    этих расходах не хватило: неполные расходы завышают налог, и молчать об
    этом нельзя.
    """

    rule: Rule
    income: Decimal = ZERO
    transferred: Decimal = ZERO
    expenses: Decimal | None = None
    missing: tuple[str, ...] = ()

    @property
    def gap(self) -> Decimal:
        """Разница между базой и поступлением: комиссия, логистика и скидка."""
        return _cents(self.income - self.transferred)

    @property
    def base(self) -> Decimal:
        if self.expenses is None:
            return _cents(self.income)
        return _cents(self.income - self.expenses)

    @property
    def loss(self) -> bool:
        """Расходы съели доходы. Налог с отрицательной базы не берётся."""
        return self.base <= ZERO

    @property
    def amount(self) -> Decimal:
        """Оценка налога. С нулевой и отрицательной базы это ноль."""
        if self.rule.rate is None or self.base <= ZERO:
            return ZERO
        return _cents(self.base * self.rule.rate / HUNDRED)

    @property
    def minimum(self) -> Decimal:
        """Один процент от доходов периода. Справочно: правило годовое.

        Считать минимальный налог как «к уплате» за неделю или месяц нельзя:
        он определяется по итогам года и сравнивается с годовым налогом.
        Поэтому отметка показывается, но платежом не называется.
        """
        return _cents(self.income * MIN_TAX_PERCENT / HUNDRED)

    @property
    def below_minimum(self) -> bool:
        """Расчётный налог периода ниже годовой отметки в один процент."""
        return self.expenses is not None and self.amount < self.minimum

    @property
    def complete(self) -> bool:
        return not self.missing

    def after(self, profit: Decimal | None) -> Decimal | None:
        """Прибыль после налога. Неизвестная прибыль остаётся неизвестной."""
        if profit is None:
            return None
        return _cents(_money(profit) - self.amount)


# --- налог по товарам ---------------------------------------------------------


@dataclass(frozen=True)
class Allocation:
    """Налог по товарам рядом с налогом по кабинету.

    `exact` это не украшение, а подпись колонки в книге. На «доходах» в
    `parts` лежит налог именно этого товара: ставка от его выручки за вычетом
    возвратов, и так колонка и называется. На «доходы минус расходы» в
    `parts` лежит наша раскладка кабинетного налога, и назвать её налогом
    товара нельзя: база считалась по кабинету целиком.

    `amount` это налог кабинета, посчитанный от общей базы, то есть настоящий.
    `total` это сумма строк, и сойтись они обязаны не всегда: каждая строка
    округлена сама по себе, по правилам округления, а не подогнана под итог.
    Разница `gap` складывается из двух разных вещей, и называть их одним словом
    нельзя: это налог с базы, у которой нет товара (`uncovered_tax`, он
    известен точно), и копейки округления строк (`residue`, он ограничен
    половиной копейки на округляемую строку). Прятать разницу нельзя, поэтому
    она здесь разобрана, а в книге названа словами на листе «Методология».

    `uncovered` это та часть базы налога, у которой нет артикула: строки отчёта
    о реализации без `nmId` в разрез по товарам не попадают, а в базу входят.
    Считается она только на «доходах»: на «доходы минус расходы» кабинетный
    налог раскладывается по весам целиком, и несовпавшей базы там не бывает.

    `rounded_rows` это число строк, которые действительно округлялись. Предел
    округления считается от него, а не от числа товаров: строка с нулевой
    выручкой даёт ровный ноль, и запас на неё позволил бы назвать округлением
    разницу, которой округление не даёт.
    """

    parts: Mapping[Any, Decimal] = None  # type: ignore[assignment]
    amount: Decimal = ZERO
    exact: bool = True
    uncovered: Decimal = ZERO
    uncovered_tax: Decimal = ZERO
    rounded_rows: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "parts", dict(self.parts or {}))

    @property
    def total(self) -> Decimal:
        """Сумма налога по товарам: ровно то, что даст сложение столбца."""
        return _cents(sum(self.parts.values(), ZERO))

    @property
    def gap(self) -> Decimal:
        """Насколько налог от общей базы разошёлся с суммой строк."""
        return _cents(self.amount - self.total)

    @property
    def matches(self) -> bool:
        return self.gap == ZERO

    @property
    def residue(self) -> Decimal:
        """Разница за вычетом налога с базы без товара: это и есть копейки."""
        return _cents(self.gap - self.uncovered_tax)

    @property
    def rounding_limit(self) -> Decimal:
        """Предел, который даёт округление: половина копейки на строку.

        Две строки запаса это само округление налога кабинета и округление
        налога с базы без товара: оба числа тоже квантованы до копейки.
        """
        return HALF_CENT * (self.rounded_rows + 2)

    @property
    def explained(self) -> bool:
        """Разница объясняется налогом с базы без товара и округлением."""
        return abs(self.residue) <= self.rounding_limit

    @property
    def rounding(self) -> bool:
        """Разница это только округление строк, и больше ничего."""
        return self.explained and self.uncovered_tax == ZERO

    @property
    def negative(self) -> tuple[Any, ...]:
        """Товары, у которых налог вышел с минусом.

        Это не ошибка расчёта: товар, проданный в прошлом периоде и
        вернувшийся в этом, даёт за период отрицательную выручку, и возврат
        по-настоящему уменьшает налог периода. Отсекать минус в ноль нельзя,
        иначе сумма строк перестанет сходиться с налогом кабинета. Поэтому
        строки названы здесь: книга обязана объяснить минус словами.
        """
        return tuple(key for key, value in self.parts.items() if value < ZERO)

    @property
    def negative_total(self) -> Decimal:
        """Сколько налога сняли возвраты. Отрицательное число или ноль."""
        return _cents(sum((value for value in self.parts.values() if value < ZERO), ZERO))

    def of(self, key: Any) -> Decimal | None:
        """Налог товара или None, если такого товара в раскладке нет."""
        return self.parts.get(key)


def allocate(
    estimate: Estimate,
    bases: Mapping[Any, Decimal],
) -> Allocation:
    """Налог по товарам. `bases` это выручка товара за вычетом возвратов.

    Один вход и два разных ответа, потому что режимы разные.

    На «доходах» выручка товара и есть его база налога, поэтому в ответе
    ставка от неё: точное число этого товара, а не доля чего-то общего.

    На «доходы минус расходы» база считается по кабинету: доходы минус
    принимаемые расходы. Разложить её по товарам можно только делением, и
    делим мы по выручке за вычетом возвратов. Почему по ней: это
    единственный вес, который есть у каждого товара и не уводит результат в
    бессмыслицу. По прибыли делить нельзя: у убыточного товара доля вышла бы
    отрицательной, то есть налог в минус, а при неположительной прибыли по
    кабинету доли не существует вовсе, хотя налог платится. Товар без выручки
    доли не получает, ровно как при разнесении обезлички.

    Отрицательные числа у двух ветвей трактуются по-разному, и это не
    недосмотр одной из них. В раскладке отрицательный вес отсекается: доли
    меньше нуля не существует, такой товар просто не участвует в делении. В
    точной ветви знак не трогается: минусовая выручка за период бывает
    по-настоящему (товар продался раньше, а вернулся сейчас), возврат
    по-настоящему уменьшает налог периода, и минус в строке это правда.
    Отсечь его в ноль значило бы развалить сложение: сумма строк перестала бы
    сходиться с налогом кабинета, а сложение это главная проверка селлера.
    Чинить здесь нужно не число, а подпись, и этим занята книга
    (`agents/profit.py` называет минус словами).

    Каждая строка округляется до копейки по правилам округления, сама по
    себе. Остаток по строкам не раздаётся и ни одна строка под итог не
    подкручивается: подкрученная копейка это число, которого у товара нет.
    Поэтому сумма строк и налог от общей базы могут разойтись, и `Allocation`
    эту разницу разбирает: налог с базы без товара отдельно, копейки
    округления отдельно.
    """
    rule = estimate.rule
    keys = list(bases)
    exact = not rule.with_expenses
    amount = estimate.amount
    if not rule.known or not keys or amount == ZERO:
        # Неположительная база это настоящий ноль налога за период, а не
        # «не посчитали»: ноль в каждой строке здесь правда.
        return Allocation({key: ZERO for key in keys}, amount=amount, exact=exact)

    if rule.with_expenses:
        weights = {
            key: _money(value) for key, value in bases.items() if _money(value) > ZERO
        }
        whole = sum(weights.values(), ZERO)
        parts = {
            key: (_cents(amount * weights[key] / whole) if key in weights else ZERO)
            for key in keys
        }
        # Кабинетный налог разложен по весам целиком, несовпавшей базы нет:
        # вся разница с итогом здесь это округление делённых строк.
        return Allocation(
            parts, amount=amount, exact=False, rounded_rows=len(weights)
        )

    rate = rule.rate or ZERO
    parts = {key: _cents(_money(value) * rate / HUNDRED) for key, value in bases.items()}
    # База, у которой нет товара: строки отчёта без nmId в разрез по товарам не
    # попадают, а в базу налога входят. Это известное число, и разницу с итогом
    # оно объясняет точно, а не «похоже на округление». Отрицательной она быть
    # не может (сумма по товарам больше базы периода означала бы ошибку
    # сборщика), и такой случай объяснением не прикрывается: он уйдёт в
    # необъяснённую разницу, где его видно.
    uncovered = _cents(estimate.income - sum((_money(v) for v in bases.values()), ZERO))
    if uncovered < ZERO:
        uncovered = ZERO
    return Allocation(
        parts,
        amount=amount,
        exact=True,
        uncovered=uncovered,
        uncovered_tax=_cents(uncovered * rate / HUNDRED),
        rounded_rows=sum(1 for value in bases.values() if _money(value) != ZERO),
    )
