"""Себестоимость артикулов: шаблон для селлера, приём файла, хранение.

Как это работает по кругу:

1. `/costs` ставит задачу `costs_template`. Очередь сама обещает клиенту
   прислать результат, а обработчик идёт в WB за карточками. Прямого
   похода в WB из хендлера нет: повтор при недоступности живёт только в
   очереди;
2. шаблон уходит клиенту уже с его артикулами и с той себестоимостью,
   которую он давал раньше, чтобы не вбивать всё заново;
3. клиент присылает книгу обратно, `save_upload` проверяет и сохраняет.

Деньги хранятся целыми копейками (`costs.cost_per_unit_kop`) и поднимаются
в `Decimal` через `core.db.from_kop`. `float` в расчётах денег не участвует
нигде, даже промежуточно.

Приём файла это поверхность для атаки, поэтому у неё три двери подряд:
размер до разбора, формат до открытия, содержимое до сохранения. Чужая
начинка с расширением `.xlsx` даёт `BadFile` с объяснением, а не падение.

Одна кривая строка не отменяет файл: хорошие строки сохраняются, по плохим
возвращается список с номерами строк, как их видит человек в Excel.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from core import audit, config, db, queue, wbapi, xlsx

__all__ = [
    "TASK_KIND",
    "TEMPLATE_SHEET",
    "TEMPLATE_HEADERS",
    "COL_NM",
    "COL_VENDOR",
    "COL_TITLE",
    "COL_COST",
    "BadFile",
    "Problem",
    "Upload",
    "template_bytes",
    "build_template",
    "parse_upload",
    "save_upload",
    "save_costs",
    "costs_for",
    "missing_costs",
    "max_upload_bytes",
    "mb",
    "template_name",
    "set_sender",
    "request_template",
    "template_task",
]

# Вид фоновой задачи. Регистрируется в одном явном месте, при сборке бота:
# `bot.handlers.costs.register(app)`. Импорт модуля сам по себе ничего в
# очередь не прописывает, иначе порядок загрузки решал бы за нас.
TASK_KIND = "costs_template"

TEMPLATE_SHEET = "Себестоимость"

COL_NM = "nmID"
COL_VENDOR = "Артикул продавца"
COL_TITLE = "Название"
COL_COST = "Себестоимость за единицу, ₽"

TEMPLATE_HEADERS = (COL_NM, COL_VENDOR, COL_TITLE, COL_COST)

# Синонимы колонок: селлер переставляет колонки, меняет регистр и падежи,
# и это не повод отвергать файл.
ALIASES: dict[str, tuple[str, ...]] = {
    COL_NM: ("nmid", "nm id", "nm_id", "артикул wb", "артикул вб", "код номенклатуры"),
    COL_VENDOR: ("артикул продавца", "vendorcode", "vendor code", "артикул"),
    COL_TITLE: ("название", "наименование", "title", "товар"),
    COL_COST: (
        "себестоимость за единицу, ₽",
        "себестоимость за единицу",
        "себестоимость",
        "себестоимость, ₽",
        "себестоимость за штуку",
        "cost",
    ),
}

# Столько карточек за один запрос к WB. Лимит метода самый щедрый из всех,
# но страница всё равно ограничена сотней.
CARDS_PAGE = 100

DEFAULT_MAX_UPLOAD_MB = 5

_ZERO = Decimal("0")
# Копейка это самая мелкая единица, в которой живёт база. Доли копейки не
# округляются молча: селлер должен узнать, что его число изменили.
KOPECK = Decimal("0.01")


class BadFile(Exception):
    """Файл целиком не годится: размер, формат или отсутствующая колонка.

    Текст исключения написан для селлера, его можно показывать как есть.
    """


@dataclass(frozen=True)
class Problem:
    """Одна плохая строка: её номер в листе и человеческая причина."""

    row: int
    reason: str


@dataclass(frozen=True)
class Upload:
    """Итог разбора книги: что принято, что пропущено и почему."""

    values: dict[int, Decimal] = field(default_factory=dict)
    problems: tuple[Problem, ...] = ()
    blank: int = 0
    saved: int = 0

    @property
    def total(self) -> int:
        """Сколько строк с данными было в файле."""
        return len(self.values) + self.blank + len(self.problems)

    @property
    def skipped(self) -> int:
        """Пустые плюс кривые: всё, что не доехало до базы."""
        return self.blank + len(self.problems)

    @property
    def ok(self) -> bool:
        return not self.problems and self.blank == 0


# --- шаблон ---


def template_name() -> str:
    """Имя файла, которое увидит селлер в Telegram."""
    return "себестоимость.xlsx"


def template_bytes(
    cards: Iterable[Mapping[str, Any]], existing: Mapping[int, Decimal] | None = None
) -> bytes:
    """Книга по карточкам WB. Чистая функция, в WB и в базу не ходит.

    `existing` подставляется в колонку себестоимости, чтобы селлер не вбивал
    заново то, что уже давал.
    """
    known = dict(existing or {})
    rows: list[list[Any]] = []
    for item in cards:
        nm_id = _to_int(item.get("nmID", item.get("nmId")))
        if nm_id is None:
            continue
        rows.append(
            [
                nm_id,
                str(item.get("vendorCode") or ""),
                str(item.get("title") or item.get("subjectName") or ""),
                known.get(nm_id),
            ]
        )
    sheet = xlsx.Sheet(
        TEMPLATE_SHEET, list(TEMPLATE_HEADERS), rows, widths=[12, 22, 46, 26]
    )
    return xlsx.write_book(sheet)


async def build_template(
    client_id: int,
    *,
    path: str | Path | None = None,
    http: Any = None,
    limit: int = CARDS_PAGE,
) -> bytes:
    """Карточки из WB плюс уже известная себестоимость, обе в одну книгу.

    Зовётся из обработчика очереди, а не из хендлера: при недоступности WB
    повторить попытку должна очередь.
    """
    client = wbapi.get_wb_client(client_id, http=http, path=path)
    cards = await client.cards_list(limit=limit)
    return template_bytes(cards, costs_for(client_id, path=path))


# --- приём файла ---


def max_upload_bytes() -> int:
    """Потолок размера присланного файла, из секции `[limits]` конфига."""
    limits = config.settings().get("limits", {}) or {}
    megabytes = limits.get("upload_max_mb", DEFAULT_MAX_UPLOAD_MB)
    try:
        value = float(megabytes)
    except (TypeError, ValueError):
        value = DEFAULT_MAX_UPLOAD_MB
    return int(max(value, 0.1) * 1024 * 1024)


def parse_upload(
    data: bytes,
    *,
    max_bytes: int | None = None,
    max_rows: int | None = None,
    max_cells: int | None = None,
) -> Upload:
    """Разбирает книгу селлера. В базу не пишет, `client_id` ей не нужен.

    Поднимает `BadFile`, если не годится файл целиком. Кривые строки в базу
    не едут, но и не отменяют остальные: они возвращаются в `problems`.
    """
    limit = max_upload_bytes() if max_bytes is None else int(max_bytes)
    try:
        sheet = _sheet_with_costs(data, limit, max_rows, max_cells)
    except xlsx.TooLargeError as error:
        raise BadFile(
            "Файл больше разрешённого размера: "
            f"{mb(error.size)} при лимите {mb(error.limit)}. "
            "Пришлите только лист с себестоимостью."
        ) from error
    except xlsx.TooManyRowsError as error:
        raise BadFile(
            f"В файле слишком много строк, больше {error.limit}. "
            "Пришлите один лист с артикулами и себестоимостью, без лишнего."
        ) from error
    except xlsx.TooManyCellsError as error:
        raise BadFile(
            f"В файле слишком много ячеек, больше {error.limit}. "
            "Пришлите один лист с артикулами и себестоимостью, без лишнего."
        ) from error
    except xlsx.NotXlsxError as error:
        raise BadFile(
            "Это не файл xlsx. Принимаю только книгу Excel: откройте шаблон, "
            "заполните колонку с себестоимостью и сохраните как xlsx."
        ) from error

    column_nm = sheet.column(*ALIASES[COL_NM])
    column_cost = sheet.column(*ALIASES[COL_COST])
    if column_nm is None or column_cost is None:
        missing = []
        if column_nm is None:
            missing.append(f"«{COL_NM}»")
        if column_cost is None:
            missing.append(f"«{COL_COST}»")
        raise BadFile(
            "В файле нет нужных колонок: "
            + " и ".join(missing)
            + ". Проще всего запросить шаблон командой /costs и заполнить его."
        )

    if not sheet.rows:
        raise BadFile(
            "В файле нет ни одной строки с данными. Запросите шаблон "
            "командой /costs и заполните колонку с себестоимостью."
        )

    values: dict[int, Decimal] = {}
    problems: list[Problem] = []
    blank = 0
    for row in sheet.rows:
        raw_cost = row.get(column_cost)
        if _is_blank(raw_cost):
            blank += 1
            continue
        nm_id = _to_int(row.get(column_nm))
        if nm_id is None or nm_id <= 0:
            problems.append(Problem(row.number, f"{COL_NM} не число"))
            continue
        cost = _to_money(raw_cost)
        if cost is None:
            problems.append(Problem(row.number, "себестоимость не число"))
            continue
        if cost < _ZERO:
            problems.append(Problem(row.number, "себестоимость отрицательная"))
            continue
        if cost != cost.quantize(KOPECK):
            problems.append(
                Problem(row.number, "себестоимость точнее копейки, округлите сами")
            )
            continue
        if nm_id in values:
            problems.append(Problem(row.number, f"{COL_NM} {nm_id} уже был выше"))
            continue
        values[nm_id] = cost

    return Upload(values=values, problems=tuple(problems), blank=blank)


def save_upload(
    client_id: int,
    data: bytes,
    *,
    max_bytes: int | None = None,
    max_rows: int | None = None,
    max_cells: int | None = None,
    path: str | Path | None = None,
) -> Upload:
    """Разбирает книгу и сохраняет хорошие строки. Возвращает тот же итог."""
    parsed = parse_upload(
        data, max_bytes=max_bytes, max_rows=max_rows, max_cells=max_cells
    )
    saved = save_costs(client_id, parsed.values, path=path)
    audit.log(
        "costs",
        client_id,
        f"Себестоимость из файла: принято {saved}, пропущено {parsed.skipped}.",
    )
    return Upload(
        values=parsed.values,
        problems=parsed.problems,
        blank=parsed.blank,
        saved=saved,
    )


def _sheet_with_costs(
    data: bytes, limit: int, max_rows: int | None, max_cells: int | None
) -> xlsx.SheetData:
    """Лист, на котором есть колонка себестоимости, иначе первый."""
    book = xlsx.read_book(
        data, max_bytes=limit, max_rows=max_rows, max_cells=max_cells
    )
    for sheet in book:
        if sheet.column(*ALIASES[COL_COST]) is not None:
            return sheet
    return book.first


# --- хранение ---


def save_costs(
    client_id: int,
    values: Mapping[int, Decimal | int | str],
    *,
    path: str | Path | None = None,
) -> int:
    """Пишет себестоимость целыми копейками. Повтор обновляет, а не двоит."""
    repo = db.repo(client_id, path)
    written = 0
    for nm_id, amount in values.items():
        money = amount if isinstance(amount, Decimal) else Decimal(str(amount))
        repo.upsert(
            "costs",
            {"nm_id": int(nm_id)},
            cost_per_unit_kop=db.to_kop(money),
            updated_at=_now(),
        )
        written += 1
    return written


def costs_for(
    client_id: int,
    *,
    nm_ids: Sequence[int] | None = None,
    path: str | Path | None = None,
) -> dict[int, Decimal]:
    """Себестоимость клиента в рублях: `{nmId: Decimal}`.

    Без `nm_ids` отдаёт всё, что есть. Артикулы без себестоимости просто
    отсутствуют в ответе: отчёт покажет их в блоке «нет себестоимости».
    """
    wanted = {int(nm_id) for nm_id in nm_ids} if nm_ids is not None else None
    found: dict[int, Decimal] = {}
    for row in db.repo(client_id, path).rows("costs"):
        nm_id = int(row["nm_id"])
        if wanted is not None and nm_id not in wanted:
            continue
        found[nm_id] = db.from_kop(row["cost_per_unit_kop"])
    return found


def missing_costs(
    client_id: int, nm_ids: Iterable[int], *, path: str | Path | None = None
) -> list[int]:
    """Какие из артикулов остались без себестоимости. Порядок сохраняется."""
    known = costs_for(client_id, path=path)
    seen: set[int] = set()
    absent: list[int] = []
    for nm_id in nm_ids:
        value = int(nm_id)
        if value in known or value in seen:
            continue
        seen.add(value)
        absent.append(value)
    return absent


# --- задача очереди ---

# Чем отправить готовую книгу клиенту. Ставит `bot.handlers.costs`: ядро не
# знает про Telegram, а очередь умеет только текст.
_sender: Callable[[int, str, bytes], Any] | None = None


def set_sender(fn: Callable[[int, str, bytes], Any] | None) -> None:
    """Задаёт доставку файла: `fn(client_id, filename, data)`."""
    global _sender
    _sender = fn


def request_template(client_id: int, *, path: str | Path | None = None) -> int:
    """Ставит сборку шаблона в очередь. Обещание клиенту даёт сама очередь."""
    return queue.enqueue(client_id, TASK_KIND, path=path)


async def template_task(task: Any) -> None:
    """Обработчик задачи: сходить в WB, собрать книгу, отдать клиенту."""
    client_id = int(task.client_id)
    data = await build_template(client_id)
    if _sender is None:
        raise RuntimeError("некому отправить шаблон: доставка не подключена")
    result = _sender(client_id, template_name(), data)
    if hasattr(result, "__await__"):
        await result


# --- мелочи ---


def _now() -> str:
    """Отметка времени в UTC, как и всё остальное в базе.

    Московское время это то, в чём показывают селлеру, а не то, в чём
    хранят: иначе сравнение с соседними таблицами даст три часа разницы.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _is_blank(value: Any) -> bool:
    return value is None or str(value).strip() == ""


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if float(value).is_integer() else None
    text = str(value).strip().replace(" ", "").replace(" ", "")
    if text.endswith(".0"):
        text = text[:-2]
    try:
        return int(text)
    except ValueError:
        return None


def _to_money(value: Any) -> Decimal | None:
    """Число из ячейки в рубли. Запятая, пробелы и знак рубля допустимы."""
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # Из книги приходит float, но дальше он живёт только как Decimal.
        return Decimal(str(value))
    text = str(value).strip()
    for junk in (" ", " ", "₽", "руб.", "руб", "р."):
        text = text.replace(junk, "")
    text = text.replace(",", ".")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def mb(size: int) -> str:
    """Размер файла словами для селлера. Общий на ядро и на хендлер."""
    return f"{size / (1024 * 1024):.1f} МБ"
