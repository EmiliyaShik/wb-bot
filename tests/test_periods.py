"""Периоды отчётов: названный месяц, свои даты и то, что не сломалось.

Почему этот файл появился. Период отсчитывался днями назад от сегодня, а
Wildberries отдаёт отчёты целыми неделями, и в отчёт попадала каждая неделя,
которая отрезок хотя бы задела. «Месяц» в середине октября был не сентябрём, и
свести такой отчёт с бухгалтерией было нельзя.

Теперь рядом живут названные месяцы и ручной период, и считаются они точно по
датам операций. Относительные кнопки остались как были, и это проверяется
здесь же: цифры, которые селлер уже видел, ехать не должны.

Швы те же два, что и у всех: путь к базе (`db_path`) и транспорт WB. Сети в
этом файле нет вовсе, потому что и ходить некуда: строки отчёта кладутся в
базу напрямую тем же `save_rows`, которым их кладёт сборщик.

Числа посчитаны руками и записаны рядом в комментариях.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from telegram.ext import ApplicationHandlerStop

from agents import ads, finance, profit
from bot.handlers import ads as handlers_ads
from bot.handlers import billing as handlers_billing
from bot.handlers import finance as handlers_finance
from bot.handlers import funnel as handlers_funnel
from bot.handlers import period as handlers_period
from bot.handlers import profit as handlers_profit
from core import access, db, queue, xlsx

# День, из которого смотрит каждый тест. Середина октября выбрана нарочно:
# именно в такой день кнопка «последние 31 день» и календарный сентябрь
# расходятся сильнее всего, и ровно на этом владелец поймала бота.
TODAY = date(2026, 10, 15)


def row(
    report_id: int,
    rrd_id: int,
    day: str,
    nm_id: int,
    amount: str,
    *,
    date_from: str,
    date_to: str,
    doc: str = "Продажа",
    quantity: int = 1,
    for_pay: str | None = None,
    logistics: str = "0",
) -> dict:
    """Строка отчёта о реализации в том виде, в каком её отдаёт Wildberries."""
    return {
        "reportId": report_id,
        "rrdId": rrd_id,
        "dateFrom": date_from,
        "dateTo": date_to,
        "rrDate": day,
        "nmId": nm_id,
        "vendorCode": f"ART-{nm_id}",
        "subjectName": "Кружка",
        "docTypeName": doc,
        "sellerOperName": doc,
        "quantity": quantity,
        "retailPrice": amount,
        "retailAmount": amount,
        "retailPriceWithDisc": amount,
        "commissionPercent": 20,
        "kvw": 20,
        "acquiringPercent": 2,
        "spp": 10,
        "vw": "0",
        "forPay": amount if for_pay is None else for_pay,
        "acquiringFee": "0",
        "deliveryService": logistics,
        "paidStorage": "0",
        "paidAcceptance": "0",
        "penalty": "0",
        "additionalPayment": "0",
        "deduction": "0",
    }


# Три недели Wildberries вокруг сентября. Крайние его режут: первая начинается
# 31 августа, последняя кончается 4 октября. Ровно так недели и ложатся на
# месяц в живом кабинете, и ровно поэтому «месяц целыми неделями» это не месяц.
AUGUST_EDGE = {"date_from": "2026-08-31", "date_to": "2026-09-06"}
INSIDE = {"date_from": "2026-09-07", "date_to": "2026-09-13"}
OCTOBER_EDGE = {"date_from": "2026-09-28", "date_to": "2026-10-04"}

ROWS = [
    # неделя 100: один день августа и один сентября
    row(100, 1, "2026-08-31", 111, "1000", **AUGUST_EDGE),
    row(100, 2, "2026-09-02", 111, "500", **AUGUST_EDGE, logistics="50"),
    # неделя 200: целиком внутри сентября
    row(200, 3, "2026-09-09", 222, "300", **INSIDE, logistics="30"),
    # неделя 300: один день сентября и один октября
    row(300, 4, "2026-09-30", 111, "700", **OCTOBER_EDGE, logistics="70"),
    row(300, 5, "2026-10-01", 222, "900", **OCTOBER_EDGE),
]

# Агрегаты недель из sales-reports/list. Суммы по неделям целиком: 1500, 300,
# 1600. Сверять с ними можно только неделю целиком, и в этом вся плата за
# точность.
AGGREGATES = {
    100: {"forPaySum": "1500", "retailAmountSum": "1500", "deliveryServiceSum": "50"},
    200: {"forPaySum": "300", "retailAmountSum": "300", "deliveryServiceSum": "30"},
    300: {"forPaySum": "1600", "retailAmountSum": "1600", "deliveryServiceSum": "70"},
}

# Выручка сентября по датам операций: 500 (02.09) + 300 (09.09) + 700 (30.09).
# День 31.08 и день 01.10 в неё не входят.
SEPTEMBER_REVENUE = Decimal("1500")

# Выручка «последних 31 дня» на 15.10.2026: окно 14.09 - 15.10 задевает только
# неделю 300, и она берётся целиком, вместе с 1 октября: 700 + 900.
RELATIVE_MONTH_REVENUE = Decimal("1600")


@pytest.fixture(autouse=True)
def owners_restored():
    """Реестр владельцев диалога общий на процесс, тест возвращает его как было.

    Иначе подменённый владелец достался бы соседнему тесту, и порядок прогона
    начал бы влиять на результат.
    """
    before = dict(handlers_period._OWNERS)
    yield
    handlers_period._OWNERS.clear()
    handlers_period._OWNERS.update(before)


@pytest.fixture
def cabinet(db_path):
    """Клиент с тремя неделями отчёта в базе. В WB никто не ходит."""
    queue.reset()
    client_id = db.admin_repo(db_path).ensure_client(7070)
    finance.save_rows(client_id, ROWS, path=db_path)
    for report_id, week in finance.aggregate(ROWS).items():
        finance.save_week(client_id, week, AGGREGATES[report_id], path=db_path)
    return client_id


def september(client_id: int, db_path, **kw):
    """Отчёт за сентябрь 2026 названным месяцем."""
    window = finance.month_period(2026, 9, TODAY)
    return finance.build(client_id, window, path=db_path, **kw)


# --- названный месяц ---------------------------------------------------------


def test_named_month_is_exactly_the_calendar_month(cabinet, db_path):
    """Сентябрь это сентябрь: ни дня августа, ни дня октября.

    Проверяется и граница, и сумма. Границу можно было бы угадать правильно, а
    сумму взять целыми неделями, и тогда отчёт назывался бы сентябрём, а считал
    бы 31 августа.
    """
    report = september(cabinet, db_path)

    assert (report.date_from, report.date_to) == (date(2026, 9, 1), date(2026, 9, 30))
    assert report.exact
    assert report.title == "сентябрь 2026"
    # 500 + 300 + 700, посчитано руками по строкам сентября
    assert report.totals.revenue == SEPTEMBER_REVENUE
    # и это не выручка недель целиком: там было бы 1500 + 300 + 1600
    assert report.totals.revenue != Decimal("3400")
    # логистика тоже только сентябрьская: 50 + 30 + 70
    assert report.totals.logistics == Decimal("150")
    # штук: три сентябрьские продажи, а не пять строк недель
    assert report.totals.sales_count == 3


def test_named_month_matches_the_rows_of_that_month(cabinet, db_path):
    """Сумма отчёта сходится со строками этого месяца, посчитанными по базе.

    Второй взгляд на то же число, и намеренно другим способом: отчёт
    складывает недели, а здесь складываются строки `fin_rows` с датой операции
    внутри месяца.
    """
    rows = db.repo(cabinet, db_path).rows_between(
        "fin_rows", "rr_dt", "2026-09-01", "2026-09-30"
    )
    by_hand = sum(
        (db.from_kop(item["retail_amount_kop"]) for item in rows), Decimal("0")
    )

    assert by_hand == SEPTEMBER_REVENUE
    assert september(cabinet, db_path).totals.revenue == by_hand


def test_named_month_counts_articles_by_the_same_dates(cabinet, db_path):
    """Разрез по артикулам живёт в тех же границах, что и весь отчёт."""
    report = september(cabinet, db_path)
    found = {item.nm_id: item.amounts.revenue for item in report.articles}

    # 111: 500 (02.09) + 700 (30.09), без 1000 за 31 августа
    assert found[111] == Decimal("1200")
    # 222: 300 (09.09), без 900 за 1 октября
    assert found[222] == Decimal("300")


def test_the_month_sheet_names_september_not_august(cabinet, db_path):
    """Свод в месяц у точного периода считает по периоду, а не по началу недели.

    Первая неделя сентября началась 31 августа. По правилу «неделя относится к
    месяцу своего начала» отчёт за сентябрь показал бы месяц 2026-08, и это
    читалось бы как ошибка в отчёте.
    """
    report = september(cabinet, db_path)
    assert [month.key for month in report.months] == ["2026-09"]
    assert report.months[0].amounts.revenue == SEPTEMBER_REVENUE


def test_month_buttons_offer_finished_months_only(cabinet, db_path):
    """Кнопки месяцев: последние законченные, свежий первым, текущего нет."""
    months = finance.recent_months(3, today=TODAY)

    assert [item.title for item in months] == [
        "сентябрь 2026",
        "август 2026",
        "июль 2026",
    ]
    # октябрь не предлагается: он ещё не кончился, а кнопка обещает месяц целиком
    assert all(item.date_to < TODAY for item in months)
    assert [(item.date_from.day, item.date_to.day) for item in months] == [
        (1, 30),
        (1, 31),
        (1, 31),
    ]


def test_month_button_label_shows_the_year_only_when_it_differs(cabinet, db_path):
    """Год в подписи нужен там, где без него непонятно."""
    this_year = finance.month_period(2026, 9, TODAY)
    last_year = finance.month_period(2025, 12, TODAY)

    assert handlers_period.month_label(this_year, TODAY) == "Сентябрь"
    assert handlers_period.month_label(last_year, TODAY) == "Декабрь 2025"


def test_month_buttons_stop_where_the_data_starts():
    """Список месяцев кончается там, где кончаются данные Wildberries."""
    # 29.01.2024 это первый день данных отчёта о реализации: месяцев раньше
    # него быть не может, сколько бы кнопок ни просили.
    months = finance.recent_months(24, today=date(2024, 4, 10))
    assert [str(item.date_from) for item in months] == ["2024-03-01", "2024-02-01"]


# --- относительные кнопки считаются как раньше -------------------------------


def test_relative_buttons_still_count_whole_weeks(cabinet, db_path):
    """Кнопка «последние 31 день» считает как считала: днями назад и неделями.

    Это главная проверка того, что ничего не сломано. Относительный период
    точного счёта не получает намеренно: цифры, которые селлер уже видел и
    сверил с кабинетом, ехать не должны.
    """
    report = finance.build(cabinet, "month", today=TODAY, path=db_path)

    # границы те же, что и были: 31 день назад от сегодня
    assert (report.date_from, report.date_to) == (date(2026, 9, 14), TODAY)
    assert finance.period_bounds("month", TODAY) == (date(2026, 9, 14), TODAY)
    assert not report.exact
    assert report.title == "последние 31 день"
    # неделя 300 задела окно и взята целиком, вместе с 1 октября: 700 + 900
    assert [week.report_id for week in report.weeks] == [300]
    assert report.totals.revenue == RELATIVE_MONTH_REVENUE
    # и сверка с итогами Wildberries на месте, как была
    assert report.weeks[0].verified and report.weeks[0].checked
    assert not report.weeks[0].clipped
    assert report.clipped == ()


def test_relative_period_names_say_how_many_days(cabinet, db_path):
    """Подписи относительных кнопок называют число дней, а не «Месяц».

    Кнопка «Месяц» и была той самой ловушкой: рядом с ней теперь стоит
    «Сентябрь», и помнить разницу человек не обязан.
    """
    titles = dict(handlers_finance.BUTTONS)
    assert titles["week"] == "Последние 7 дней"
    assert titles["month"] == "Последние 31 день"
    assert titles["quarter"] == "Последние 92 дня"
    assert titles["year"] == "Последние 365 дней"
    # число в подписи берётся из PERIODS, второго списка дней нет
    assert all(str(days) in titles[name] for name, days in finance.PERIODS.items())


def test_the_funnel_names_days_too_and_keeps_its_own_numbers(cabinet, db_path):
    """У воронки подписи тоже про дни, и числа в них её собственные.

    Названного месяца у воронки нет: период сравнивается с равным периодом до
    него. Но слово «Месяц» на кнопке бот говорить перестал везде, иначе в одном
    отчёте «Месяц» значило бы одно, а в соседнем другое.
    """
    from agents import funnel

    assert dict(handlers_funnel.BUTTONS) == {
        "week": "Последние 7 дней",
        "month": "Последние 30 дней",
        "quarter": "Последние 90 дней",
    }
    # числа в названиях те же, что в PERIODS: второго списка дней нет
    for name, days in funnel.PERIODS.items():
        assert str(days) in funnel.PERIOD_TITLES[name]
        assert str(days) in dict(handlers_funnel.BUTTONS)[name]
    # и точного периода у воронки нет вовсе: кнопки «Свои даты» там не стоит
    assert all("custom" not in key for key, _ in handlers_funnel.BUTTONS)


def test_relative_buttons_of_profit_and_ads_are_unchanged(cabinet, db_path):
    """У прибыли и рекламы относительные кнопки те же самые, что были."""
    assert [key for key, _ in handlers_profit.BUTTONS] == list(profit.PERIODS)
    assert [key for key, _ in handlers_ads.BUTTONS] == list(ads.PERIODS)
    # года у рекламы нет и не появилось
    assert "year" not in dict(handlers_ads.BUTTONS)


def test_profit_relative_period_counts_whole_weeks(cabinet, db_path):
    """Прибыль за относительный период считается по тем же неделям, что и была."""
    report = profit.build(cabinet, "month", today=TODAY, path=db_path)
    assert (report.date_from, report.date_to) == (date(2026, 9, 14), TODAY)
    assert not report.exact
    assert [week.report_id for week in report.weeks] == [300]
    # выручка недели целиком: 700 + 900
    assert sum((item.revenue for item in report.articles), Decimal("0")) == Decimal(
        "1600"
    )


def test_profit_named_month_counts_by_dates(cabinet, db_path):
    """Прибыль за названный месяц считается по датам, как и деньги.

    Один период на два отчёта: иначе сентябрь в прибыли разошёлся бы с
    сентябрём в деньгах, и верить было бы нечему.
    """
    window = finance.month_period(2026, 9, TODAY)
    report = profit.build(cabinet, window, path=db_path)

    assert (report.date_from, report.date_to) == (date(2026, 9, 1), date(2026, 9, 30))
    assert report.exact and report.title == "сентябрь 2026"
    assert sum(
        (item.revenue for item in report.articles), Decimal("0")
    ) == SEPTEMBER_REVENUE


# --- плата за точность: обрезанную неделю сверить нечем -----------------------


def test_clipped_week_is_marked_unchecked_not_matched(cabinet, db_path):
    """Обрезанная неделя помечена «сверить нечем», а не «сошлось».

    У Wildberries готовые итоги есть только на неделю целиком. Молча взять
    сверку целой недели для её части значило бы сверить одно с другим и
    назвать это подтверждением.
    """
    report = september(cabinet, db_path)
    weeks = {week.report_id: week for week in report.weeks}

    # крайние недели обрезаны периодом
    assert weeks[100].clipped and weeks[300].clipped
    # и ни одна из них не «сошлась»: сверка по ним не выполнялась вовсе
    for week in (weeks[100], weeks[300]):
        assert not week.verified
        assert not week.checked
        assert week.checks == ()
        assert not week.trustworthy
    # а неделя внутри периода сверена как обычно
    assert weeks[200].verified and weeks[200].checked and not weeks[200].clipped

    assert [week.report_id for week in report.clipped] == [100, 300]
    # «сверка не выполнена» это другое состояние, и обрезанные недели в него
    # не попадают: подсказка «запросите ещё раз позже» им не подходит
    assert report.unverified == ()


def test_clipped_week_says_so_in_the_message_and_in_the_book(cabinet, db_path):
    """Про обрезанные недели сказано и в сообщении, и в книге."""
    report = september(cabinet, db_path)

    text = handlers_finance.summary_text(report)
    assert "Не целиком вошли в период" in text and "Сверить их нечем" in text
    assert "точно по датам операций" in text

    book = xlsx.read_book(finance.excel_bytes(report))
    cells = {
        row.get("Отчёт WB"): str(row.get("Сверка с отчётом WB"))
        for row in book["Недели"].rows
    }
    assert "сверить нечем" in cells[100]
    assert "сверить нечем" in cells[300]
    assert "сошлось" in cells[200]

    method = [tuple(str(value) for value in row.values) for row in book["Методология"].rows]
    assert any("Как посчитан период" in line[0] for line in method)
    assert any("по датам операций" in " ".join(line) for line in method)


def test_a_whole_week_report_has_nothing_clipped(cabinet, db_path):
    """Период из целых недель обрезанных недель не имеет, и оговорки тоже."""
    # 07.09 - 13.09 это неделя 200 целиком
    window = finance.period_of(
        finance.DATES, TODAY, date_from=date(2026, 9, 7), date_to=date(2026, 9, 13)
    )
    report = finance.build(cabinet, window, path=db_path)

    assert [week.report_id for week in report.weeks] == [200]
    assert report.clipped == ()
    assert report.weeks[0].verified and report.weeks[0].checked
    assert "сверить их нечем" not in handlers_finance.summary_text(report)


def test_exact_period_equals_the_week_when_the_week_is_whole(cabinet, db_path):
    """Точный счёт целой недели даёт то же, что и счёт по агрегату недели.

    Иначе выбор способа менял бы цифру там, где менять её нечему, и два отчёта
    за одну и ту же неделю разошлись бы.
    """
    exact = finance.build(
        cabinet,
        finance.period_of(
            finance.DATES, TODAY, date_from=date(2026, 9, 7), date_to=date(2026, 9, 13)
        ),
        path=db_path,
    )
    weekly = finance.build(cabinet, "week", today=date(2026, 9, 14), path=db_path)

    assert [week.report_id for week in weekly.weeks] == [200]
    assert exact.totals.revenue == weekly.totals.revenue == Decimal("300")
    assert exact.totals.for_pay == weekly.totals.for_pay
    assert exact.weeks[0].commission_percent == weekly.weeks[0].commission_percent


def test_a_row_without_an_operation_date_is_counted_and_named(cabinet, db_path):
    """Строка без даты операции берётся в целую неделю и названа в обрезанной.

    На живом кабинете таких строк нет ни одной, и денег в них нет вовсе. Но
    молча потерять строку нельзя: в обрезанной неделе её некуда положить, и
    отчёт говорит об этом числом.
    """
    extra = row(300, 6, "", 111, "0", **OCTOBER_EDGE, doc="", for_pay="11")
    finance.save_rows(cabinet, [extra], path=db_path)

    exact = september(cabinet, db_path)
    assert exact.undated == 1
    assert "без даты операции" in handlers_finance.summary_text(exact)

    # та же строка в периоде из целых недель посчитана: её неделя вошла вся
    whole = finance.build(
        cabinet,
        finance.period_of(
            finance.DATES, TODAY, date_from=date(2026, 9, 28), date_to=date(2026, 10, 4)
        ),
        path=db_path,
    )
    assert whole.undated == 0
    assert whole.totals.for_pay == Decimal("1611")  # 700 + 900 + 11


# --- кривой и подделанный период ---------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "сентябрь",
        "01.09.2026",
        "",
        "   ",
        "32.13.2026 - 30.09.2026",
        "01.09.2026 - 02.09.2026 - 03.09.2026",
        "0 0 0",
    ],
)
def test_a_crooked_text_period_gets_a_hint_not_a_crash(text):
    """Непонятный ввод это подсказка с примером, а не ошибка и не падение."""
    with pytest.raises(finance.PeriodError) as failure:
        first, last = finance.parse_dates(text)
        finance.period_of(finance.DATES, TODAY, date_from=first, date_to=last)
    assert handlers_period.error_text(failure.value)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("01.09.2026 - 30.09.2026", (date(2026, 9, 1), date(2026, 9, 30))),
        ("1.9.2026-30.9.2026", (date(2026, 9, 1), date(2026, 9, 30))),
        ("01.09.2026 30.09.2026", (date(2026, 9, 1), date(2026, 9, 30))),
        ("2026-09-01 - 2026-09-30", (date(2026, 9, 1), date(2026, 9, 30))),
        ("период с 1.9.26 по 30.9.26", (date(2026, 9, 1), date(2026, 9, 30))),
    ],
)
def test_dates_are_read_the_way_people_write_them(text, expected):
    """Период принимается по-человечески, а не одним заученным видом."""
    assert finance.parse_dates(text) == expected


@pytest.mark.parametrize(
    "first, last, reason",
    [
        (date(2026, 9, 30), date(2026, 9, 1), finance.BAD_ORDER),
        (date(2020, 1, 1), date(2021, 1, 1), finance.TOO_EARLY),
        (date(2027, 1, 1), date(2027, 2, 1), finance.TOO_LATE),
        (date(2024, 2, 1), date(2026, 9, 30), finance.TOO_LONG),
    ],
)
def test_impossible_periods_are_refused_with_a_reason(first, last, reason):
    """Границы проверяются в функции, и у отказа есть причина для человека."""
    with pytest.raises(finance.PeriodError) as failure:
        finance.period_of(finance.DATES, TODAY, date_from=first, date_to=last)
    assert failure.value.reason == reason
    assert handlers_period.error_text(failure.value)


def test_the_edges_of_a_period_are_trimmed_not_refused():
    """Край периода подрезается: данных раньше 29.01.2024 и позже сегодня нет."""
    early = finance.period_of(
        finance.DATES, TODAY, date_from=date(2024, 1, 1), date_to=date(2024, 3, 1)
    )
    assert early.date_from == finance.DATA_SINCE

    future = finance.period_of(
        finance.DATES, TODAY, date_from=date(2026, 10, 1), date_to=date(2026, 12, 31)
    )
    assert future.date_to == TODAY
    # подрезанный период уже не называется месяцем: октябрь ещё не кончился
    assert future.title == "свои даты"


@pytest.mark.parametrize(
    "data",
    [
        "d:2016-01-01:2026-10-15",  # десять лет: предел не обойти
        "d:2026-13-45:2026-09-30",  # такого месяца и дня нет
        "d:2026-09-01",  # дат не две
        "d:2026-09-30:2026-09-01",  # конец раньше начала
        "d:2026-09-01:2026-09-30:2026-10-01",  # лишняя часть
        "d:;DROP TABLE:fin_rows",
        "d::",
        "dates",
    ],
)
def test_a_forged_button_period_is_not_counted(data):
    """Подделанный `callback_data` не считается: период проверяет функция.

    Кнопки рисуем мы, но данные присылает клиент, и нарисованные кнопки его
    клиент Telegram ничем не ограничивают.
    """
    assert handlers_period.period_of_callback(data, today=TODAY) is None


@pytest.mark.asyncio
async def test_a_forged_period_neither_crashes_nor_queues_work(cabinet, db_path):
    """Подделанная кнопка не роняет бота и не ставит работу в очередь.

    А значит, и у Wildberries по такому периоду ничего не просится: в WB ходит
    только обработчик задачи, а задачи нет.
    """
    access.grant_access(cabinet, "finance", 30, "test-forged", path=db_path)
    telegram_id = int(db.admin_repo(db_path).client(cabinet)["telegram_id"])

    for data in ("fin:d:2016-01-01:2026-10-15", "fin:d:мусор", "fin:everything"):
        message = FakeMessage()
        query = FakeQuery(data, message)
        await handlers_finance.period_chosen(
            FakeUpdate(telegram_id, message, query), FakeContext(), path=db_path
        )

    assert db.admin_repo(db_path).tasks_by_kind(finance.TASK_KIND) == []


@pytest.mark.asyncio
async def test_a_forged_period_answers_with_a_hint(cabinet, db_path):
    """На подделанный период бот отвечает подсказкой, а не молчит и не падает."""
    telegram_id = int(db.admin_repo(db_path).client(cabinet)["telegram_id"])
    message = FakeMessage()
    query = FakeQuery("fin:d:2016-01-01:2026-10-15", message)

    await handlers_finance.period_chosen(
        FakeUpdate(telegram_id, message, query), FakeContext(), path=db_path
    )
    assert "/finance" in message.sent[0][0]


def test_a_task_with_an_impossible_period_asks_wb_for_nothing():
    """Задача с невозможным периодом не превращается в запрос к Wildberries.

    Payload задачи пишем мы сами, но строка в базе может оказаться старой или
    поправленной руками. Тогда работа честно падает на разборе периода, а не
    идёт выгружать у Wildberries десять лет.
    """
    task = SimpleNamespace(
        client_id=1, payload={"period": "dates", "from": "2016-01-01", "to": "2026-10-15"}
    )
    with pytest.raises(finance.PeriodError):
        finance.period_of_task(task)


def test_an_exact_task_keeps_its_dates_whatever_day_the_worker_takes_it():
    """Точный период едет в задаче датами, и назавтра он тот же самый.

    У относительной кнопки «сегодня» подвижно, и это её свойство. Названный
    месяц обязан остаться тем же месяцем, даже если воркер взял работу через
    сутки.
    """
    window = finance.month_period(2026, 9, TODAY)
    assert window.payload == {
        "period": "dates",
        "from": "2026-09-01",
        "to": "2026-09-30",
    }
    restored = finance.period_of_task(SimpleNamespace(client_id=1, payload=window.payload))
    assert (restored.date_from, restored.date_to) == (window.date_from, window.date_to)
    assert restored.exact
    # а относительный период в задаче по-прежнему назван именем кнопки
    assert finance.period_of("month", TODAY).payload == {"period": "month"}


def test_the_ads_period_keeps_its_own_shorter_limit():
    """У рекламы свой предел длины, и ручной период его не обходит.

    Года у рекламы нет намеренно: Wildberries отдаёт статистику кусками по 31
    дню при лимите 3 запроса в минуту. Ручной период на год обошёл бы это
    решение молча.
    """
    assert ads.max_days() == finance.PERIODS["quarter"]
    with pytest.raises(finance.PeriodError):
        finance.period_of(
            finance.DATES,
            TODAY,
            date_from=date(2026, 1, 1),
            date_to=date(2026, 10, 1),
            max_days=ads.max_days(),
        )
    # а в деньгах тот же период принимается: там предел другой
    assert finance.period_of(
        finance.DATES, TODAY, date_from=date(2026, 1, 1), date_to=date(2026, 10, 1)
    ).days == 274


# --- очередь и доставка точного периода --------------------------------------


class FakeMessage:
    def __init__(self):
        self.sent = []

    async def reply_text(self, text, **kwargs):
        self.sent.append((text, kwargs))


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


class FakeContext:
    def __init__(self, user_data=None):
        self.user_data = {} if user_data is None else user_data


@pytest.mark.asyncio
async def test_the_month_button_queues_the_month_by_dates(cabinet, db_path):
    """Кнопка месяца ставит в очередь тот же период датами, а не именем."""
    access.grant_access(cabinet, "finance", 30, "test-month", path=db_path)
    telegram_id = int(db.admin_repo(db_path).client(cabinet)["telegram_id"])

    message = FakeMessage()
    query = FakeQuery("fin:d:2026-09-01:2026-09-30", message)
    await handlers_finance.period_chosen(
        FakeUpdate(telegram_id, message, query), FakeContext(), path=db_path
    )

    assert query.answered == "Принято"
    tasks = db.admin_repo(db_path).tasks_by_kind(finance.TASK_KIND)
    assert len(tasks) == 1
    assert json.loads(tasks[0]["payload"]) == {
        "period": "dates",
        "from": "2026-09-01",
        "to": "2026-09-30",
    }


@pytest.mark.asyncio
async def test_custom_dates_are_asked_and_then_queued(cabinet, db_path):
    """«Свои даты»: подсказка с примером, потом период текстом, потом очередь."""
    access.grant_access(cabinet, "finance", 30, "test-custom", path=db_path)
    telegram_id = int(db.admin_repo(db_path).client(cabinet)["telegram_id"])
    handlers_period.set_owner(
        finance.MODULE,
        lambda update, context, window: handlers_finance.dates_chosen(
            update, context, window, path=db_path
        ),
    )
    context = FakeContext()

    ask = FakeMessage()
    await handlers_finance.period_chosen(
        FakeUpdate(telegram_id, ask, FakeQuery("fin:custom", ask)), context, path=db_path
    )
    assert "двумя датами" in ask.sent[0][0]
    assert handlers_period.STATE in context.user_data

    answer = FakeMessage()
    answer.text = "01.09.2026 - 30.09.2026"
    with pytest.raises(ApplicationHandlerStop):
        await handlers_period.text_step(
            FakeUpdate(telegram_id, answer), context
        )

    assert handlers_period.STATE not in context.user_data
    tasks = db.admin_repo(db_path).tasks_by_kind(finance.TASK_KIND)
    assert len(tasks) == 1
    assert json.loads(tasks[0]["payload"])["from"] == "2026-09-01"


@pytest.mark.asyncio
async def test_a_crooked_answer_keeps_the_dialog_and_offers_an_example(cabinet, db_path):
    """Кривой ввод не закрывает диалог: человек правит даты и присылает ещё раз."""
    context = FakeContext({handlers_period.STATE: {"owner": finance.MODULE}})
    message = FakeMessage()
    message.text = "в сентябре"

    with pytest.raises(ApplicationHandlerStop):
        await handlers_period.text_step(FakeUpdate(1, message), context)

    assert handlers_period.STATE in context.user_data
    assert "Не разобрал даты" in message.sent[0][0]
    assert db.admin_repo(db_path).tasks_by_kind(finance.TASK_KIND) == []


# --- два диалога не перехватывают друг друга ---------------------------------


def test_the_two_text_dialogs_live_in_different_groups():
    """Диалог счёта и ввод дат стоят в разных группах, и порядок известен.

    В одной группе телеграм отдаёт сообщение только первому подходящему
    хендлеру, и второй текстовый шаг не получил бы его никогда. В разных
    группах порядок определён: счёт раньше, даты следом.
    """
    assert handlers_billing.DIALOG_GROUP == -10
    assert handlers_period.DIALOG_GROUP == -9
    assert handlers_billing.DIALOG_GROUP < handlers_period.DIALOG_GROUP < 0


def test_handlers_register_their_text_steps_in_those_groups():
    """Группы не только объявлены, но и правда заняты при сборке."""

    class FakeApp:
        def __init__(self):
            self.groups = []

        def add_handler(self, handler, group=0):
            self.groups.append((type(handler).__name__, group))

    app = FakeApp()
    handlers_period.register(app)
    assert ("MessageHandler", handlers_period.DIALOG_GROUP) in app.groups


@pytest.mark.asyncio
async def test_the_invoice_dialog_is_not_hijacked_by_the_date_step(cabinet, db_path):
    """Человек вводит ИНН для счёта: ввод дат его сообщение не берёт.

    Состояния у диалогов разные, и шаг дат чужого не трогает: он молчит и
    пропускает сообщение дальше, как будто его нет.
    """
    context = FakeContext({handlers_billing.STATE: {"module": "finance", "step": "inn"}})
    message = FakeMessage()
    message.text = "7707083893"

    # шаг дат ничего не ответил и цепочку не остановил
    assert await handlers_period.text_step(FakeUpdate(1, message), context) is None
    assert message.sent == []
    assert handlers_billing.STATE in context.user_data


@pytest.mark.asyncio
async def test_the_date_step_is_not_hijacked_by_the_invoice_dialog(cabinet, db_path):
    """Человек вводит даты: диалог счёта его сообщение не берёт.

    Обратная сторона того же: шаг счёта стоит раньше, и если бы он отвечал на
    чужое, до ввода дат сообщение не доехало бы никогда.
    """
    context = FakeContext({handlers_period.STATE: {"owner": finance.MODULE}})
    message = FakeMessage()
    message.text = "01.09.2026 - 30.09.2026"

    # шаг счёта молчит и цепочку не останавливает
    assert (
        await handlers_billing.text_step(FakeUpdate(1, message), context) is None
    )
    assert message.sent == []

    taken = []

    async def remember(update, context, window):
        taken.append(window)

    handlers_period.set_owner(finance.MODULE, remember)
    with pytest.raises(ApplicationHandlerStop):
        await handlers_period.text_step(FakeUpdate(1, message), context)
    assert [item.date_from for item in taken] == [date(2026, 9, 1)]


@pytest.mark.asyncio
async def test_cancel_closes_the_date_dialog(cabinet, db_path):
    """«Отмена» закрывает ввод дат, и обычные сообщения снова идут как обычно."""
    context = FakeContext({handlers_period.STATE: {"owner": finance.MODULE}})
    message = FakeMessage()
    query = FakeQuery(handlers_period.CANCEL, message)

    await handlers_period.cancel_callback(FakeUpdate(1, message, query), context)
    assert handlers_period.STATE not in context.user_data

    plain = FakeMessage()
    plain.text = "123456789"
    assert await handlers_period.text_step(FakeUpdate(1, plain), context) is None
    assert plain.sent == []
