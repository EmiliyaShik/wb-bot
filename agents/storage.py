"""Агент 8. Хранение и приёмка по товарам: второй источник тех же денег.

Зачем он есть. Владелец просила колонку «Хранение» у каждого товара, а в
отчёте о реализации все строки хранения приходят **без артикула**: деньги
лежат общей статьёй и разносятся пропорционально выручке. Для хранения это
плохое основание, оно зависит от объёма и времени на складе, а не от выручки:
крупный медленный товар получал бы копейки.

У Wildberries есть отдельный отчёт о платном хранении, и в нём сумма стоит
по артикулу и по суткам. Такой же отчёт есть про приёмку. Оба устроены
одинаково: три шага с ожиданием готовности (заказать задание, дождаться
`done`, скачать), хост `seller-analytics-api`, категория токена «Аналитика».
Поэтому агент один, а источник стоит колонкой `kind`: два близнеца означали
бы две копии сборщика и читателя, которые разошлись бы молча.

**Это второй источник тех же денег, и сойтись с первым он не обязан.** Общего
ключа у двух отчётов нет вовсе, и Wildberries нигде не обещает, что суммы
совпадут. Причины расходиться названы в его же документации: перерасчёты
задним числом (`date` против `originalDate`), сутки против недель, скидка
лояльности отдельным полем, разные сервисы. Поэтому:

- в финансовой раскладке `/finance` эти цифры **ничего не подменяют**: там
  `paidStorage` и `paidAcceptance` из отчёта о реализации, это документ
  взаиморасчётов, и сверка с итогами Wildberries стоит на нём;
- в `/profit` колонка названа своим источником, а разница между двумя числами
  показывается, а не прячется. Тот же приём, что в сверке `/finance` и в двух
  цифрах расхода `/ads`;
- деньги не пропадают и не удваиваются: в прибыли их столько, сколько их в
  большем из двух источников, см. `agents.profit`.

Сутки, которые собрали, пишутся отдельно (`expense_days`). Без этого «за эти
сутки хранения не было» и «эти сутки мы не собирали» выглядят одинаково: в
обоих случаях строк нет. А разница тут главная, потому что только она
позволяет честно сравнивать сумму столбца с отчётом о реализации.

История копится ночным сбором, как у рекламы, и по той же причине: окно
одного задания восемь суток, а догнать год задним числом это 46 окон, больше
полутора часов машинного времени на кабинет. Поэтому каждую ночь берётся
свежее окно (его данные ещё меняются перерасчётами) плюс несколько окон
истории вглубь (`[storage] backfill_windows`): история дотягивается сама, а
кабинет не занят час подряд. Глубже самой ранней выгруженной недели отчёта о
реализации сбор не идёт: прибыль всё равно считается по этим неделям.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from core import db, queue, scheduler, wbapi

logger = logging.getLogger(__name__)

__all__ = [
    "STORAGE",
    "ACCEPTANCE",
    "KINDS",
    "SOURCE_TITLES",
    "COLUMN_TITLES",
    "BLOCK_TITLES",
    "TASK_KINDS",
    "COLLECT_ALL",
    "COLLECT_ONE",
    "CLEANUP",
    "OK",
    "NO_CATEGORY",
    "UNAVAILABLE",
    "NOT_COLLECTED",
    "Collected",
    "Spend",
    "collect_days",
    "backfill_windows",
    "history_days",
    "storage_rows",
    "acceptance_rows",
    "parse_rows",
    "day_totals",
    "collect",
    "save",
    "spend_of",
    "covered_days",
    "missing_days",
    "windows_for",
    "request_collect",
    "fan_out_collect",
    "collect_client",
    "cleanup",
    "register_jobs",
]

# Источники. Это значения колонки `kind` в базе, а не тексты: словами про них
# говорят `SOURCE_TITLES` и хендлер отчёта.
STORAGE = "storage"
ACCEPTANCE = "acceptance"
KINDS: tuple[str, ...] = (STORAGE, ACCEPTANCE)

# Чем источник назван в книге и в сообщении. Название источника обязательно:
# это другой отчёт Wildberries, и молча подписать колонку «Хранение» значило
# бы обещать то же число, что в отчёте о реализации.
SOURCE_TITLES: dict[str, str] = {
    STORAGE: "отчёт WB о платном хранении",
    ACCEPTANCE: "отчёт WB о приёмке",
}

# Заголовок колонки в книге `/profit`.
COLUMN_TITLES: dict[str, str] = {
    STORAGE: "Хранение по отчёту WB о платном хранении, ₽",
    ACCEPTANCE: "Приёмка по отчёту WB о приёмке, ₽",
}

# Подпись блока в листе проблем книги `/profit`: там селлер ищет деньги,
# которых не нашёл в колонках.
BLOCK_TITLES: dict[str, str] = {
    STORAGE: "хранение по отчёту WB",
    ACCEPTANCE: "приёмка по отчёту WB",
}

# Виды задач. Первая работа расписания ставит вторую на каждый подключённый
# кабинет: недоступность WB у одного клиента не отменяет сбор у остальных, а
# повтор достаётся ровно тому, кто упал. Третья чистит историю глубже срока.
COLLECT_ALL = "storage_collect"
COLLECT_ONE = "storage_collect_client"
CLEANUP = "storage_cleanup"
TASK_KINDS: tuple[str, ...] = (COLLECT_ALL, COLLECT_ONE, CLEANUP)

# Почему данных может не быть. Ключи, а не тексты.
OK = "ok"
NO_CATEGORY = "no_category"
UNAVAILABLE = "unavailable"
NOT_COLLECTED = "not_collected"

# Какой метод клиента WB отвечает за источник. Имя метода, а не сам метод:
# клиент создаётся на кабинет, а эта таблица общая.
FETCH: dict[str, str] = {STORAGE: "paid_storage", ACCEPTANCE: "acceptance_report"}

# Окно одного задания Wildberries. Сами числа живут в `core.wbapi`: это факт
# про WB, и второго места у него быть не должно.
WINDOW_DAYS: dict[str, int] = {
    STORAGE: wbapi.PAID_STORAGE_MAX_DAYS,
    ACCEPTANCE: wbapi.ACCEPTANCE_MAX_DAYS,
}

# Запасные значения настроек, если в `config.toml` про них молчат.
COLLECT_DAYS_DEFAULT = 8
BACKFILL_WINDOWS_DEFAULT = 2
HISTORY_DAYS_DEFAULT = 400

ZERO = Decimal("0")
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def _money(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value or 0))
    except Exception:  # noqa: BLE001 - чужое поле не должно ронять сбор
        return ZERO


def _int(value: Any) -> int:
    try:
        return int(_money(value))
    except Exception:  # noqa: BLE001
        return 0


def _now() -> str:
    """Отметка времени в UTC: в базе время хранится только так."""
    return datetime.now(timezone.utc).strftime(TIME_FORMAT)


def _conf() -> dict[str, Any]:
    from core import config

    try:
        return dict(config.settings().get("storage") or {})
    except Exception:  # noqa: BLE001 - настройки не роняют сбор
        return {}


def collect_days() -> int:
    """Окно свежего сбора в сутках. Число из `config.toml`, секция `[storage]`.

    Свежее окно берётся каждую ночь целиком, а не «вчерашние сутки»: у
    Wildberries бывают перерасчёты задним числом, и сутки, собранные позавчера,
    сегодня могут оказаться другими. Больше окна одного задания просить
    бессмысленно, поэтому значение подрезается им же.
    """
    try:
        value = int(_conf().get("collect_days", COLLECT_DAYS_DEFAULT))
    except (TypeError, ValueError):
        value = COLLECT_DAYS_DEFAULT
    return max(1, min(value, WINDOW_DAYS[STORAGE]))


def backfill_windows() -> int:
    """Сколько окон истории догонять за одну ночь. Число из `config.toml`.

    Ноль и меньше значат «историю не догонять»: тогда копится только то, что
    идёт вперёд со дня подключения. Это дорогая настройка: одно окно это
    примерно две минуты машинного времени на кабинет, потому что создание
    задания и скачивание отчёта идут по дорожке «1 запрос в минуту».
    """
    try:
        return max(0, int(_conf().get("backfill_windows", BACKFILL_WINDOWS_DEFAULT)))
    except (TypeError, ValueError):
        return BACKFILL_WINDOWS_DEFAULT


def history_days() -> int:
    """Срок хранения суточной истории в днях. Тот же, что у рекламы и воронки.

    Секция `[storage]`, ключ `daily_history_days`: один срок на всю суточную
    историю кабинета, иначе два числа с одним смыслом разошлись бы в первый
    же раз. Ноль и меньше значат «не чистить».
    """
    try:
        return int(_conf().get("daily_history_days", HISTORY_DAYS_DEFAULT))
    except (TypeError, ValueError):
        return HISTORY_DAYS_DEFAULT


# --- разбор ответа WB --------------------------------------------------------


def _day(value: Any) -> str:
    """Дата WB в вид ГГГГ-ММ-ДД. В базе всё без часовых поясов."""
    return str(value or "")[:10]


def storage_rows(payload: Iterable[Mapping[str, Any]]) -> dict[tuple[str, int], dict]:
    """Строки отчёта о платном хранении, сложенные по суткам и артикулу.

    Строка Wildberries это сутки плюс поставка плюс размер, поэтому складывать
    приходится: колонка в книге одна на товар.

    Дата берётся из поля `date` («дата, за которую был расчёт или
    перерасчёт»), а не из `originalDate`: нам нужны сутки, за которые
    Wildberries начислил деньги сейчас. Именно поэтому окно сбора
    переписывается целиком, а не дополняется: перерасчёт меняет уже собранные
    сутки, и дополнение сложило бы старое с новым.

    Скидка лояльности лежит отдельным полем и в сумму не вмешивается: вычтена
    ли она из `warehousePrice`, в документации Wildberries не сказано, а
    вычесть её самим значит придумать правило, которого у WB нет.
    """
    return _fold(
        payload,
        date_field="date",
        nm_fields=("nmId", "nmID"),
        amount_field="warehousePrice",
        units_field="barcodesCount",
        discount_field="loyaltyDiscount",
    )


def acceptance_rows(payload: Iterable[Mapping[str, Any]]) -> dict[tuple[str, int], dict]:
    """Строки отчёта о приёмке, сложенные по дате приёмки и артикулу.

    Артикул здесь называется `nmID`, с заглавной D, а в хранении `nmId`: это
    факт про Wildberries, а не опечатка, и поэтому разбор смотрит оба имени.
    Скидки у приёмки нет вовсе.
    """
    return _fold(
        payload,
        date_field="shkCreateDate",
        nm_fields=("nmID", "nmId"),
        amount_field="total",
        units_field="count",
        discount_field="",
    )


def _fold(
    payload: Iterable[Mapping[str, Any]],
    *,
    date_field: str,
    nm_fields: Sequence[str],
    amount_field: str,
    units_field: str,
    discount_field: str,
) -> dict[tuple[str, int], dict]:
    """Складывает строки отчёта по суткам и артикулу.

    Строка без артикула в разрез по товарам не попадает: поставить её на
    случайный товар нельзя, а выдумать артикул тем более. Деньги при этом не
    теряются: суточный итог (`expense_days`) считается по всем строкам, и
    разница между ним и суммой по товарам видна в отчёте. Артикул с нулевым
    номером это тоже «нет артикула»: у Wildberries так приходит отсутствующее
    значение.
    """
    folded: dict[tuple[str, int], dict] = {}
    for item in payload or []:
        row = item or {}
        day = _day(row.get(date_field))
        if not day:
            continue
        nm_id = 0
        for name in nm_fields:
            nm_id = _int(row.get(name))
            if nm_id:
                break
        if not nm_id:
            continue
        key = (day, nm_id)
        found = folded.setdefault(
            key, {"amount": ZERO, "discount": ZERO, "units": 0, "rows": 0}
        )
        # Складывается в рублях и копейками становится один раз, в самом
        # конце. Wildberries отдаёт сумму хранения точнее копейки (на живом
        # кабинете это 0,89528 ₽ за строку), а строка это сутки плюс поставка
        # плюс размер, и округли каждую по отдельности - на восьми сутках
        # набежит рубля два, на годе под сотню. Деньги в этом проекте
        # считаются в Decimal ровно за этим.
        found["amount"] += _money(row.get(amount_field))
        if discount_field:
            found["discount"] += _money(row.get(discount_field))
        found["units"] += _int(row.get(units_field))
        found["rows"] += 1
    return {
        key: {
            "amount_kop": db.to_kop(values["amount"]),
            "discount_kop": db.to_kop(values["discount"]),
            "units": values["units"],
            "rows": values["rows"],
        }
        for key, values in folded.items()
    }


def day_totals(
    payload: Iterable[Mapping[str, Any]],
    *,
    kind: str,
) -> dict[str, dict]:
    """Итог по суткам из всех строк отчёта, включая строки без артикула.

    Нужен двум вещам сразу. Во-первых, он отмечает собранные сутки: сутки без
    расхода это строка с нулём, а не отсутствие строки, иначе её не отличить
    от несобранных. Во-вторых, он честно считает весь расход отчёта, а по
    товарам раскладывается только та его часть, у которой артикул есть.
    """
    date_field = "date" if kind == STORAGE else "shkCreateDate"
    amount_field = "warehousePrice" if kind == STORAGE else "total"
    totals: dict[str, dict] = {}
    for item in payload or []:
        row = item or {}
        day = _day(row.get(date_field))
        if not day:
            continue
        found = totals.setdefault(day, {"amount": ZERO, "rows": 0})
        # В рублях и до самого конца, по той же причине, что в `_fold`: сумма
        # строки точнее копейки, и округление каждой из них порознь стоило бы
        # рублей.
        found["amount"] += _money(row.get(amount_field))
        found["rows"] += 1
    return {
        day: {"amount_kop": db.to_kop(values["amount"]), "rows": values["rows"]}
        for day, values in totals.items()
    }


def parse_rows(payload: Iterable[Mapping[str, Any]], kind: str) -> dict[tuple[str, int], dict]:
    """Разбор по источнику. Одно место, где эти два отчёта различаются."""
    return storage_rows(payload) if kind == STORAGE else acceptance_rows(payload)


# --- сбор --------------------------------------------------------------------


@dataclass(frozen=True)
class Collected:
    """Чем закончился сбор. `reason` объясняет, чего в базе не прибавилось."""

    kind: str = STORAGE
    days: int = 0
    articles: int = 0
    amount: Decimal = ZERO
    reason: str = OK

    @property
    def ok(self) -> bool:
        return self.reason == OK


def save(
    client_id: int,
    kind: str,
    date_from: date,
    date_to: date,
    payload: Iterable[Mapping[str, Any]],
    *,
    path: str | Path | None = None,
) -> Collected:
    """Кладёт окно отчёта в базу, переписывая его целиком.

    Окно переписывается, а не дополняется, и это не осторожность, а
    требование: у Wildberries бывают перерасчёты задним числом, после них
    сутки выглядят иначе, и дополнение сложило бы прежнюю сумму с новой.
    Ответ Wildberries за окно и есть вся правда об этом окне.

    Сутки окна отмечаются все, включая те, в которых расхода не было: иначе
    «не было» и «не собирали» в базе выглядят одинаково.
    """
    repo = db.repo(client_id, path)
    first, last = date_from.isoformat(), date_to.isoformat()
    stamp = _now()
    rows = list(payload or [])
    # Сутки вне запрошенного окна отбрасываются сразу, а не при записи:
    # окно, которое их переписало бы, никто не запрашивал, и такая строка
    # осталась бы в базе навсегда. Заодно и в ответе этой функции стоит ровно
    # то, что в базу легло.
    folded = {
        key: values
        for key, values in parse_rows(rows, kind).items()
        if first <= key[0] <= last
    }
    totals = {
        day: values
        for day, values in day_totals(rows, kind=kind).items()
        if first <= day <= last
    }

    repo.delete_between("expense_daily", "date", first, last, kind=kind)
    repo.delete_between("expense_days", "date", first, last, kind=kind)
    for (day, nm_id), values in folded.items():
        repo.upsert(
            "expense_daily",
            {"kind": kind, "date": day, "nm_id": nm_id},
            updated_at=stamp,
            **values,
        )
    cursor = date_from
    while cursor <= date_to:
        day = cursor.isoformat()
        values = totals.get(day) or {"amount_kop": 0, "rows": 0}
        repo.upsert(
            "expense_days", {"kind": kind, "date": day}, updated_at=stamp, **values
        )
        cursor += timedelta(days=1)
    return Collected(
        kind=kind,
        days=(date_to - date_from).days + 1,
        articles=len({nm_id for _, nm_id in folded}),
        amount=sum(
            (db.from_kop(values["amount_kop"]) for values in totals.values()), ZERO
        ),
    )


async def collect(
    client_id: int,
    kind: str,
    date_from: date,
    date_to: date,
    *,
    http: Any = None,
    path: str | Path | None = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], Any] | None = None,
) -> Collected:
    """Забирает одно окно отчёта и складывает его в базу. Зовётся из очереди.

    Отчёт с заданием целиком живёт в `core.wbapi`: три шага и ожидание
    готовности это разговор с Wildberries, а не дело агента. Повторов здесь
    нет, их делает очередь.

    Отсутствие категории «Аналитика» это штатное состояние кабинета, а не
    авария: тогда колонки по товарам не будет, а прибыль посчитается как
    раньше. Наружу летит только 401: его разбирает `core.clients`, и без
    этого модули не встанут на паузу.
    """
    client = wbapi.get_wb_client(client_id, http=http, path=path, clock=clock, sleep=sleep)
    try:
        rows = await getattr(client, FETCH[kind])(date_from, date_to)
    except wbapi.WBForbiddenError:
        logger.info(
            "у клиента %s нет категории «Аналитика», %s пропускаю", client_id, kind
        )
        return Collected(kind=kind, reason=NO_CATEGORY)
    except (wbapi.WBUnavailable, wbapi.WBRateLimited, wbapi.WBApiError) as error:
        logger.warning("%s клиента %s не получено: %s", kind, client_id, error)
        return Collected(kind=kind, reason=UNAVAILABLE)
    return save(client_id, kind, date_from, date_to, rows, path=path)


# --- чтение ------------------------------------------------------------------


@dataclass(frozen=True)
class Spend:
    """Расход по товарам за период и честный ответ, за сколько суток он есть.

    `available=False` это не «расхода не было», а «этих суток мы не
    собирали». Разница принципиальная: нулём мы приписали бы селлеру
    прибыль, которой у него может и не быть.

    `total` это сумма по товарам, `collected` это весь расход собранных
    суток, включая строки без артикула. Два числа рядом, а не одно: разницу
    между ними отчёт называет, а не прячет.
    """

    kind: str = STORAGE
    spend: Mapping[int, Decimal] = None  # type: ignore[assignment]
    available: bool = True
    reason: str = OK
    days: int = 0
    days_total: int = 0
    collected: Decimal = ZERO

    def __post_init__(self) -> None:
        object.__setattr__(self, "spend", dict(self.spend or {}))

    def of(self, nm_id: int | None) -> Decimal | None:
        """Расход по артикулу или None, если данных нет вовсе."""
        if not self.available:
            return None
        if nm_id is None:
            return ZERO
        return _money(self.spend.get(int(nm_id)))

    @property
    def total(self) -> Decimal:
        return sum((_money(value) for value in self.spend.values()), ZERO)

    @property
    def complete(self) -> bool:
        """Собраны все сутки периода: оговаривать нечего."""
        return self.available and self.days >= self.days_total > 0

    @property
    def source(self) -> str:
        return SOURCE_TITLES.get(self.kind, self.kind)

    @property
    def column(self) -> str:
        return COLUMN_TITLES.get(self.kind, self.kind)


def covered_days(
    client_id: int,
    kind: str,
    date_from: date,
    date_to: date,
    *,
    path: str | Path | None = None,
) -> set[str]:
    """Сутки периода, которые по этому отчёту уже собраны."""
    rows = db.repo(client_id, path).rows_between(
        "expense_days", "date", date_from.isoformat(), date_to.isoformat(), kind=kind
    )
    return {str(row["date"])[:10] for row in rows}


def missing_days(
    client_id: int,
    kind: str,
    date_from: date,
    date_to: date,
    *,
    path: str | Path | None = None,
) -> list[date]:
    """Сутки периода, которых по этому отчёту нет. По возрастанию даты."""
    known = covered_days(client_id, kind, date_from, date_to, path=path)
    found: list[date] = []
    cursor = date_from
    while cursor <= date_to:
        if cursor.isoformat() not in known:
            found.append(cursor)
        cursor += timedelta(days=1)
    return found


def spend_of(
    client_id: int,
    kind: str,
    date_from: date,
    date_to: date,
    *,
    path: str | Path | None = None,
) -> Spend:
    """Расход по товарам за период. Это чтение базы, в WB отсюда не ходят."""
    repo = db.repo(client_id, path)
    first, last = date_from.isoformat(), date_to.isoformat()
    spend: dict[int, Decimal] = {}
    for row in repo.rows_between("expense_daily", "date", first, last, kind=kind):
        nm_id = int(row["nm_id"])
        spend[nm_id] = spend.get(nm_id, ZERO) + db.from_kop(row["amount_kop"])
    days = repo.rows_between("expense_days", "date", first, last, kind=kind)
    total_days = (date_to - date_from).days + 1
    if not days:
        return Spend(
            kind=kind,
            spend={},
            available=False,
            reason=NOT_COLLECTED,
            days=0,
            days_total=total_days,
        )
    return Spend(
        kind=kind,
        spend=spend,
        available=True,
        reason=OK,
        days=len({str(row["date"])[:10] for row in days}),
        days_total=total_days,
        collected=sum((db.from_kop(row["amount_kop"]) for row in days), ZERO),
    )


# --- очередь и расписание ----------------------------------------------------


def _payload_date(task: Any, field: str = "date") -> date:
    raw = str((getattr(task, "payload", None) or {}).get(field) or "")
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return datetime.now(scheduler.tz()).date()


def _payload_window(task: Any) -> tuple[date, date] | None:
    """Окно из задачи, если его назвали. Иначе ночной сбор решает сам."""
    payload = getattr(task, "payload", None) or {}
    if not payload.get("from") or not payload.get("to"):
        return None
    try:
        first = date.fromisoformat(str(payload["from"])[:10])
        last = date.fromisoformat(str(payload["to"])[:10])
    except ValueError:
        return None
    return (first, last) if first <= last else (last, first)


def _payload_kinds(task: Any) -> tuple[str, ...]:
    """Какие источники собирать. По умолчанию оба."""
    raw = (getattr(task, "payload", None) or {}).get("kind")
    if not raw:
        return KINDS
    wanted = tuple(name for name in KINDS if name == str(raw))
    return wanted or KINDS


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


def _floor(client_id: int, today: date, *, path: str | Path | None = None) -> date:
    """Глубже этой даты история не нужна.

    Две границы, и берётся поздняя. Первая это срок хранения суточной истории:
    глубже её чистка всё равно удалит собранное. Вторая это самая ранняя
    выгруженная неделя отчёта о реализации: прибыль считается по этим неделям,
    и хранение за сутки, которых в деньгах нет, сравнивать не с чем.

    Недель нет вовсе - истории не нужно никакой: кабинет подключён, а денег за
    него бот ещё не выгружал, и собирать хранение за прошлый год значило бы
    жечь лимит клиента впустую.
    """
    days = history_days()
    edge = today - timedelta(days=days - 1) if days > 0 else today
    earliest = db.repo(client_id, path).first_value("fin_weeks", "date_from")
    if not earliest:
        return today
    try:
        return max(edge, date.fromisoformat(str(earliest)[:10]))
    except ValueError:
        return edge


def windows_for(kind: str, date_from: date, date_to: date) -> list[tuple[date, date]]:
    """Период окнами, которые Wildberries принимает за один раз.

    Длину окна знает `core.wbapi`: это факт про WB, и своего числа здесь нет.
    """
    return wbapi.date_windows(date_from, date_to, WINDOW_DAYS[kind])


def _backfill_windows(
    client_id: int,
    kind: str,
    today: date,
    fresh_start: date,
    *,
    path: str | Path | None = None,
) -> list[tuple[date, date]]:
    """Окна истории, которые стоит догнать этой ночью, от свежих к старым.

    От свежих, а не от старых: селлер чаще спрашивает недавние периоды, и
    дотягивать историю в ту же сторону, в какую он смотрит, полезнее.
    """
    limit = backfill_windows()
    if limit <= 0:
        return []
    floor = _floor(client_id, today, path=path)
    last = fresh_start - timedelta(days=1)
    if last < floor:
        return []
    gaps = missing_days(client_id, kind, floor, last, path=path)
    if not gaps:
        return []
    chosen: list[tuple[date, date]] = []
    step = WINDOW_DAYS[kind]
    pending = set(gaps)
    cursor = max(gaps)
    while pending and len(chosen) < limit and cursor >= floor:
        start = max(floor, cursor - timedelta(days=step - 1))
        chosen.append((start, cursor))
        pending -= {day for day in pending if start <= day <= cursor}
        if not pending:
            break
        cursor = max(pending)
    return chosen


def request_collect(
    client_id: int,
    date_from: date,
    date_to: date,
    *,
    kind: str = STORAGE,
    path: str | Path | None = None,
) -> Any:
    """Ставит сбор названного окна в очередь. Тихо: клиент его не заказывал.

    Нужно отчёту о прибыли: селлер спросил период, которого в истории нет, и
    собирать его прямо в отчёте нельзя. Окно одного задания это две минуты по
    дорожке «1 запрос в минуту», а год это 46 окон, то есть больше часа, и всё
    это время у кабинета занято его единственное место в очереди. Поэтому
    отчёт уходит с тем, что есть, говорит об этом вслух, а дыру начинает
    закрывать отдельная работа. Повтор той же просьбы ничего не удваивает:
    одинаковую задачу очередь не ставит дважды.
    """
    return queue.enqueue(
        client_id,
        COLLECT_ONE,
        {
            "kind": kind,
            "from": date_from.isoformat(),
            "to": date_to.isoformat(),
        },
        notify=False,
        path=path,
    )


def fan_out_collect(task: Any, *, path: str | Path | None = None) -> list[Any]:
    """Ночной сбор: по задаче на каждый подключённый кабинет.

    **Доступ к модулю здесь не проверяется, и это не упущение.** Окно одного
    задания восемь суток, историю задним числом догонять дорого, а подписку
    клиент может оформить и через месяц.
    """
    stamp = _payload_date(task).isoformat()
    return [
        queue.enqueue(client_id, COLLECT_ONE, {"date": stamp}, notify=False, path=path)
        for client_id in _connected_clients(path)
    ]


async def collect_client(task: Any, *, path: str | Path | None = None) -> list[Collected]:
    """Сбор одного кабинета. Либо названное окно, либо ночная работа.

    Ночная работа это свежее окно (его сутки ещё меняются перерасчётами) плюс
    несколько окон истории вглубь. Свежее окно идёт первым: история терпит, а
    вчерашние деньги нужны отчёту сегодня.
    """
    client_id = int(task.client_id)
    kinds = _payload_kinds(task)
    named = _payload_window(task)
    done: list[Collected] = []
    if named is not None:
        for kind in kinds:
            for window in windows_for(kind, named[0], named[1]):
                done.append(await collect(client_id, kind, window[0], window[1], path=path))
        return done
    today = _payload_date(task)
    fresh_start = today - timedelta(days=collect_days() - 1)
    for kind in kinds:
        done.append(await collect(client_id, kind, fresh_start, today, path=path))
        for window in _backfill_windows(
            client_id, kind, today, fresh_start, path=path
        ):
            done.append(await collect(client_id, kind, window[0], window[1], path=path))
    return done


CLEANED_TABLES = ("expense_daily", "expense_days")


def cleanup(task: Any = None, *, path: str | Path | None = None) -> int:
    """Убирает суточную историю глубже срока хранения. Возвращает число строк.

    В Wildberries отсюда не ходят: это работа по базе. Срок тот же, что у
    рекламы и воронки: история копится у всех подключённых кабинетов каждый
    день, и без срока таблицы растут бесконечно.
    """
    days = history_days()
    if days <= 0:
        return 0
    edge = (_payload_date(task) - timedelta(days=days)).isoformat()
    removed = 0
    for row in db.admin_repo(path).all_clients():
        client_id = int(row["id"])
        try:
            repo = db.repo(client_id, path)
            for table in CLEANED_TABLES:
                removed += repo.delete_before(table, "date", edge)
        except Exception:  # noqa: BLE001 - один клиент не ломает обход
            logger.exception("не удалось почистить историю расходов клиента %s", client_id)
    return removed


def register_jobs() -> None:
    """Ставит ночной сбор в расписание, а сбор кабинета в очередь.

    Зовёт её `register(app)` хендлера `/profit`: своей команды у этого агента
    нет, его данные это колонки в книге прибыльности.
    """
    scheduler.register_daily(COLLECT_ALL, fan_out_collect)
    # Чистка идёт тем же утром и в WB не ходит: это работа по базе.
    scheduler.register_daily(CLEANUP, cleanup)
    # Сбор идёт ночью и клиентом не заказан: о его сбое знает владелец.
    queue.register(COLLECT_ONE, collect_client, quiet=True)
