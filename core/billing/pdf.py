"""PDF счёта на reportlab.

Модуль разделён надвое намеренно. `content()` собирает то, что в счёте
написано, и это обычный словарь: его видно в тестах, и по нему понятно, что
именно уходит клиенту. `build()` только рисует этот словарь на листе. Так
проверка содержимого не зависит от разбора PDF, а вёрстку можно менять, не
переписывая тесты.

Ни одного реквизита и ни одной формулировки в этом файле нет. Всё приходит из
переменных окружения и конфига, а если чего-то нет, на его месте остаётся
пустое место: пустое исправят, выдуманное отгрузят.

Шрифт. Кириллицы во встроенных шрифтах PDF нет, нужен TTF с диска. Он ищется
среди системных, а не лежит в репозитории: тащить в публичный репозиторий
750 килобайт чужого шрифта ради счёта, который выставляется раз в месяц,
не стоит. Не нашли ни одного - счёт не формируется, и владелец получает
точное указание, что поставить на сервере. Это та же честность, что и с
пустыми реквизитами: молча нарисовать вместо букв квадратики хуже, чем
сказать правду.
"""

from __future__ import annotations

import io
import logging
from decimal import Decimal
from pathlib import Path
from typing import Any

from core import config
from core.billing import (
    DetailsMissing,
    Invoice,
    local_day,
    missing_details,
    months_words,
    service_line,
    vat_note,
)

logger = logging.getLogger(__name__)

FONT_NAME = "WBR"
FONT_BOLD = "WBR-Bold"

# Где искать шрифт с кириллицей. Порядок от самого вероятного к запасным.
FONT_CANDIDATES = (
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    ("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf", "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"),
    ("C:/Windows/Fonts/DejaVuSans.ttf", "C:/Windows/Fonts/DejaVuSans-Bold.ttf"),
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
    ("C:/Windows/Fonts/tahoma.ttf", "C:/Windows/Fonts/tahomabd.ttf"),
    ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
)

FONT_HINT = (
    "На сервере нет шрифта с кириллицей, счёт нарисовать нечем. "
    "Установите пакет шрифтов, например fonts-dejavu-core."
)

# Подписи полей счёта. Это не реквизиты, а названия строк бланка.
SELLER_LABELS = {
    "SELLER_NAME": "Получатель",
    "SELLER_INN": "ИНН",
    "SELLER_OGRNIP": "ОГРНИП",
    "SELLER_ADDRESS": "Адрес",
    "SELLER_ACCOUNT": "Расчётный счёт",
    "SELLER_BANK": "Банк",
    "SELLER_BIK": "БИК",
    "SELLER_CORR_ACCOUNT": "Корреспондентский счёт",
}

# Порядок полей на бланке.
SELLER_ORDER = (
    "SELLER_NAME",
    "SELLER_INN",
    "SELLER_OGRNIP",
    "SELLER_ADDRESS",
    "SELLER_BANK",
    "SELLER_BIK",
    "SELLER_ACCOUNT",
    "SELLER_CORR_ACCOUNT",
)


class FontMissing(Exception):
    """Шрифта с кириллицей на машине нет, рисовать счёт нечем."""


def _has_cyrillic(path: str) -> bool:
    """Умеет ли шрифт писать по-русски. Проверяется по таблице символов."""
    try:
        from reportlab.pdfbase.ttfonts import TTFont

        face = TTFont("проба", path).face
        return all(ord(ch) in face.charToGlyph for ch in "АЯаяёЁ")
    except Exception:  # noqa: BLE001 - битый файл шрифта это просто не наш шрифт
        logger.debug("шрифт %s не подошёл", path, exc_info=True)
        return False


def find_font() -> tuple[str, str]:
    """Пара путей (обычный, жирный). Жирного нет - вернётся обычный дважды."""
    for regular, bold in FONT_CANDIDATES:
        if Path(regular).exists() and _has_cyrillic(regular):
            return regular, (bold if Path(bold).exists() else regular)
    raise FontMissing(FONT_HINT)


def _register_fonts() -> tuple[str, str]:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    if FONT_NAME in pdfmetrics.getRegisteredFontNames():
        return FONT_NAME, FONT_BOLD
    regular, bold = find_font()
    pdfmetrics.registerFont(TTFont(FONT_NAME, regular))
    pdfmetrics.registerFont(TTFont(FONT_BOLD, bold))
    return FONT_NAME, FONT_BOLD


def money(amount: Decimal | int) -> str:
    """Сумма словами бланка: рубли и копейки, без значка валюты.

    Значок рубля намеренно не используется: он есть не в каждом шрифте, и
    вместо него в счёте появился бы пустой квадрат.
    """
    value = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    whole = int(value)
    kop = int((value - whole) * 100)
    return f"{whole:,}".replace(",", " ") + f" руб. {kop:02d} коп."


def content(item: Invoice) -> dict[str, Any]:
    """Что написано в счёте. Словарь, а не PDF: его видно и проверяющему.

    Пустые поля остаются пустыми. Единственное место, где модуль что-то
    дописывает от себя, это подписи строк бланка.
    """
    details = config.seller_details()
    return {
        "number": item.number,
        "date": local_day(item.issued_at),
        "due": item.due_at.strftime("%d.%m.%Y") if item.due_at else "",
        "seller": details,
        "seller_lines": [
            (SELLER_LABELS[key], details.get(key, "")) for key in SELLER_ORDER
        ],
        "buyer_name": item.org_name,
        "buyer_inn": item.inn,
        "buyer_address": item.org_address,
        "rows": [
            {
                "name": service_line(item.module, item.period_months),
                "period": months_words(item.period_months),
                "amount": item.amount,
            }
        ],
        "total": item.amount,
        "purpose": item.payment_purpose(),
        "vat": vat_note(),
        "offer": config.env("OFFER_URL"),
    }


def file_name(item: Invoice) -> str:
    """Имя файла, которое увидит клиент."""
    return f"{item.number}.pdf"


def build(item: Invoice) -> bytes:
    """Рисует счёт. Без реквизитов и без шрифта не рисует ничего.

    Проверка реквизитов повторяется здесь, хотя `create_invoice` уже её
    сделала: PDF могут запросить повторно по старому счёту, когда переменные
    успели опустеть, и молча выдать бланк с дырами нельзя.
    """
    absent = missing_details()
    if absent:
        raise DetailsMissing(absent)

    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas as pdf_canvas

    regular, bold = _register_fonts()
    doc = content(item)

    buffer = io.BytesIO()
    page = pdf_canvas.Canvas(buffer, pagesize=A4)
    page.setTitle(doc["number"])
    width, height = A4
    left = 20 * mm
    right = width - 20 * mm
    y = height - 20 * mm

    def line(text: str, size: int = 10, font: str = regular, gap: float = 6 * mm) -> None:
        nonlocal y
        page.setFont(font, size)
        page.drawString(left, y, text)
        y -= gap

    line(f"Счёт № {doc['number']} от {doc['date']}", 15, bold, 10 * mm)

    line("Получатель платежа", 11, bold, 6 * mm)
    for label, value in doc["seller_lines"]:
        line(f"{label}: {value}", 9, regular, 5 * mm)

    y -= 3 * mm
    line("Плательщик", 11, bold, 6 * mm)
    line(f"Наименование: {doc['buyer_name']}", 9, regular, 5 * mm)
    line(f"ИНН: {doc['buyer_inn']}", 9, regular, 5 * mm)
    line(f"Адрес: {doc['buyer_address']}", 9, regular, 5 * mm)

    y -= 3 * mm
    page.setFont(bold, 10)
    page.drawString(left, y, "Наименование услуги")
    page.drawRightString(right, y, "Сумма")
    y -= 5 * mm
    page.line(left, y + 2 * mm, right, y + 2 * mm)

    page.setFont(regular, 9)
    for row in doc["rows"]:
        for chunk in _wrap(str(row["name"]), 78):
            page.drawString(left, y, chunk)
            y -= 5 * mm
        page.drawRightString(right, y + 5 * mm, money(row["amount"]))

    page.line(left, y + 2 * mm, right, y + 2 * mm)
    y -= 4 * mm
    page.setFont(bold, 11)
    page.drawString(left, y, "Итого к оплате")
    page.drawRightString(right, y, money(doc["total"]))
    y -= 8 * mm

    page.setFont(regular, 9)
    for text in (
        f"Оплатить до: {doc['due']}",
        f"Назначение платежа: {doc['purpose']}",
        doc["vat"],
        f"Оферта: {doc['offer']}" if doc["offer"] else "",
    ):
        if not text:
            continue
        for chunk in _wrap(text, 95):
            page.drawString(left, y, chunk)
            y -= 5 * mm

    page.showPage()
    page.save()
    return buffer.getvalue()


def _wrap(text: str, limit: int) -> list[str]:
    """Простой перенос по словам: в счёте длинных полей всего два."""
    words = str(text).split()
    if not words:
        return [""]
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        if len(current) + 1 + len(word) <= limit:
            current += " " + word
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines
