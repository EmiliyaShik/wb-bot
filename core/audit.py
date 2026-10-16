"""Журнал событий: что происходило, без секретов.

В журнал не попадают токены, ключи и банковские реквизиты. Но артикул товара
и Telegram ID остаются: ради них журнал и читают, и вырезать их означало бы
сделать его бесполезным. Поэтому чистка адресная, а не «любое длинное число».

Отдельно живёт место ошибки. Одной строки текста при сбое задачи не хватает:
задача падала с «неизвестный период: dates», а узнать, кто её поставил и на
какой строке она развалилась, было негде, и та же задача потом прошла целиком.
Поэтому у сбоя остаётся стек вызовов, и он записывается своим видом события
(`TRACE_KIND`), а не дописывается к человеческой строке. Причин две. Стек
нужен тому, кто разбирает, а не владельцу: `/tasks` показывает ему последние
ошибки журнала, и стеку там не место, поэтому этот вид оттуда просто
исключается. И стек проходит ту же чистку, что всё остальное: в кадрах лежат
значения переменных и куски запросов, то есть ровно то, где секрет и
встречается.
"""

from __future__ import annotations

import logging
import re
import traceback
from pathlib import Path
from typing import Any

from core import config, db

logger = logging.getLogger("rentgen.audit")

HIDDEN = "[скрыто]"

# Вид события, под которым лежит место ошибки. Отдельный вид, а не уровень:
# уровень у стека тот же, что у самого сбоя, иначе ошибку пришлось бы искать
# в журнале дважды.
TRACE_KIND = "trace"

# Чем помечено, что от стека оставили только конец.
TRACE_CUT = "[начало стека вызовов отброшено]"

# Насколько длинной бывает одна строка стека. Кадр («File ..., line ..., in
# ...» и сама строка кода) в эти знаки укладывается с запасом, а не
# укладывается последняя строка, где стоит текст ошибки: ответ Wildberries или
# разобранный payload бывают в тысячи знаков. Такую строку режем отдельно,
# потому что иначе она вытеснила бы из стека все кадры: текст ошибки стоит в
# конце, а конец мы и оставляем. Сам текст ошибки при этом не теряется, он в
# человеческой строке события и в `tasks.last_error`.
TRACE_LINE_CHARS = 300

# Чем помечено, что от длинной строки оставили начало.
LINE_CUT = " [...]"

# Токены и ключи: их в журнале быть не должно ни в каком виде.
_TOKEN_PATTERNS = (
    # JWT: три части через точку, начинается с eyJ
    re.compile(r"\beyJ[A-Za-z0-9_\-]{5,}(?:\.[A-Za-z0-9_\-]*){1,3}"),
    # длинная непрерывная строка без пробелов похожа на ключ или токен
    re.compile(r"\b[A-Za-z0-9_\-]{40,}\b"),
)

# Реквизиты, которые однозначны по длине: ОГРНИП это 15 цифр,
# расчётный и корреспондентский счёт - 20. Артикул WB короче (4-12),
# Telegram ID тоже, поэтому они под это правило не попадают.
_LONG_NUMBER = re.compile(r"\b\d{15,}\b")

# Остальное узнаём по слову рядом: ИНН и БИК по длине не отличить от артикула.
_LABELLED_NUMBER = re.compile(
    r"(?i)\b(ИНН|КПП|БИК|ОГРНИП|ОГРН|счёт|счет|р/с|к/с|карта|карты)"
    r"(\s*(?:N|№|:)?\s*)(\d{8,20})"
)

_SECRET_ENV = (
    "TELEGRAM_BOT_TOKEN",
    "ENCRYPTION_KEY",
    "DADATA_API_KEY",
    "WB_PROXY",
    "SELLER_INN",
    "SELLER_OGRNIP",
    "SELLER_ACCOUNT",
    "SELLER_BIK",
    "SELLER_CORR_ACCOUNT",
)


def redact(text: str) -> str:
    """Прячет токены, ключи и реквизиты, оставляя артикулы и Telegram ID."""
    cleaned = str(text)
    for name in _SECRET_ENV:
        value = config.env(name)
        if value and len(value) >= 4:
            cleaned = cleaned.replace(value, HIDDEN)
    for pattern in _TOKEN_PATTERNS:
        cleaned = pattern.sub(HIDDEN, cleaned)
    cleaned = _LABELLED_NUMBER.sub(lambda m: f"{m.group(1)}{m.group(2)}{HIDDEN}", cleaned)
    return _LONG_NUMBER.sub(HIDDEN, cleaned)


def trace_max_chars() -> int:
    """Предел длины места ошибки в журнале. Из конфига, секция audit."""
    try:
        return int(config.settings().get("audit", {}).get("trace_max_chars", 2000))
    except Exception:  # noqa: BLE001 - конфиг не должен ронять запись в журнал
        return 2000


def _short_line(line: str) -> str:
    """Одна строка стека не длиннее `TRACE_LINE_CHARS`."""
    if len(line) <= TRACE_LINE_CHARS:
        return line
    return line[: TRACE_LINE_CHARS - len(LINE_CUT)] + LINE_CUT


def place_of(error: BaseException | None, limit: int | None = None) -> str:
    """Место ошибки: стек вызовов, вычищенный от секретов и укороченный.

    Режется начало, а не конец: упало в последних кадрах, и именно они нужны
    тому, кто разбирает. Отброшенное начало помечается, чтобы читающий не
    принял обрезок за полный стек.

    Чистка та же, что у любой записи журнала, и она здесь обязательна: в
    кадрах стека лежат значения переменных и куски запросов, а значит и
    токен, если он случился рядом.
    """
    if error is None:
        return ""
    cap = trace_max_chars() if limit is None else int(limit)
    if cap <= 0:
        return ""
    raw = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    ).strip()
    safe = redact(raw)
    safe = "\n".join(_short_line(line) for line in safe.splitlines())
    if len(safe) <= cap:
        return safe
    # Метка входит в предел: иначе «не длиннее cap» перестало бы быть правдой
    # ровно в том случае, ради которого предел и заводили.
    tail = cap - len(TRACE_CUT) - 1
    if tail <= 0:
        return TRACE_CUT[:cap]
    return TRACE_CUT + "\n" + safe[-tail:]


def _write(
    kind: str,
    client_id: int | None,
    safe: str,
    level: str,
    path: str | Path | None,
) -> None:
    """Запись в базу. Ошибка записи не должна ронять бота.

    Событие клиента идёт через repo(client_id), общее - через именованный
    метод общего слоя. Записи мимо client_id в слое данных не существует.
    """
    try:
        if client_id is None:
            db.admin_repo(path).add_event(kind=kind, message=safe, level=level)
        else:
            db.repo(client_id, path).insert(
                "events", level=level, kind=kind, message=safe
            )
    except Exception:  # журнал не имеет права остановить работу бота
        logger.exception("не удалось записать событие в базу")


def log(
    kind: str,
    client_id: int | None,
    message: str,
    level: str = "info",
    path: str | Path | None = None,
    error: BaseException | None = None,
) -> None:
    """Записывает событие в журнал. Ошибка записи не должна ронять бота.

    `error` это сам пойманный сбой, и он добавляет к событию второе: место
    ошибки отдельной записью вида `TRACE_KIND`. Зовут с ним только там, где
    упал наш код непредвиденно: отказ Wildberries или непринятый ключ место
    своё и так называют, а стек на каждую такую ошибку раздул бы журнал.
    """
    safe = redact(message)
    level_no = {"error": logging.ERROR, "warning": logging.WARNING}.get(
        level, logging.INFO
    )
    logger.log(
        level_no,
        "%s | клиент %s | %s",
        kind,
        client_id if client_id is not None else "нет",
        safe,
    )
    _write(kind, client_id, safe, level, path)
    if error is None:
        return
    place = place_of(error)
    if not place:
        return
    # Место ошибки идёт и в журнал базы, и в вывод процесса: на хостинге
    # разбирают по второму, а он до этой правки стека не видел вовсе.
    #
    # Ссылка на само событие укорачивается до одной строки, и это не
    # придирка: в человеческой строке сбоя стоит текст ошибки целиком, а он
    # бывает в тысячи знаков (ответ Wildberries, разобранный payload). Иначе
    # предел на стек ничего не ограничивал бы: распухла бы запись рядом.
    # Полный текст ошибки при этом не теряется, он в самом событии.
    body = _short_line(f"{kind}: {safe}") + "\n" + place
    logger.log(level_no, "%s | клиент %s | %s", TRACE_KIND, client_id, body)
    _write(TRACE_KIND, client_id, body, level, path)


def recent(
    limit: int = 50,
    level: str | None = None,
    client_id: int | None = None,
    path: str | Path | None = None,
    exclude_kind: str | None = None,
) -> list[Any]:
    """Последние записи журнала, свежие сверху.

    `exclude_kind` нужен владельцу: место ошибки лежит в журнале своим видом
    и читать его в чате незачем, а срез по последним записям без исключения
    отдал бы стеки вместо самих ошибок.
    """
    if client_id is not None:
        conditions: dict[str, Any] = {"level": level} if level else {}
        rows = db.repo(client_id, path).rows(
            "events", order_by="id DESC", limit=limit, **conditions
        )
        if not exclude_kind:
            return rows
        return [row for row in rows if row["kind"] != exclude_kind]
    return db.admin_repo(path).events(
        limit=limit, level=level, exclude_kind=exclude_kind
    )
