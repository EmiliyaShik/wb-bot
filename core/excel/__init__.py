"""Книги Excel, которые бот отдаёт клиенту.

Сама работа с openpyxl живёт в `core.xlsx` и повторять её тут нечего: здесь
только состав листов и подписи колонок. Пока книга одна, финансовая.
"""

from __future__ import annotations

from core.excel.finance import (
    ARTICLES_SHEET,
    METHOD_SHEET,
    MONTHS_SHEET,
    WEEKS_SHEET,
    finance_book,
    finance_sheets,
)

__all__ = [
    "finance_book",
    "finance_sheets",
    "WEEKS_SHEET",
    "MONTHS_SHEET",
    "ARTICLES_SHEET",
    "METHOD_SHEET",
]
