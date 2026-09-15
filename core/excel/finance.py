"""Книга финансовой раскладки: «Недели», «Месяцы», «По артикулам», «Методология».

Четыре листа собираются одним вызовом `core.xlsx.write_book`. Своей работы с
openpyxl здесь нет.

Лист «Методология» не украшение. Требование R165 говорит, что цифры любой
недели совпадают с отчётом в личном кабинете WB, а проверить это можно только
руками: для каждой строки названо поле ответа Wildberries и формула, по
которой из него получилось число. Те же формулы продублированы в `CLAUDE.md`
между маркерами autopilot.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from core.xlsx import Sheet, write_book

__all__ = [
    "finance_book",
    "finance_sheets",
    "WEEKS_SHEET",
    "MONTHS_SHEET",
    "ARTICLES_SHEET",
    "METHOD_SHEET",
    "METHODOLOGY",
]

WEEKS_SHEET = "Недели"
MONTHS_SHEET = "Месяцы"
ARTICLES_SHEET = "По артикулам"
METHOD_SHEET = "Методология"

MONEY_HEADERS = (
    "Продажи, ₽",
    "Возвраты, ₽",
    "К перечислению, ₽",
    "Комиссия WB, ₽",
    "Эквайринг, ₽",
    "Логистика, ₽",
    "Хранение, ₽",
    "Приёмка, ₽",
    "Штрафы, ₽",
    "Прочие удержания, ₽",
    "Корректировка вознаграждения, ₽",
)

WEEK_HEADERS = (
    "Отчёт WB",
    "Начало",
    "Конец",
    *MONEY_HEADERS,
    "Комиссия WB, %",
    "кВВ итоговый, %",
    "Эквайринг, %",
    "СПП, %",
    "Продано, шт",
    "Возвращено, шт",
    "Данные за неделю",
    "Сверка с отчётом WB",
)

MONTH_HEADERS = ("Месяц", "Недель", *MONEY_HEADERS, "Продано, шт", "Возвращено, шт")

ARTICLE_HEADERS = (
    "Артикул WB",
    "Артикул продавца",
    "Предмет",
    "Штук",
    *MONEY_HEADERS,
)

METHOD_HEADERS = ("Показатель", "Поле ответа WB", "Формула", "Пояснение")

CENT = Decimal("0.01")

# Источник всех цифр. Метод из ТЗ Wildberries отключил 15.07.2026.
SOURCE = (
    "POST https://finance-api.wildberries.ru/api/finance/v1/sales-reports/detailed, "
    "категория токена «Финансы», период weekly. Сверка - "
    "POST /api/finance/v1/sales-reports/list."
)

# Каждая строка: показатель, поле WB, формула, пояснение. По ней цифру можно
# проверить руками, открыв тот же отчёт в личном кабинете.
METHODOLOGY: tuple[tuple[str, str, str, str], ...] = (
    (
        "Источник данных",
        "-",
        SOURCE,
        "Лимит 1 запрос в минуту, самый жёсткий в проекте. Длинные периоды "
        "выгружаются страницами по rrdId и идут долго, поэтому работа всегда "
        "через очередь.",
    ),
    (
        "Ключ строки",
        "reportId, rrdId",
        "одна строка отчёта = одна строка в базе",
        "Повторная выгрузка той же недели заменяет строки, а не добавляет "
        "вторые.",
    ),
    (
        "Продажи, ₽",
        "retailAmount, docTypeName",
        "сумма retailAmount по строкам, где docTypeName не «Возврат»",
        "«Wildberries реализовал товар» по документам продажи.",
    ),
    (
        "Возвраты, ₽",
        "retailAmount, docTypeName",
        "сумма retailAmount по строкам, где docTypeName «Возврат»",
        "Возвраты показаны отдельной статьёй, а не вычтены из продаж.",
    ),
    (
        "К перечислению, ₽",
        "forPay",
        "сумма forPay по всем строкам недели, включая возвраты",
        "Готовое поле WB. Не пересчитывается: любой свой пересчёт разошёлся "
        "бы с личным кабинетом.",
    ),
    (
        "Комиссия WB, ₽",
        "vw",
        "сумма vw по строкам недели",
        "Вознаграждение Wildberries без НДС.",
    ),
    (
        "Комиссия WB, %",
        "commissionPercent",
        "средневзвешенное готового поля, вес - retailPriceWithDisc строки",
        "Процент не считается из денег. Проверка руками: "
        "vw / retailPriceWithDisc * 100.",
    ),
    (
        "кВВ итоговый, %",
        "kvw",
        "средневзвешенное готового поля, вес - retailPriceWithDisc строки",
        "Итоговый кВВ без НДС, уже с учётом рейтинга (supRatingUp) и акций "
        "(isKgvpV2).",
    ),
    (
        "Эквайринг, ₽",
        "acquiringFee",
        "сумма acquiringFee по строкам недели",
        "Компенсация платёжных услуг, банк в поле acquiringBank.",
    ),
    (
        "Эквайринг, %",
        "acquiringPercent",
        "средневзвешенное готового поля, вес - retailPriceWithDisc строки",
        "Проверка руками: acquiringFee / retailPriceWithDisc * 100.",
    ),
    (
        "Логистика, ₽",
        "deliveryService",
        "сумма deliveryService по строкам недели",
        "Услуги по доставке товара покупателю.",
    ),
    (
        "Доля логистики, %",
        "deliveryService, retailAmount",
        "сумма deliveryService / сумма retailAmount * 100",
        "Это наше определение, в документации Wildberries такой формулы нет.",
    ),
    (
        "Хранение, ₽",
        "paidStorage",
        "сумма paidStorage по строкам недели",
        "Платное хранение на складах WB.",
    ),
    (
        "Доля хранения, %",
        "paidStorage, retailAmount",
        "сумма paidStorage / сумма retailAmount * 100",
        "Это наше определение, в документации Wildberries такой формулы нет.",
    ),
    (
        "Приёмка, ₽",
        "paidAcceptance",
        "сумма paidAcceptance по строкам недели",
        "Платные операции на приёмке поставки.",
    ),
    (
        "Штрафы, ₽",
        "penalty",
        "сумма penalty по строкам недели",
        "Общая сумма штрафов недели.",
    ),
    (
        "Прочие удержания, ₽",
        "deduction",
        "сумма deduction по строкам недели",
        "Сюда Wildberries складывает в том числе удержания за рекламу.",
    ),
    (
        "Корректировка вознаграждения, ₽",
        "additionalPayment",
        "сумма additionalPayment по строкам недели",
        "Доплаты и корректировки вознаграждения WB.",
    ),
    (
        "СПП, %",
        "spp",
        "средневзвешенное готового поля, вес - retailPriceWithDisc строки",
        "Скидка постоянного покупателя, она же платформенные скидки.",
    ),
    (
        "Продано и возвращено, шт",
        "quantity, docTypeName",
        "сумма quantity отдельно по продажам и по возвратам",
        "Штуки, а не строки отчёта.",
    ),
    (
        "Сверка с отчётом WB",
        "forPaySum, retailAmountSum, deliveryServiceSum, paidStorageSum, "
        "paidAcceptanceSum, penaltySum, deductionSum",
        "наша сумма по строкам минус готовая сумма из sales-reports/list",
        "Расхождение больше рубля показывается честно. Рубль и меньше это "
        "округление копеек, а не ошибка. Если агрегат не получен, в колонке "
        "стоит «сверка не выполнена»: это не то же самое, что «сошлось».",
    ),
    (
        "Данные за неделю",
        "rrdId",
        "страниц прочитано против потолка страниц выгрузки",
        "Пагинация идёт по rrdId. Если страницы кончились по потолку, а "
        "курсор остался живым, неделя помечается неполной: показать "
        "обрезанные данные как целые значит тихо разойтись с кабинетом WB.",
    ),
    (
        "Свод в месяц",
        "dateFrom",
        "неделя относится к месяцу, в котором она началась",
        "Отчётная неделя WB может лежать на границе месяцев, делить её "
        "по дням мы не беремся.",
    ),
    (
        "По артикулам",
        "nmId, vendorCode, subjectName",
        "те же статьи, сгруппированные по nmId строк недели",
        "Строки без nmId (например, общие удержания) собраны в отдельную "
        "группу «без артикула».",
    ),
)


def _money(value: Any) -> Decimal:
    amount = value if isinstance(value, Decimal) else Decimal(str(value or 0))
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


def _percent(value: Any) -> Decimal | str:
    if value is None:
        return "нет данных"
    amount = value if isinstance(value, Decimal) else Decimal(str(value))
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


def _money_cells(amounts: Any) -> list[Decimal]:
    return [
        _money(amounts.revenue),
        _money(amounts.returns_amount),
        _money(amounts.for_pay),
        _money(amounts.commission),
        _money(amounts.acquiring),
        _money(amounts.logistics),
        _money(amounts.storage),
        _money(amounts.acceptance),
        _money(amounts.penalties),
        _money(amounts.deductions),
        _money(amounts.additional_payment),
    ]


def _check_cell(week: Any) -> str:
    """Словами: сошлось, разошлось или не проверялось. Прятать нельзя ничего.

    Три состояния, и они разные. «Сверка не выполнена» это не «сошлось»:
    неделя без агрегата Wildberries никем не подтверждена.
    """
    if not week.verified or not week.checks:
        return "сверка не выполнена: Wildberries не отдал итоги отчёта"
    bad = week.mismatches
    if not bad:
        return "сошлось с отчётом WB"
    parts = [f"{check.name}: расхождение {_money(check.diff)} ₽" for check in bad]
    return "; ".join(parts)


def _complete_cell(week: Any) -> str:
    """Полная неделя или обрезанная потолком страниц.

    Формулировка утвердительная, потому что признак это факт от самой
    пагинации: страницы кончились по потолку, а курсор остался живым. Ровно
    полная последняя страница сюда не попадает.
    """
    if week.complete:
        return "полные"
    return "неполные: выгрузка упёрлась в предел страниц, запросите период короче"


def finance_sheets(report: Any) -> list[Sheet]:
    """Четыре листа книги. Отдельно от записи, чтобы их можно было проверить."""
    weeks = [
        [
            week.report_id,
            week.date_from,
            week.date_to,
            *_money_cells(week.amounts),
            _percent(week.commission_percent),
            _percent(week.kvw),
            _percent(week.acquiring_percent),
            _percent(week.spp),
            week.amounts.sales_count,
            week.amounts.returns_count,
            _complete_cell(week),
            _check_cell(week),
        ]
        for week in report.weeks
    ]

    months = [
        [
            month.key,
            month.weeks,
            *_money_cells(month.amounts),
            month.amounts.sales_count,
            month.amounts.returns_count,
        ]
        for month in report.months
    ]

    articles = [
        [
            article.nm_id if article.nm_id is not None else "без артикула",
            article.vendor_code,
            article.subject,
            article.quantity,
            *_money_cells(article.amounts),
        ]
        for article in report.articles
    ]

    return [
        Sheet(WEEKS_SHEET, WEEK_HEADERS, weeks),
        Sheet(MONTHS_SHEET, MONTH_HEADERS, months),
        Sheet(ARTICLES_SHEET, ARTICLE_HEADERS, articles),
        Sheet(METHOD_SHEET, METHOD_HEADERS, [list(row) for row in METHODOLOGY]),
    ]


def finance_book(report: Any, *, path: str | None = None) -> bytes:
    """Книга целиком, байтами: её чаще отправляют, чем сохраняют."""
    return write_book(finance_sheets(report), path=path)
