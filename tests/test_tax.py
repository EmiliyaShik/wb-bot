"""Налог селлера: база, ставка, оценка и прибыль после неё.

Швы те же два, что и у всех: путь к базе (временный файл) и транспорт WB.
Сети здесь нет вовсе: отчёт о прибыли строится по базе, а расход рекламы
подаётся готовым объектом, как его подаёт обработчик очереди.

Главное, что проверяется, это база налога. У нас в базе лежат оба числа:
`retail_amount_kop` (заплатил покупатель) и `ppvz_for_pay_kop` (перечислил
Wildberries). Они здесь нарочно разные и разные заметно, иначе подмена
одного другим прошла бы мимо теста незамеченной.

Ожидаемые числа посчитаны руками и записаны рядом в комментариях.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from types import SimpleNamespace

import pytest

from agents import finance, profit
from bot.handlers import profit as handlers_profit
from bot.handlers import settings as settings_handler
from core import clients, config, costs as costs_module, db, tax, xlsx

TODAY = date(2026, 9, 7)

# Одна неделя отчёта о реализации. Числа подобраны так, чтобы «заплатил
# покупатель» и «перечислил Wildberries» расходились на четверть: процент с
# поступления и процент с полной цены продажи обязаны различаться в тесте не
# в последнем знаке, а в первом.
#
# Заплатил покупатель: 10000 + 5000 - 2000 = 13000.
# Перечислил Wildberries: 7000 + 3500 - 1400 = 9100.
ROWS = [
    {
        "reportId": 1,
        "rrdId": 1,
        "dateFrom": "2026-09-01",
        "dateTo": "2026-09-07",
        "nmId": 111,
        "vendorCode": "A-1",
        "subjectName": "Кружка",
        "docTypeName": "Продажа",
        "quantity": 5,
        "retailAmount": 10000,
        "retailPriceWithDisc": 2000,
        "forPay": 7000,
        "vw": 1500,
        "acquiringFee": 200,
        "deliveryService": 500,
        "paidStorage": 100,
        "paidAcceptance": 50,
    },
    {
        "reportId": 1,
        "rrdId": 2,
        "dateFrom": "2026-09-01",
        "dateTo": "2026-09-07",
        "nmId": 222,
        "vendorCode": "B-2",
        "subjectName": "Ложка",
        "docTypeName": "Продажа",
        "quantity": 2,
        "retailAmount": 5000,
        "retailPriceWithDisc": 2500,
        "forPay": 3500,
        "vw": 800,
        "acquiringFee": 100,
        "deliveryService": 300,
        "penalty": 200,
    },
    {
        "reportId": 1,
        "rrdId": 3,
        "dateFrom": "2026-09-01",
        "dateTo": "2026-09-07",
        "docTypeName": "Удержание",
        "quantity": 0,
        "deduction": 400,
    },
    {
        "reportId": 1,
        "rrdId": 4,
        "dateFrom": "2026-09-01",
        "dateTo": "2026-09-07",
        "nmId": 111,
        "vendorCode": "A-1",
        "subjectName": "Кружка",
        "docTypeName": "Возврат",
        "quantity": 1,
        "retailAmount": 2000,
        "retailPriceWithDisc": 2000,
        "forPay": -1400,
        "vw": -300,
    },
]

ADS = profit.AdSpend({111: Decimal("300"), 222: Decimal("100")})

# Что заплатил покупатель за вычетом возвратов и что перечислил Wildberries.
INCOME = Decimal("13000")
TRANSFERRED = Decimal("9100")

# Прибыль по обоим артикулам, посчитанная руками:
# 111: (10000 - 2000) - 1600 себестоимости - 2050 удержаний - 300 рекламы
#      - 246.15 обезлички = 3803.85
# 222: 5000 - 1800 - 1400 - 100 - 153.85 = 1546.15
PROFIT = Decimal("5350.00")


@pytest.fixture
def seller(db_path):
    """Кабинет с неделей агента 1 и загруженной себестоимостью."""
    client_id = db.admin_repo(db_path).ensure_client(7070)
    finance.save_rows(client_id, ROWS, path=db_path)
    for week in finance.aggregate(ROWS).values():
        finance.save_week(client_id, week, path=db_path)
    costs_module.save_costs(
        client_id, {111: Decimal("400"), 222: Decimal("900")}, path=db_path
    )
    return client_id


def report_of(client_id, db_path):
    return profit.build(client_id, "week", today=TODAY, ads=ADS, path=db_path)


# --- база налога --------------------------------------------------------------


def test_the_base_is_what_the_buyer_paid_and_not_what_wildberries_transferred(
    seller, db_path
):
    """Ради этого вся работа и делалась.

    Селлер видит поступление на счёт и считает процент с него, а налоговая
    считает с полной цены продажи. Здесь эти два числа расходятся на 3900
    рублей, и налог обязан сойтись с первым, а не со вторым.
    """
    tax.set_rule(seller, tax.USN_INCOME, 6, path=db_path)
    estimate = report_of(seller, db_path).tax

    assert estimate.income == INCOME
    assert estimate.transferred == TRANSFERRED
    assert estimate.gap == Decimal("3900.00")

    # 13000 * 6 / 100 = 780. С поступления вышло бы 546, и это та самая
    # ошибка, за которую приходят доначисления.
    assert estimate.amount == Decimal("780.00")
    assert estimate.amount != (TRANSFERRED * Decimal("6") / 100).quantize(
        Decimal("0.01")
    )


def test_the_base_takes_every_row_of_the_period_not_only_the_articles(seller, db_path):
    """База считается по неделям, а не по разрезу артикулов.

    Строки без `nmId` в разрез по товарам не попадают вовсе. Считать базу по
    нему значило бы потерять выручку, которую Wildberries не разнёс.
    """
    report = report_of(seller, db_path)
    amounts = report.period_amounts

    assert amounts.revenue - amounts.returns_amount == INCOME
    assert amounts.for_pay == TRANSFERRED


def test_returns_lower_the_base(seller, db_path):
    """Возврат уменьшает базу, и делит продажи с возвратами тип документа.

    Второго способа отличать возврат в проекте нет: тот же `docTypeName`,
    по которому их делит агент 1.
    """
    tax.set_rule(seller, tax.USN_INCOME, 6, path=db_path)
    with_return = report_of(seller, db_path).tax

    # Та же неделя без строки возврата: база обязана вырасти ровно на её
    # retailAmount, а налог ровно на 6 процентов от него.
    other = db.admin_repo(db_path).ensure_client(7171)
    rows = [row for row in ROWS if row["docTypeName"] != "Возврат"]
    finance.save_rows(other, rows, path=db_path)
    for week in finance.aggregate(rows).values():
        finance.save_week(other, week, path=db_path)
    tax.set_rule(other, tax.USN_INCOME, 6, path=db_path)
    without_return = report_of(other, db_path).tax

    assert without_return.income - with_return.income == Decimal("2000")
    assert without_return.amount - with_return.amount == Decimal("120.00")


# --- режим не выбран ----------------------------------------------------------


def test_without_a_chosen_mode_the_tax_is_not_invented(seller, db_path):
    """Ни нуля, ни расчёта по умолчанию: налога просто нет."""
    report = report_of(seller, db_path)

    assert tax.rule_of(seller, path=db_path).known is False
    assert report.tax is None
    assert report.profit_after_tax is None
    # Прибыль при этом считается как раньше, отчёт не ломается.
    assert report.total_profit == PROFIT


def test_the_report_says_out_loud_that_the_profit_is_before_tax(seller, db_path):
    """Молчаливый ноль хуже отсутствия строки, поэтому строка есть."""
    text = handlers_profit.summary_text(report_of(seller, db_path))

    assert "Налог не учтён" in text
    assert "до налога" in text
    assert "/settings" in text


def test_a_broken_setting_is_read_as_no_mode_at_all(seller, db_path):
    """Испорченная настройка это «не выбран», а не «посчитаем по умолчанию».

    Подставить умолчание значило бы посчитать налог по ставке, которой селлер
    не выбирал, и назвать её его ставкой.
    """
    for broken in ({"mode": "osno", "rate": "20"}, {"mode": tax.USN_INCOME, "rate": "200"}):
        data = clients.settings_of(seller, path=db_path)
        data[tax.SETTINGS_KEY] = broken
        clients.save_settings(seller, data, path=db_path)

        assert tax.rule_of(seller, path=db_path).known is False
        assert report_of(seller, db_path).tax is None


# --- ставка -------------------------------------------------------------------


def test_the_rate_comes_from_the_settings_of_this_seller(seller, db_path):
    """Ставка региональная, поэтому её выбирает селлер, а не код."""
    tax.set_rule(seller, tax.USN_INCOME, 1, path=db_path)
    # 13000 * 1 / 100 = 130.
    assert report_of(seller, db_path).tax.amount == Decimal("130.00")

    tax.set_rule(seller, tax.USN_INCOME, 6, path=db_path)
    assert report_of(seller, db_path).tax.amount == Decimal("780.00")


def test_the_default_rate_lives_in_the_config_and_not_in_the_code(seller, db_path, monkeypatch):
    """Умолчание меняется правкой конфига, выбор селлера сильнее его."""
    import copy

    assert tax.default_rate(tax.USN_INCOME) == Decimal(
        str(config.settings()["tax"]["usn_income"])
    )

    patched = copy.deepcopy(config.settings())
    patched["tax"]["usn_income"] = 5
    monkeypatch.setattr(config, "settings", lambda: patched)
    assert tax.default_rate(tax.USN_INCOME) == Decimal("5")

    tax.set_rule(seller, tax.USN_INCOME, 2, path=db_path)
    patched["tax"]["usn_income"] = 4
    assert tax.rule_of(seller, path=db_path).rate == Decimal("2")


@pytest.mark.parametrize("bad", [-6, 0, 200, "шесть", "", None, 6.5, "6,5"])
def test_a_forged_rate_is_refused(seller, db_path, bad):
    """Белый список, а не границы: принимается то, что бот сам и нарисовал.

    `None` тут отдельный случай: это «ставку не назвали», и тогда встаёт
    умолчание режима. Всё остальное отказ.
    """
    if bad is None:
        assert tax.set_rule(seller, tax.USN_INCOME, None, path=db_path).rate == (
            tax.default_rate(tax.USN_INCOME)
        )
        return
    with pytest.raises(ValueError):
        tax.set_rule(seller, tax.USN_INCOME, bad, path=db_path)
    assert tax.rule_of(seller, path=db_path).known is False


def test_a_rate_of_the_other_mode_is_refused(seller, db_path):
    """Шесть процентов бывают у «доходов», но не у «доходы минус расходы»."""
    with pytest.raises(ValueError):
        tax.set_rule(seller, tax.USN_INCOME_MINUS, 6, path=db_path)
    with pytest.raises(ValueError):
        tax.set_rule(seller, tax.USN_INCOME, 15, path=db_path)


def test_a_made_up_mode_is_refused(seller, db_path):
    """Два режима и только два: решение владельца."""
    for mode in ("osno", "patent", "", None, tax.USN_INCOME.upper()):
        with pytest.raises(ValueError):
            tax.set_rule(seller, mode, 6, path=db_path)
    assert tax.rule_of(seller, path=db_path).known is False


def test_the_tax_setting_does_not_wipe_the_neighbours(seller, db_path):
    """Словарь настроек общий: свой ключ не затирает чужие."""
    data = clients.settings_of(seller, path=db_path)
    data["token_reminders"] = [14]
    clients.save_settings(seller, data, path=db_path)

    tax.set_rule(seller, tax.USN_INCOME, 6, path=db_path)

    assert clients.reminded(seller, path=db_path) == (14,)
    assert tax.rule_of(seller, path=db_path).rate == Decimal("6")


# --- прибыль после налога -----------------------------------------------------


def test_the_profit_after_tax_is_counted_and_not_a_kopeck_is_lost(seller, db_path):
    """Прибыль после налога это прибыль минус оценка, копейка в копейку."""
    tax.set_rule(seller, tax.USN_INCOME, 6, path=db_path)
    report = report_of(seller, db_path)

    assert report.total_profit == PROFIT
    # 5350.00 - 780.00 = 4570.00
    assert report.profit_after_tax == Decimal("4570.00")
    assert report.profit_after_tax + report.tax.amount == report.total_profit
    assert report.profit_after_tax.as_tuple().exponent == -2


def test_the_money_path_is_decimal_all_the_way(seller, db_path):
    """`float` в денежном пути не появляется даже промежуточно."""
    tax.set_rule(seller, tax.USN_INCOME_MINUS, 15, path=db_path)
    estimate = report_of(seller, db_path).tax

    for value in (
        estimate.income,
        estimate.transferred,
        estimate.gap,
        estimate.expenses,
        estimate.base,
        estimate.amount,
        estimate.minimum,
    ):
        assert isinstance(value, Decimal)


def test_rounding_is_half_up_to_the_kopeck():
    """Округление такое же, как у соседей. Чистый расчёт, база не нужна."""
    rule = tax.Rule(mode=tax.USN_INCOME, rate=Decimal("6"))
    # 1000.25 * 6 / 100 = 60.015, вверх это 60.02.
    assert tax.Estimate(rule=rule, income=Decimal("1000.25")).amount == Decimal("60.02")
    # 1000.05 * 6 / 100 = 60.003, вниз это 60.00.
    assert tax.Estimate(rule=rule, income=Decimal("1000.05")).amount == Decimal("60.00")


# --- доходы минус расходы -----------------------------------------------------


def test_penalties_do_not_get_into_the_expenses(seller, db_path):
    """Штрафы Wildberries в расходы обычно не принимаются, и бот их не берёт.

    Удержания недели: комиссия 2000, эквайринг 300, логистика 800, хранение
    100, приёмка 50, штрафы 200, прочие 400. Без штрафов это 3650.
    Себестоимость 1600 + 1800 = 3400, реклама 400. Итого 7450.
    """
    tax.set_rule(seller, tax.USN_INCOME_MINUS, 15, path=db_path)
    estimate = report_of(seller, db_path).tax

    assert estimate.expenses == Decimal("7450")
    # 13000 - 7450 = 5550, 15 процентов от неё это 832.50.
    assert estimate.base == Decimal("5550.00")
    assert estimate.amount == Decimal("832.50")


def test_the_report_names_what_went_into_the_expenses_and_what_did_not(seller, db_path):
    """Честность важнее полноты: состав расходов назван словами."""
    tax.set_rule(seller, tax.USN_INCOME_MINUS, 15, path=db_path)
    text = handlers_profit.summary_text(report_of(seller, db_path))

    assert "себестоимость проданного товара" in text
    assert "Штрафы не взял" in text
    assert "Страховые" in text and "аренду" in text


def test_incomplete_expenses_are_named_and_not_hidden(seller, db_path):
    """Нет себестоимости по всем артикулам или нет рекламы - налог завышен."""
    lean = db.admin_repo(db_path).ensure_client(7272)
    finance.save_rows(lean, ROWS, path=db_path)
    for week in finance.aggregate(ROWS).values():
        finance.save_week(lean, week, path=db_path)
    costs_module.save_costs(lean, {111: Decimal("400")}, path=db_path)
    tax.set_rule(lean, tax.USN_INCOME_MINUS, 15, path=db_path)
    report = profit.build(
        lean,
        "week",
        today=TODAY,
        ads=profit.AdSpend({}, available=False, reason=profit.ADS_NO_CATEGORY),
        path=db_path,
    )

    assert set(report.tax.missing) == {tax.MISSING_COST, tax.MISSING_ADS}
    assert report.tax.complete is False
    text = handlers_profit.summary_text(report)
    assert "Расходы посчитаны не полностью" in text
    assert "завышен" in text


def test_the_minimum_tax_rule_is_named_and_not_passed_off_as_a_payment(seller, db_path):
    """Правило годовое, а отчёт за период: так и написано, а не замолчано."""
    tax.set_rule(seller, tax.USN_INCOME_MINUS, 15, path=db_path)
    report = report_of(seller, db_path)

    # 1 процент от 13000 это 130.
    assert report.tax.minimum == Decimal("130.00")
    assert report.tax.below_minimum is False
    text = handlers_profit.summary_text(report)
    assert "минимального налога" in text
    assert "правило годовое" in text.lower()


def test_a_loss_period_gives_no_tax_and_says_so(seller, db_path):
    """С отрицательной базы налог не берётся, и это сказано вслух."""
    rule = tax.Rule(mode=tax.USN_INCOME_MINUS, rate=Decimal("15"))
    estimate = tax.Estimate(
        rule=rule, income=Decimal("1000"), expenses=Decimal("1500")
    )

    assert estimate.base == Decimal("-500.00")
    assert estimate.loss is True
    assert estimate.amount == Decimal("0")


# --- отчёт --------------------------------------------------------------------


def test_the_message_shows_the_base_the_transfer_and_the_difference(seller, db_path):
    """Польза не в проценте, а в двух суммах рядом."""
    tax.set_rule(seller, tax.USN_INCOME, 6, path=db_path)
    text = handlers_profit.summary_text(report_of(seller, db_path))

    assert "13 000 ₽" in text
    assert "9 100 ₽" in text
    assert "3 900 ₽" in text
    assert "780 ₽" in text
    assert "4 570 ₽" in text
    # Никаких обещаний: это оценка, а не налог к уплате.
    assert "оценка, а не налог к уплате" in text
    assert "бухгалтера" in text


def test_the_workbook_has_a_tax_sheet_with_the_base_named(seller, db_path):
    """Лист «Налог» и лист «Методология» обязаны объяснить цифру."""
    tax.set_rule(seller, tax.USN_INCOME, 6, path=db_path)
    report = report_of(seller, db_path)
    book = xlsx.read_book(profit.excel_bytes(report))

    sheet = book[profit.TAX_SHEET]
    assert tuple(sheet.headers) == profit.TAX_HEADERS
    labels = [row.get("Показатель") for row in sheet.rows]
    assert "Доходы: заплатил покупатель" in labels
    assert "Для сравнения: перечислил Wildberries" in labels
    assert "Налог, оценка" in labels
    assert "Чистая прибыль после налога" in labels

    values = {row.get("Показатель"): row.get("Сумма, ₽") for row in sheet.rows}
    assert Decimal(str(values["Доходы: заплатил покупатель"])) == INCOME
    assert Decimal(str(values["Для сравнения: перечислил Wildberries"])) == TRANSFERRED
    assert Decimal(str(values["Налог, оценка"])) == Decimal("780.00")
    assert Decimal(str(values["Чистая прибыль после налога"])) == Decimal("4570.00")

    method = " ".join(
        str(cell)
        for row in book[profit.METHOD_SHEET].rows
        for cell in (row.get("Показатель"), row.get("Формула"), row.get("Пояснение"))
    )
    assert "retailAmount" in method and "forPay" in method
    assert "ставка / 100" in method


def test_the_workbook_without_a_mode_says_the_tax_is_not_counted(seller, db_path):
    """Пустой лист прочитался бы как потеря данных, поэтому там фраза."""
    book = xlsx.read_book(profit.excel_bytes(report_of(seller, db_path)))
    rows = book[profit.TAX_SHEET].rows

    assert len(rows) == 1
    assert rows[0].get("Показатель") == "Налог не посчитан"
    assert "/settings" in str(rows[0].get("Пояснение"))


def articles_sheet(report):
    return xlsx.read_book(profit.excel_bytes(report))[profit.ARTICLES_SHEET]


def by_label(sheet):
    return {str(row.get("Артикул WB")): row for row in sheet.rows}


def method_sheet(report):
    return xlsx.read_book(profit.excel_bytes(report))[profit.METHOD_SHEET]


def method_text(report):
    return " ".join(str(cell) for row in method_sheet(report).rows for cell in row.values)


def articles_by_nm(sheet):
    """Строки товаров по артикулу: итог и строки под ним сюда не попадают."""
    return {
        row.get("Артикул WB"): row
        for row in sheet.rows
        if isinstance(row.get("Артикул WB"), int)
    }


def test_on_usn_income_the_tax_of_an_article_is_the_rate_of_its_own_revenue(
    seller, db_path
):
    """На «доходах» налог товара это не доля общего, а ставка от его выручки.

    Прежнее решение (налог только в итоге, разнесение названо лёгкой
    неправдой) владелец отменила и объяснила зачем: нужна прибыль по каждому
    артикулу. На этом режиме так и можно без всякой раскладки: база налога
    это выручка, и у товара она своя.

    Числа руками: выручка 111 это 10000 минус возврат 2000, то есть 8000, и
    шесть процентов от неё 480. У 222 выручка 5000, налог 300. Вместе 780, и
    столько же выходит налог кабинета от 13000 доходов.
    """
    tax.set_rule(seller, tax.USN_INCOME, 6, path=db_path)
    report = report_of(seller, db_path)
    allocation = report.tax_by_article

    assert allocation.exact is True
    assert allocation.of(111) == Decimal("480.00")
    assert allocation.of(222) == Decimal("300.00")
    # То же число, посчитанное другим способом: ставка от выручки артикула за
    # вычетом его возвратов. Это и есть «точный налог этого товара».
    for nm_id, net_revenue in ((111, Decimal("8000")), (222, Decimal("5000"))):
        assert allocation.of(nm_id) == net_revenue * Decimal("6") / Decimal("100")
    assert allocation.total == Decimal("780.00")
    assert allocation.gap == Decimal("0")

    sheet = articles_sheet(report)
    assert profit.TAX_COLUMN in sheet.headers
    assert profit.AFTER_TAX_COLUMN in sheet.headers
    # Раскладкой это не называется: на «доходах» раскладки нет вовсе.
    assert profit.TAX_SPREAD_COLUMN not in sheet.headers

    rows = articles_by_nm(sheet)
    assert Decimal(str(rows[111].get(profit.TAX_COLUMN))) == Decimal("480")
    assert Decimal(str(rows[222].get(profit.TAX_COLUMN))) == Decimal("300")
    # Прибыль после налога: 3803,85 - 480 и 1546,15 - 300.
    assert Decimal(str(rows[111].get(profit.AFTER_TAX_COLUMN))) == Decimal("3323.85")
    assert Decimal(str(rows[222].get(profit.AFTER_TAX_COLUMN))) == Decimal("1246.15")

    total = by_label(sheet)[profit.TOTAL_LABEL]
    assert Decimal(str(total.get(profit.TAX_COLUMN))) == Decimal("780")
    assert Decimal(str(total.get(profit.AFTER_TAX_COLUMN))) == Decimal("4570")
    # Денежный формат колонкам приходит сам, по знаку рубля в заголовке:
    # второго правила для денег в книге не заводится.
    assert xlsx.is_money_header(profit.TAX_COLUMN) is True
    assert xlsx.is_money_header(profit.AFTER_TAX_COLUMN) is True

    note = str(by_label(sheet)[profit.TAX_NOTE_LABEL].values[1])
    assert "УСН «доходы»" in note and "ставка 6%" in note
    assert "налог именно этого товара" in note
    assert "раскладка" not in note


# Четыре товара с выручкой 1000,25. Шесть процентов от неё это 60,015, то есть
# ровно половина копейки: округление по правилам обязано дать 60,02 в каждой
# строке, сумма столбца 240,08, а ставка от общей базы 4001,00 даёт 240,06.
# Расхождение в две копейки тут не случайность, а то самое, о чём отчёт должен
# сказать вслух. Строк без артикула здесь нет нарочно: в разницу не должно
# примешиваться ничего, кроме округления.
ROUNDING_ROWS = [
    {
        "reportId": 9,
        "rrdId": 90 + index,
        "dateFrom": "2026-09-01",
        "dateTo": "2026-09-07",
        "nmId": 101 + index,
        "vendorCode": f"R-{index}",
        "subjectName": "Кружка",
        "docTypeName": "Продажа",
        "quantity": 1,
        "retailAmount": 1000.25,
        "retailPriceWithDisc": 1000.25,
        "forPay": 700,
        "vw": 100,
    }
    for index in range(4)
]


@pytest.fixture
def rounding_seller(db_path):
    """Кабинет, на котором округление строк заведомо разъезжается с итогом."""
    client_id = db.admin_repo(db_path).ensure_client(7171)
    finance.save_rows(client_id, ROUNDING_ROWS, path=db_path)
    for week in finance.aggregate(ROUNDING_ROWS).values():
        finance.save_week(client_id, week, path=db_path)
    # Себестоимость есть не у всех: у 104 прибыли не будет вовсе, а налог
    # будет, он считается от выручки.
    costs_module.save_costs(
        client_id,
        {101: Decimal("100"), 102: Decimal("100"), 103: Decimal("100")},
        path=db_path,
    )
    tax.set_rule(client_id, tax.USN_INCOME, 6, path=db_path)
    return client_id


def rounding_report(client_id, db_path):
    return profit.build(
        client_id, "week", today=TODAY, ads=profit.AdSpend({}), path=db_path
    )


def test_every_row_is_rounded_on_its_own_and_the_gap_is_named(rounding_seller, db_path):
    """Строки округлены по правилам, а расхождение названо, а не спрятано.

    Подгонять строки под итог нельзя: подкрученная копейка это число, которого
    у товара нет. Поэтому в итоге листа стоит сумма столбца (селлер проверяет
    таблицу сложением, и расхождение в две копейки он прочитает как ошибку во
    всей таблице), на листе «Налог» остаётся расчёт от общей базы, а разница
    объяснена словами.

    Числа руками: 1000,25 * 6% = 60,015, по правилам округления 60,02 в каждой
    из четырёх строк, сумма 240,08. Общая база 4001,00, ставка от неё 240,06.
    Разница две копейки.
    """
    report = rounding_report(rounding_seller, db_path)
    allocation = report.tax_by_article

    assert report.tax.income == Decimal("4001.00")
    assert report.tax.amount == Decimal("240.06")
    for nm_id in (101, 102, 103, 104):
        # Каждая строка это обычное округление своей же ставки, без подкруток.
        assert allocation.of(nm_id) == Decimal("60.02")
        assert allocation.of(nm_id) == (
            Decimal("1000.25") * Decimal("6") / Decimal("100")
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    assert allocation.total == Decimal("240.08")
    assert allocation.gap == Decimal("-0.02")
    assert allocation.matches is False

    sheet = articles_sheet(report)
    rows = articles_by_nm(sheet)
    column = [Decimal(str(row.get(profit.TAX_COLUMN))) for row in rows.values()]
    total = by_label(sheet)[profit.TOTAL_LABEL]
    # Сложение столбца и клетка итога это одно и то же число.
    assert sum(column) == Decimal("240.08")
    assert Decimal(str(total.get(profit.TAX_COLUMN))) == Decimal("240.08")

    # А на листе «Налог» остался расчёт от общей базы, и он другой.
    amounts = {
        row.get("Показатель"): row.get("Сумма, ₽")
        for row in xlsx.read_book(profit.excel_bytes(report))[profit.TAX_SHEET].rows
    }
    assert Decimal(str(amounts["Налог, оценка"])) == Decimal("240.06")

    # Разница названа под итогом: оба числа и причина.
    note = str(by_label(sheet)[profit.TAX_NOTE_LABEL].values[1])
    assert "240.08" in note and "240.06" in note
    assert "округление строк" in note

    # И объяснена на листе «Методология», а не оставлена загадкой.
    method = method_text(report)
    assert "сумма столбца" in method
    assert "от общей базы" in method
    assert "не ошибка" in method


def test_a_gap_too_big_for_rounding_is_not_called_rounding(rounding_seller, db_path):
    """Большую разницу округлением не называем: это была бы та же неправда.

    Округление строки не ошибается больше, чем на половину копейки, поэтому
    разница в рубли это что-то другое: обычно выручка, которую Wildberries по
    товарам не разнёс. Эта выручка нам известна числом, поэтому она и названа
    числом, а не «чем-то кроме округления». Проверяется подменой самой базы
    налога, потому что настоящую такую неделю на живом кабинете ещё ждать.
    """
    whole = rounding_report(rounding_seller, db_path)
    # У отчёта убран один товар, а доходы периода остались те же: ровно так
    # выглядит выручка, которую Wildberries по товарам не разнёс.
    report = replace(whole, articles=whole.articles[:3])
    allocation = report.tax_by_article

    # Налог кабинета тот же 240,06, а столбец теперь 180,06.
    assert allocation.total == Decimal("180.06")
    assert allocation.gap == Decimal("60.00")
    # И причина разницы названа точно: выручка без артикула 1000,25 и налог с
    # неё. Копеечный остаток после неё это уже округление.
    assert allocation.uncovered == Decimal("1000.25")
    assert allocation.uncovered_tax == Decimal("60.02")
    assert allocation.rounding is False
    assert allocation.explained is True

    note = str(by_label(articles_sheet(report))[profit.TAX_NOTE_LABEL].values[1])
    assert "это не округление" in note
    assert "не разнёс по товарам выручку" in note
    assert "1000.25" in note and "60.02" in note
    assert "это округление строк до копейки" not in note

    # А у целого отчёта разница копеечная, и она названа округлением.
    whole_note = str(by_label(articles_sheet(whole))[profit.TAX_NOTE_LABEL].values[1])
    assert whole.tax_by_article.rounding is True
    assert "это округление строк до копейки" in whole_note


def test_an_article_without_profit_gets_a_tax_but_no_profit_after_it(
    rounding_seller, db_path
):
    """Налог есть, прибыли после налога нет: неизвестное не становится нулём.

    У 104 себестоимость не загружена, прибыль по нему не считается. Налог при
    этом считается: он от выручки, а выручка известна всегда. Ноль в клетке
    прибыли после налога прочитался бы как «отработали в ноль».
    """
    report = rounding_report(rounding_seller, db_path)
    sheet = articles_sheet(report)
    row = articles_by_nm(sheet)[104]

    assert Decimal(str(row.get(profit.TAX_COLUMN))) == Decimal("60.02")
    assert row.get("Чистая прибыль, ₽") == profit.NO_DATA
    assert row.get(profit.AFTER_TAX_COLUMN) == profit.NO_DATA
    assert row.get(profit.AFTER_TAX_COLUMN) != 0
    # И в итоге сказано, что налог сложен по всем, а прибыль после него нет:
    # иначе два столбца с разным числом слагаемых выглядели бы ошибкой.
    note = str(by_label(sheet)[profit.TOTAL_LABEL].values[1])
    assert "налог сложен по всем артикулам" in note
    assert "прибыль после налога только по тем" in note


def test_the_seller_can_check_the_after_tax_total_by_adding(rounding_seller, db_path):
    """Итог прибыли после налога проверяется вычитанием, и оно сходится.

    Главная проверка владельца это сложение столбца, поэтому в обеих клетках
    итога стоит сумма своего столбца, и подрезать их нельзя ни одну. Но тогда
    «итого прибыль после налога» не равно «итого прибыль» минус «итого налог»:
    налог сложен по всем артикулам, а прибыль после налога только по тем, у
    кого посчитана прибыль. Разница это налог артикулов без прибыли, и она
    названа отдельной строкой: по ней селлер проверяет строку и не теряет
    доверия к таблице.

    Числа руками: у 101, 102 и 103 прибыль 1000,25 - 100 себестоимости - 100
    комиссии = 800,25, налог 60,02, после налога 740,23. Итог прибыли 2400,75,
    итог налога 240,08 (с 104, у которого прибыли нет), итог после налога
    2220,69. Вычитание в лоб даёт 2160,67, то есть на 60,02 меньше, и ровно
    это число стоит в строке.
    """
    report = rounding_report(rounding_seller, db_path)
    sheet = articles_sheet(report)
    total = by_label(sheet)[profit.TOTAL_LABEL]
    bridge = by_label(sheet)[profit.TAX_UNPRICED_LABEL]

    profit_total = Decimal(str(total.get("Чистая прибыль, ₽")))
    tax_total = Decimal(str(total.get(profit.TAX_COLUMN)))
    after_total = Decimal(str(total.get(profit.AFTER_TAX_COLUMN)))
    unpriced = Decimal(str(bridge.get(profit.TAX_COLUMN)))

    assert profit_total == Decimal("2400.75")
    assert tax_total == Decimal("240.08")
    assert after_total == Decimal("2220.69")
    assert unpriced == Decimal("60.02")
    # Вычитание в лоб не сходится, и это не чинится подрезкой итогов.
    assert profit_total - tax_total != after_total
    # А вот эта проверка сходится копейка в копейку, и она написана в книге
    # словами: из итога налога вычесть строку, результат вычесть из прибыли.
    assert profit_total - (tax_total - unpriced) == after_total

    # Оба итога при этом остались суммами своих столбцов: сложение работает.
    rows = articles_by_nm(sheet)
    assert sum(Decimal(str(row.get(profit.TAX_COLUMN))) for row in rows.values()) == (
        tax_total
    )
    assert sum(
        Decimal(str(row.get(profit.AFTER_TAX_COLUMN)))
        for row in rows.values()
        if row.get(profit.AFTER_TAX_COLUMN) != profit.NO_DATA
    ) == after_total

    # И сказано, что именно проверять: оговорки словами здесь мало, селлер
    # проверяет вычитанием.
    note = str(bridge.values[1])
    assert "«Итого прибыль после налога» это не «Итого прибыль» минус" in note
    assert "«Итого налог» минус это число" in note
    assert str(report.tax_without_profit) == "60.02"


def test_without_articles_out_of_the_profit_there_is_no_bridge_row(seller, db_path):
    """Себестоимость загружена по всем: строке-мосту в книге делать нечего.

    Строка объясняет расхождение, которого в этой книге нет: вычитание здесь
    сходится само. Постоянная строка про случай, которого нет, отучает читать
    строки под итогом вообще.
    """
    tax.set_rule(seller, tax.USN_INCOME, 6, path=db_path)
    report = report_of(seller, db_path)
    sheet = articles_sheet(report)
    total = by_label(sheet)[profit.TOTAL_LABEL]

    assert report.tax_without_profit == Decimal("0.00")
    assert profit.TAX_UNPRICED_LABEL not in by_label(sheet)
    # И вычитание в лоб здесь сходится: 5350 - 780 = 4570.
    assert Decimal(str(total.get("Чистая прибыль, ₽"))) - Decimal(
        str(total.get(profit.TAX_COLUMN))
    ) == Decimal(str(total.get(profit.AFTER_TAX_COLUMN)))


# Товар 555 продан в прошлом периоде, а вернулся в этом: в этой неделе у него
# только строка возврата, и выручка за период уходит в минус. На живом кабинете
# это обычная неделя, а не редкость.
RETURN_ROWS = [
    {
        "reportId": 5,
        "rrdId": 51,
        "dateFrom": "2026-09-01",
        "dateTo": "2026-09-07",
        "nmId": 444,
        "vendorCode": "D-4",
        "subjectName": "Кружка",
        "docTypeName": "Продажа",
        "quantity": 5,
        "retailAmount": 10000,
        "retailPriceWithDisc": 2000,
        "forPay": 7000,
        "vw": 1500,
    },
    {
        "reportId": 5,
        "rrdId": 52,
        "dateFrom": "2026-09-01",
        "dateTo": "2026-09-07",
        "nmId": 555,
        "vendorCode": "E-5",
        "subjectName": "Ложка",
        "docTypeName": "Возврат",
        "quantity": 1,
        "retailAmount": 3000,
        "retailPriceWithDisc": 3000,
        "forPay": -2100,
        "vw": -450,
    },
]


@pytest.fixture
def return_seller(db_path):
    """Кабинет, где у одного товара за период только возврат."""
    client_id = db.admin_repo(db_path).ensure_client(7272)
    finance.save_rows(client_id, RETURN_ROWS, path=db_path)
    for week in finance.aggregate(RETURN_ROWS).values():
        finance.save_week(client_id, week, path=db_path)
    costs_module.save_costs(
        client_id, {444: Decimal("200"), 555: Decimal("300")}, path=db_path
    )
    tax.set_rule(client_id, tax.USN_INCOME, 6, path=db_path)
    return client_id


def return_report(client_id, db_path):
    return profit.build(
        client_id, "week", today=TODAY, ads=profit.AdSpend({}), path=db_path
    )


def test_a_negative_tax_is_a_return_and_not_a_payment_from_the_state(
    return_seller, db_path
):
    """Минус в колонке налога объяснён словами, потому что число верное.

    Товар продан в прошлом периоде, а вернулся в этом: выручка за период у него
    отрицательная, и налог выходит с минусом. Расчёт при этом правильный,
    возврат по-настоящему уменьшает налог периода. Отсечь минус в ноль нельзя,
    поэтому чинится подпись, а не число: селлер должен прочитать «возврат
    уменьшил налог», а не «государство мне доплатит».

    Числа руками: выручка 444 это 10000, налог 600. У 555 выручка -3000, налог
    -180. Доходы периода 7000, налог кабинета 420, и сумма строк та же.
    """
    report = return_report(return_seller, db_path)
    allocation = report.tax_by_article

    assert allocation.of(444) == Decimal("600.00")
    assert allocation.of(555) == Decimal("-180.00")
    assert allocation.negative == (555,)
    assert allocation.negative_total == Decimal("-180.00")

    sheet = articles_sheet(report)
    note = str(by_label(sheet)[profit.TAX_NOTE_LABEL].values[1])
    assert "налог с минусом" in note
    assert "возврат уменьшил налог" in note
    assert "не доплату вам от государства" in note
    # Вторая половина той же строки: прибыль после налога выходит больше
    # прибыли, и об этом сказано там же.
    assert "прибыль после налога выходит больше прибыли" in note

    row = articles_by_nm(sheet)[555]
    item_profit = Decimal(str(row.get("Чистая прибыль, ₽")))
    after = Decimal(str(row.get(profit.AFTER_TAX_COLUMN)))
    assert after == item_profit + Decimal("180.00")
    assert after > item_profit

    # И на листе «Методология» та же история остаётся с книгой, с номером
    # артикула: через месяц текста сообщения селлер уже не помнит.
    method = method_text(report)
    assert "Налог с минусом" in method
    assert "555" in method
    assert "не доплата от государства" in method


def test_the_column_still_adds_up_to_the_tax_of_the_cabinet_with_a_minus(
    return_seller, db_path
):
    """Расчёт не тронут: сумма строк с минусом сходится с налогом кабинета.

    Это и есть причина не отсекать минус в ноль. Владелец проверяет таблицу
    сложением, и отсечение развалило бы именно эту проверку: столбец дал бы
    600, а налог кабинета 420.
    """
    report = return_report(return_seller, db_path)
    allocation = report.tax_by_article

    assert report.tax.income == Decimal("7000.00")
    assert report.tax.amount == Decimal("420.00")
    assert allocation.total == Decimal("420.00")
    assert allocation.gap == Decimal("0")
    assert allocation.matches is True
    # Та же сумма сложением столбца в книге, а не только в расчёте.
    rows = articles_by_nm(articles_sheet(report))
    column = [Decimal(str(row.get(profit.TAX_COLUMN))) for row in rows.values()]
    assert sum(column) == Decimal("420.00")
    assert min(column) == Decimal("-180.00")
    # А с отсечением минуса в ноль столбец перестал бы сходиться. Число ниже
    # это то, что селлер увидел бы вместо налога кабинета.
    assert sum(value for value in column if value > 0) == Decimal("600.00")


def test_without_a_minus_the_book_says_nothing_about_it(return_seller, db_path):
    """Отрицательной строки нет: про минус не говорится ни слова.

    Постоянное предупреждение про случай, которого в отчёте нет, читать
    перестанут, а вместе с ним перестанут читать и остальные пояснения.
    """
    whole = return_report(return_seller, db_path)
    # У отчёта остался только проданный товар, возврата в нём нет.
    report = replace(whole, articles=whole.articles[:1])
    allocation = report.tax_by_article

    assert allocation.negative == ()
    note = str(by_label(articles_sheet(report))[profit.TAX_NOTE_LABEL].values[1])
    assert "налог с минусом" not in note
    assert "возврат уменьшил налог" not in note
    assert "Налог с минусом" not in method_text(report)


def test_revenue_without_an_article_is_not_passed_off_as_rounding():
    """Предел округления не растёт вместе с числом товаров.

    Прежний предел был копейка на артикул, то есть при двух сотнях товаров два
    рубля: настоящая нехватка налога в столбце объяснялась селлеру словом
    «округление». Теперь считается то, чем разница объясняется на самом деле:
    налог с выручки, у которой нет артикула, и половина копейки на строку,
    которая правда округлялась.

    Числа руками: двести товаров по 50 рублей выручки это 10000, а доходы
    периода 10030: тридцать рублей Wildberries по товарам не разнёс. Налог с
    них 1,80, и ровно столько не хватает в столбце.
    """
    rule = tax.Rule(mode=tax.USN_INCOME, rate=Decimal("6"))
    estimate = tax.Estimate(rule=rule, income=Decimal("10030"))
    bases = {index: Decimal("50") for index in range(200)}
    allocation = tax.allocate(estimate, bases)

    assert allocation.gap == Decimal("1.80")
    # Прежний предел эту разницу пропускал, и это была уверенная неправда.
    assert abs(allocation.gap) <= Decimal("0.01") * len(bases)
    # А теперь она округлением не называется, потому что ею не является.
    assert allocation.rounding is False
    # Причина названа точно: выручка без артикула и налог с неё.
    assert allocation.uncovered == Decimal("30.00")
    assert allocation.uncovered_tax == Decimal("1.80")
    assert allocation.explained is True
    assert allocation.residue == Decimal("0")
    # Предел считается по строкам, которые округлялись, а не по числу товаров.
    assert allocation.rounded_rows == 200


def test_a_difference_nothing_explains_is_not_explained_away(
    rounding_seller, db_path
):
    """Разницу, которую не объясняют ни выручка без артикула, ни округление,
    бот признаёт необъяснённой.

    Такого на живом кабинете быть не должно, и именно поэтому бот обязан
    сказать «не знаю», а не выбрать из двух готовых объяснений то, которое
    ближе: придуманное объяснение хуже признанного незнания. Проверяется
    лишним артикулом в отчёте: сумма выручки по товарам больше доходов
    периода, то есть так выглядела бы ошибка сборщика.
    """
    whole = rounding_report(rounding_seller, db_path)
    extra = replace(whole.articles[0], nm_id=999)
    report = replace(whole, articles=whole.articles + (extra,))
    allocation = report.tax_by_article

    # Столбец теперь 300,10 при налоге кабинета 240,06.
    assert allocation.total == Decimal("300.10")
    assert allocation.gap == Decimal("-60.04")
    # Выручкой без артикула это не объясняется: её тут нет вовсе, по товарам
    # выручки больше, чем в базе периода. Отрицательной «обезличкой» разница
    # не прикрывается.
    assert allocation.uncovered == Decimal("0")
    assert allocation.uncovered_tax == Decimal("0")
    assert allocation.rounding is False
    assert allocation.explained is False

    note = str(by_label(articles_sheet(report))[profit.TAX_NOTE_LABEL].values[1])
    assert "больше, чем дают округление строк и выручка без артикула" in note
    assert "бот не знает" in note
    assert "это округление строк до копейки" not in note


def test_on_income_minus_expenses_the_column_is_called_a_spread(seller, db_path):
    """База считается по кабинету, значит по товарам это раскладка.

    Назвать её «налогом этого товара» значило бы соврать уверенным голосом:
    своей базы у товара на этом режиме нет вовсе.

    Числа руками: доходы 13000, расходы это 3850 удержаний минус 200 штрафов
    плюс 3400 себестоимости плюс 400 рекламы, то есть 7450. База 5550, налог
    15% это 832,50. По выручке 8000 и 5000 из 13000 это 512,31 и 320,19.
    """
    tax.set_rule(seller, tax.USN_INCOME_MINUS, 15, path=db_path)
    report = report_of(seller, db_path)
    allocation = report.tax_by_article

    assert report.tax.base == Decimal("5550.00")
    assert report.tax.amount == Decimal("832.50")
    assert allocation.exact is False
    assert allocation.of(111) == Decimal("512.31")
    assert allocation.of(222) == Decimal("320.19")

    sheet = articles_sheet(report)
    # Главное в этом тесте: подпись колонки. Налогом товара она не называется.
    assert profit.TAX_SPREAD_COLUMN in sheet.headers
    assert profit.TAX_COLUMN not in sheet.headers
    assert "раскладка" in profit.TAX_SPREAD_COLUMN

    note = str(by_label(sheet)[profit.TAX_NOTE_LABEL].values[1])
    assert "наша раскладка, а не налог именно этого товара" in note
    assert "по кабинету целиком" in note

    # И основание раскладки названо вместе с причиной не делить по прибыли.
    method = method_text(report)
    assert "наша раскладка" in method
    assert "пропорционально выручке" in method
    assert "По прибыли делить нельзя" in method


def test_a_loss_making_article_does_not_get_a_negative_share_of_the_tax():
    """Отрицательной доли налога не бывает: это была бы бессмыслица.

    Делить кабинетную базу по прибыли заманчиво, но у убыточного товара доля
    вышла бы отрицательной, то есть налог в минус, а при неположительной
    прибыли по кабинету доли не существует вовсе, хотя налог платится. Поэтому
    делим по выручке, а товар без выручки доли не получает.
    """
    rule = tax.Rule(mode=tax.USN_INCOME_MINUS, rate=Decimal("15"))
    estimate = tax.Estimate(rule=rule, income=Decimal("1000"), expenses=Decimal("400"))
    allocation = tax.allocate(
        estimate, {1: Decimal("1000"), 2: Decimal("0"), 3: Decimal("-50")}
    )

    assert estimate.amount == Decimal("90.00")
    assert allocation.of(1) == Decimal("90.00")
    assert allocation.of(2) == Decimal("0")
    assert allocation.of(3) == Decimal("0")
    assert min(allocation.parts.values()) >= Decimal("0")

    # А если выручки нет ни у кого, раскладывать не на что вовсе: налог
    # остаётся целиком в разнице, и книга её называет, а не делит ноль на ноль.
    nothing = tax.allocate(estimate, {1: Decimal("0"), 2: Decimal("-10")})
    assert nothing.total == Decimal("0")
    assert nothing.gap == Decimal("90.00")


def test_without_a_mode_there_are_no_tax_columns_at_all(seller, db_path):
    """Режим не выбран: колонок налога в книге нет вовсе, и нулей тоже.

    Это то же свойство, которое тест стерёг и до появления колонок: пустая
    колонка у каждого товара хуже отсутствующей, а ноль в ней прочитался бы
    как «налога нет», хотя мы его просто не считали. Поэтому колонки не
    скрываются и не заполняются нулями, а не заводятся.
    """
    report = report_of(seller, db_path)
    sheet = articles_sheet(report)

    assert report.tax is None
    assert report.tax_by_article is None
    assert profit.tax_columns(report) == ()
    # Ни одной колонки налога: ни точной, ни раскладки, ни прибыли после него.
    # Проверка по началу заголовка, а не по подстроке «налог»: её содержит и
    # «Маржинальность до налога, %», а она про налог ничего не считает.
    assert [header for header in sheet.headers if str(header).startswith("Налог")] == []
    for header in (profit.TAX_COLUMN, profit.TAX_SPREAD_COLUMN, profit.AFTER_TAX_COLUMN):
        assert header not in sheet.headers
    # Лишних клеток в строках тоже нет: ширина строки равна числу заголовков.
    assert all(len(row.values) == len(sheet.headers) for row in sheet.rows)

    rows = by_label(sheet)
    cell = rows[profit.TAX_OFF_LABEL].get(profit.PROFIT_COLUMN)
    assert cell == profit.BEFORE_TAX
    assert cell != 0
    note = str(rows[profit.TAX_OFF_LABEL].values[1])
    assert "не выбран" in note
    assert "/settings" in note
    # Прибыль в итоге при этом на месте и ни на что не поделена.
    assert rows[profit.TOTAL_LABEL].get(profit.PROFIT_COLUMN) == 5350


def test_the_money_column_of_the_tax_sheet_is_marked_with_the_rouble(seller, db_path):
    """Формат денег в книге стоит на знаке рубля в заголовке."""
    assert [header for header in profit.TAX_HEADERS if xlsx.is_money_header(header)] == [
        "Сумма, ₽"
    ]


# --- экран настроек -----------------------------------------------------------


class FakeMessage:
    def __init__(self):
        self.sent = []

    async def reply_text(self, text, **kwargs):
        self.sent.append((text, kwargs))
        return self


class FakeQuery:
    def __init__(self, data, message):
        self.data = data
        self.message = message
        self.answered = None

    async def answer(self, text=None, **kwargs):
        self.answered = text or ""


class FakeUpdate:
    def __init__(self, telegram_id, message, query=None):
        self.effective_user = SimpleNamespace(id=telegram_id)
        self.effective_message = message
        self.callback_query = query


async def press(telegram_id, data, db_path):
    message = FakeMessage()
    await settings_handler.toggle_callback(
        FakeUpdate(telegram_id, message, FakeQuery(data, message)), None, path=db_path
    )


@pytest.mark.asyncio
async def test_the_settings_button_saves_the_mode_and_then_the_rate(seller, db_path):
    await press(7070, f"{settings_handler.TAX_MODE_PREFIX}{tax.USN_INCOME}", db_path)
    rule = tax.rule_of(seller, path=db_path)
    assert rule.mode == tax.USN_INCOME
    # Режим выбран впервые: ставка встала умолчанием из конфига.
    assert rule.rate == tax.default_rate(tax.USN_INCOME)

    await press(7070, f"{settings_handler.TAX_RATE_PREFIX}1", db_path)
    assert tax.rule_of(seller, path=db_path).rate == Decimal("1")

    # Смена режима не тащит за собой ставку чужого списка.
    await press(
        7070, f"{settings_handler.TAX_MODE_PREFIX}{tax.USN_INCOME_MINUS}", db_path
    )
    rule = tax.rule_of(seller, path=db_path)
    assert rule.mode == tax.USN_INCOME_MINUS
    assert rule.rate == tax.default_rate(tax.USN_INCOME_MINUS)

    await press(7070, settings_handler.TAX_OFF_TOKEN, db_path)
    assert tax.rule_of(seller, path=db_path).known is False


@pytest.mark.asyncio
async def test_a_forged_button_does_not_get_into_the_settings(seller, db_path):
    """Нарисованные кнопки ничего не ограничивают, проверка в функции."""
    tax.set_rule(seller, tax.USN_INCOME, 6, path=db_path)
    for data in (
        f"{settings_handler.TAX_MODE_PREFIX}osno",
        f"{settings_handler.TAX_MODE_PREFIX}",
        f"{settings_handler.TAX_RATE_PREFIX}200",
        f"{settings_handler.TAX_RATE_PREFIX}-6",
        f"{settings_handler.TAX_RATE_PREFIX}0",
        f"{settings_handler.TAX_RATE_PREFIX}шесть",
    ):
        await press(7070, data, db_path)

    rule = tax.rule_of(seller, path=db_path)
    assert rule.mode == tax.USN_INCOME
    assert rule.rate == Decimal("6")


@pytest.mark.asyncio
async def test_the_settings_screen_shows_the_mode_and_the_buttons(seller, db_path):
    message = FakeMessage()
    await settings_handler.settings_command(
        FakeUpdate(7070, message), None, path=db_path
    )
    text, kwargs = message.sent[0]
    assert "режим не выбран" in text
    codes = [
        button.callback_data
        for row in kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert f"{settings_handler.TAX_MODE_PREFIX}{tax.USN_INCOME}" in codes
    # Кнопок ставки до выбора режима нет: выбирать не из чего.
    assert not any(code.startswith(settings_handler.TAX_RATE_PREFIX) for code in codes)

    tax.set_rule(seller, tax.USN_INCOME, 6, path=db_path)
    message = FakeMessage()
    await settings_handler.settings_command(
        FakeUpdate(7070, message), None, path=db_path
    )
    text, kwargs = message.sent[0]
    assert "УСН «доходы»" in text and "6%" in text
    codes = [
        button.callback_data
        for row in kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    # Кнопки ставок рисуются из того же белого списка, по которому их и
    # принимают: разойтись им негде.
    assert [
        code.removeprefix(settings_handler.TAX_RATE_PREFIX)
        for code in codes
        if code.startswith(settings_handler.TAX_RATE_PREFIX)
    ] == [str(value) for value in tax.RATE_CHOICES[tax.USN_INCOME]]


def test_the_seller_reads_words_and_not_field_names(seller, db_path):
    """Читатель это селлер с телефона, а не бухгалтер и не программист."""
    tax.set_rule(seller, tax.USN_INCOME, 6, path=db_path)
    text = handlers_profit.summary_text(report_of(seller, db_path))

    assert "заплатил покупатель" in text
    for machine in ("retailAmount", "forPay", "usn_income", "Decimal"):
        assert machine not in text
