"""Агент 1, финансист: недельная раскладка, дедупликация, сверка, Excel.

Швы те же два, что и у всех: транспорт WB (httpx.MockTransport с записанными
ответами, сети нет ни байта) и путь к базе (временный файл).

Ожидаемые числа посчитаны руками по записанному ответу и записаны в
комментариях рядом: считать их тем же способом, что и код, значит не
проверить ничего.
"""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from agents import finance
from bot.handlers import finance as handlers_finance
from core import access, crypto, db, queue, wbapi, xlsx

# Маска из документации: Контент 1, Аналитика 2, Статистика 5, Продвижение 6,
# Финансы 13 - то есть 2 + 4 + 32 + 64 + 8192 = 8294. Посчитана руками.
MASK_FIVE = 8294
EXP = 1789000000


def make_token() -> str:
    """JWT с нужным payload. Подпись не проверяется, она тут не нужна."""

    def part(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    body = {"id": "ab" * 8, "sid": "s" * 8, "exp": EXP, "s": MASK_FIVE, "acc": 3}
    return f"{part({'alg': 'HS256', 'typ': 'JWT'})}.{part(body)}.c2lnbmF0dXJl"


TOKEN = make_token()


class FakeTime:
    """Часы и пауза под контролем теста: 1 запрос в минуту вживую не ждём."""

    def __init__(self) -> None:
        self.now = 1000.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += float(seconds)


# --- записанный ответ WB -----------------------------------------------------
#
# Суммы приходят строками, как и сказано в документации. Три строки одной
# недели: две продажи и один возврат.
#
# Проценты у строк РАЗНЫЕ, и веса (retailPriceWithDisc: 1000, 500, 300, всего
# 1800) подобраны так, что средневзвешенное не совпадает ни с простым
# средним, ни со значением первой строки. Посчитано руками:
#   комиссия  (15*1000 + 24*500 + 15*300) / 1800 = 31500 / 1800 = 17.5
#             простое среднее (15+24+15)/3 = 18, первая строка 15
#   эквайринг (2*1000 + 11*500 + 2*300) / 1800 = 8100 / 1800 = 4.5
#             простое среднее (2+11+2)/3 = 5, первая строка 2
#   СПП       (10*1000 + 28*500 + 10*300) / 1800 = 27000 / 1800 = 15
#             простое среднее (10+28+10)/3 = 16, первая строка 10
#   кВВ       (14*1000 + 23*500 + 14*300) / 1800 = 29700 / 1800 = 16.5
#
# Копейки в ответе тоже есть: 1000.50 продажи и 700.25 к перечислению
# проходят весь путь рубли -> копейки -> рубли -> Excel -> текст.

WEEK = {"reportId": 900, "dateFrom": "2026-09-07", "dateTo": "2026-09-13"}

ROWS = [
    {
        **WEEK,
        "rrdId": 1,
        "rrDate": "2026-09-08",
        "nmId": 111,
        "vendorCode": "ART-1",
        "subjectName": "Кружка",
        "docTypeName": "Продажа",
        "sellerOperName": "Продажа",
        "quantity": 1,
        "retailPrice": "1200",
        "retailAmount": "1000.50",
        "retailPriceWithDisc": "1000",
        "salePercent": 20,
        "spp": 10,
        "commissionPercent": 15,
        "kvwBase": 15,
        "kvw": 14,
        "vw": "150",
        "vwNds": "30",
        "ppvzSalesCommission": "150",
        "ppvzReward": "0",
        "forPay": "700.25",
        "acquiringFee": "20",
        "acquiringPercent": 2,
        "acquiringBank": "Банк",
        "deliveryAmount": 1,
        "returnAmount": 0,
        "deliveryService": "50",
        "rebillLogisticCost": "0",
        "paidStorage": "5",
        "paidAcceptance": "1",
        "penalty": "0",
        "additionalPayment": "0",
        "deduction": "0",
    },
    {
        **WEEK,
        "rrdId": 2,
        "rrDate": "2026-09-09",
        "nmId": 222,
        "vendorCode": "ART-2",
        "subjectName": "Ложка",
        "docTypeName": "Продажа",
        "sellerOperName": "Продажа",
        "quantity": 1,
        "retailPrice": "600",
        "retailAmount": "500",
        "retailPriceWithDisc": "500",
        "salePercent": 15,
        "spp": 28,
        "commissionPercent": 24,
        "kvwBase": 24,
        "kvw": 23,
        "vw": "75",
        "vwNds": "15",
        "ppvzSalesCommission": "75",
        "ppvzReward": "0",
        "forPay": "350",
        "acquiringFee": "10",
        "acquiringPercent": 11,
        "acquiringBank": "Банк",
        "deliveryAmount": 1,
        "returnAmount": 0,
        "deliveryService": "25",
        "rebillLogisticCost": "0",
        "paidStorage": "3",
        "paidAcceptance": "0",
        "penalty": "100",
        "additionalPayment": "0",
        "deduction": "40",
    },
    {
        **WEEK,
        "rrdId": 3,
        "rrDate": "2026-09-10",
        "nmId": 111,
        "vendorCode": "ART-1",
        "subjectName": "Кружка",
        "docTypeName": "Возврат",
        "sellerOperName": "Возврат",
        "quantity": 1,
        "retailPrice": "1200",
        "retailAmount": "300",
        "retailPriceWithDisc": "300",
        "salePercent": 20,
        "spp": 10,
        "commissionPercent": 15,
        "kvwBase": 15,
        "kvw": 14,
        "vw": "0",
        "vwNds": "0",
        "ppvzSalesCommission": "0",
        "ppvzReward": "0",
        "forPay": "-210",
        "acquiringFee": "0",
        "acquiringPercent": 2,
        "acquiringBank": "Банк",
        "deliveryAmount": 0,
        "returnAmount": 1,
        "deliveryService": "0",
        "rebillLogisticCost": "0",
        "paidStorage": "0",
        "paidAcceptance": "0",
        "penalty": "0",
        "additionalPayment": "0",
        "deduction": "0",
    },
]

# Агрегат из sales-reports/list: 700.25 + 350 - 210 = 840.25 к перечислению,
# 1000.50 + 500 + 300 = 1800.50 «реализовал товар». Совпадает с нашим расчётом.
AGGREGATE = [
    {
        "reportId": 900,
        "dateFrom": "2026-09-07",
        "dateTo": "2026-09-13",
        "forPaySum": "840.25",
        "retailAmountSum": "1800.50",
        "deliveryServiceSum": "75",
        "paidStorageSum": "8",
        "paidAcceptanceSum": "1",
        "penaltySum": "100",
        "deductionSum": "40",
    }
]

DETAILED = "/api/finance/v1/sales-reports/detailed"
LIST = "/api/finance/v1/sales-reports/list"


def wb_http(rows=None, aggregate=None, pages=None) -> httpx.AsyncClient:
    """Записанные ответы WB. `pages` отдаёт детализацию по страницам."""
    left = list(pages) if pages is not None else None

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == LIST:
            if isinstance(aggregate, int):
                return httpx.Response(aggregate, json={"title": "нет"})
            return httpx.Response(200, json=AGGREGATE if aggregate is None else aggregate)
        if request.url.path == DETAILED:
            if left is not None:
                return httpx.Response(200, json=left.pop(0) if left else [])
            return httpx.Response(200, json=ROWS if rows is None else rows)
        return httpx.Response(200, json={})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture
def cabinet(db_path, monkeypatch):
    """Подключённый кабинет: клиент в базе и зашифрованный токен рядом."""
    monkeypatch.setenv("ENCRYPTION_KEY", crypto.generate_key())
    # Бюджет запросов к WB общий на процесс: без сброса соседний тест ждал бы
    # чужую паузу.
    wbapi.reset_limits()
    queue.reset()
    client_id = db.admin_repo(db_path).ensure_client(5050)
    db.repo(client_id, db_path).insert(
        "wb_tokens", ciphertext=crypto.encrypt(TOKEN), exp=str(EXP), scopes="finance"
    )
    return client_id


async def collect(client_id, db_path, **kw):
    time = FakeTime()
    return await finance.collect(
        client_id,
        date(2026, 9, 7),
        date(2026, 9, 13),
        path=db_path,
        http=kw.pop("http", None) or wb_http(),
        clock=time.clock,
        sleep=time.sleep,
        **kw,
    )


# --- раскладка недели --------------------------------------------------------


@pytest.mark.asyncio
async def test_week_breakdown_uses_ready_wb_fields(cabinet, db_path):
    """Все статьи недели и проценты - из готовых полей, посчитаны руками."""
    await collect(cabinet, db_path)
    report = finance.build(cabinet, "week", today=date(2026, 9, 15), path=db_path)

    assert len(report.weeks) == 1
    week = report.weeks[0]
    # продажи: 1000.50 + 500, возврат отдельной строкой 300
    assert week.revenue == Decimal("1500.50")
    assert week.returns_amount == Decimal("300")
    # к перечислению: 700.25 + 350 - 210, поле forPay, без пересчёта
    assert week.for_pay == Decimal("840.25")
    # комиссия: vw 150 + 75
    assert week.commission == Decimal("225")
    assert week.acquiring == Decimal("30")  # acquiringFee 20 + 10
    assert week.logistics == Decimal("75")  # deliveryService 50 + 25
    assert week.storage == Decimal("8")  # paidStorage 5 + 3
    assert week.acceptance == Decimal("1")  # paidAcceptance 1 + 0
    assert week.penalties == Decimal("100")
    assert week.deductions == Decimal("40")
    # проценты взяты готовыми полями и взвешены по retailPriceWithDisc:
    # ни простое среднее (18, 5, 16), ни первая строка (15, 2, 10) так не дают
    assert week.commission_percent == Decimal("17.5")
    assert week.acquiring_percent == Decimal("4.5")
    assert week.spp == Decimal("15")
    assert week.kvw == Decimal("16.5")
    # неделя сверена с агрегатом WB и выгружена целиком
    assert week.verified and week.complete and week.checked


@pytest.mark.asyncio
async def test_same_week_downloaded_twice_replaces_rows(cabinet, db_path):
    """R102: повторная выгрузка той же недели заменяет данные, а не двоит."""
    await collect(cabinet, db_path)
    first = db.repo(cabinet, db_path).count("fin_rows")

    await collect(cabinet, db_path)
    assert db.repo(cabinet, db_path).count("fin_rows") == first == 3
    assert db.repo(cabinet, db_path).count("fin_weeks") == 1

    # и суммы недели остались прежними, а не удвоились
    report = finance.build(cabinet, "week", today=date(2026, 9, 15), path=db_path)
    assert report.weeks[0].for_pay == Decimal("840.25")


@pytest.mark.asyncio
async def test_mismatch_with_wb_aggregate_is_shown_not_hidden(cabinet, db_path):
    """R165: расхождение с sales-reports/list видно, а не заметается."""
    lying = [dict(AGGREGATE[0], forPaySum="900")]
    await collect(cabinet, db_path, http=wb_http(aggregate=lying))
    week = finance.build(cabinet, "week", today=date(2026, 9, 15), path=db_path).weeks[0]

    assert not week.checked
    bad = {check.name: check for check in week.mismatches}
    # наши 840.25 против 900 у WB это 59.75 рубля разницы
    assert bad["к перечислению"].diff == Decimal("-59.75")
    assert bad["к перечислению"].theirs == Decimal("900")


@pytest.mark.asyncio
async def test_long_period_is_downloaded_page_by_page(cabinet, db_path):
    """Пагинация по rrdId: год идёт страницами, а не одним куском."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == LIST:
            return httpx.Response(200, json=AGGREGATE)
        body = json.loads(request.content.decode())
        seen.append(body["rrdId"])
        start = {0: 0, 2: 2}.get(body["rrdId"], 3)
        return httpx.Response(200, json=ROWS[start : start + 2])

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await collect(cabinet, db_path, http=http, limit=2)

    # первая страница с курсора 0, вторая с rrdId последней строки
    assert seen == [0, 2]
    assert db.repo(cabinet, db_path).count("fin_rows") == 3


@pytest.mark.asyncio
async def test_empty_period_is_an_empty_report_not_a_crash(cabinet, db_path):
    """Пустой период: ни строк, ни недель, и всё равно понятный отчёт."""
    await collect(cabinet, db_path, http=wb_http(rows=[], aggregate=[]))
    report = finance.build(cabinet, "week", today=date(2026, 9, 15), path=db_path)

    assert report.empty
    assert report.weeks == ()
    assert report.totals.for_pay == Decimal("0")


@pytest.mark.asyncio
async def test_excel_has_four_sheets_and_a_usable_methodology(cabinet, db_path):
    """E3: четыре листа, и «Методология» называет поля и формулы."""
    await collect(cabinet, db_path)
    report = finance.build(cabinet, "week", today=date(2026, 9, 15), path=db_path)

    book = xlsx.read_book(finance.excel_bytes(report))
    assert book.titles == ("Недели", "Месяцы", "По артикулам", "Методология")

    weeks = book["Недели"]
    assert len(weeks.rows) == 1
    assert weeks.rows[0].get("К перечислению, ₽") == 840.25
    assert weeks.rows[0].get("Комиссия WB, %") == 17.5
    assert weeks.rows[0].get("Данные за неделю") == "полные"
    assert "сошлось" in str(weeks.rows[0].get("Сверка с отчётом WB"))

    # свод недель в месяц: одна неделя сентября
    assert book["Месяцы"].rows[0].get("Месяц") == "2026-09"
    # по артикулам: два артикула, сверху тот, что дал больше продаж
    assert [row.get("Артикул WB") for row in book["По артикулам"].rows] == [111, 222]

    method = {row.get("Показатель"): row for row in book["Методология"].rows}
    # по каждой строке видно, какое поле WB и какая формула
    assert method["К перечислению, ₽"].get("Поле ответа WB") == "forPay"
    assert method["Комиссия WB, %"].get("Поле ответа WB") == "commissionPercent"
    assert method["Эквайринг, %"].get("Поле ответа WB") == "acquiringPercent"
    assert method["СПП, %"].get("Поле ответа WB") == "spp"
    assert "retailAmount" in str(method["Продажи, ₽"].get("Формула"))


# --- команда бота ------------------------------------------------------------


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


@pytest.mark.asyncio
async def test_finance_offers_four_periods_and_only_queues_the_work(cabinet, db_path):
    """E1: кнопки периода, ответ сразу, в WB из хендлера никто не ходит."""
    access.grant_access(cabinet, "finance", 30, "test-1", path=db_path)
    telegram_id = int(db.admin_repo(db_path).client(cabinet)["telegram_id"])

    message = FakeMessage()
    await handlers_finance.finance_command(
        FakeUpdate(telegram_id, message), None, path=db_path
    )
    buttons = message.sent[0][1]["reply_markup"].inline_keyboard
    assert [row[0].callback_data for row in buttons] == [
        "fin:week",
        "fin:month",
        "fin:quarter",
        "fin:year",
    ]

    query = FakeQuery("fin:quarter", FakeMessage())
    await handlers_finance.period_chosen(
        FakeUpdate(telegram_id, query.message, query), None, path=db_path
    )
    assert query.answered == "Принято"

    # задача поставлена, а не выполнена: WB подождёт очереди
    tasks = db.admin_repo(db_path).tasks_by_kind(finance.TASK_KIND)
    assert len(tasks) == 1
    assert json.loads(tasks[0]["payload"])["period"] == "quarter"


@pytest.mark.asyncio
async def test_the_same_period_pressed_twice_answers_that_the_work_is_already_going(
    cabinet, db_path
):
    """Вторая такая же задача не ставится, и кнопка не говорит «принято» дважды."""
    access.grant_access(cabinet, "finance", 30, "test-2", path=db_path)
    telegram_id = int(db.admin_repo(db_path).client(cabinet)["telegram_id"])

    answers = []
    for _ in range(2):
        query = FakeQuery("fin:month", FakeMessage())
        await handlers_finance.period_chosen(
            FakeUpdate(telegram_id, query.message, query), None, path=db_path
        )
        answers.append(query.answered)

    assert answers[0] == "Принято"
    assert answers[1] == queue.ALREADY_QUEUED
    assert len(db.admin_repo(db_path).tasks_by_kind(finance.TASK_KIND)) == 1


@pytest.mark.asyncio
async def test_summary_names_every_article_the_seller_asked_for(cabinet, db_path):
    """E2: в сводке все статьи и проценты, без длинных тире и без жаргона."""
    await collect(cabinet, db_path)
    report = finance.build(cabinet, "week", today=date(2026, 9, 15), path=db_path)
    text = handlers_finance.summary_text(report)

    for word in (
        "Продажи",
        "Возвраты",
        "К перечислению",
        "комиссия",
        "эквайринг",
        "логистика",
        "хранение",
        "приёмка",
        "штрафы",
        "прочие удержания",
        "СПП",
    ):
        assert word in text
    assert "840,25" in text and "17,5%" in text and "15%" in text
    # длинные тире в текстах бота запрещены
    assert chr(0x2014) not in text and chr(0x2013) not in text


# --- честность отчёта --------------------------------------------------------


@pytest.mark.asyncio
async def test_truncated_download_is_marked_incomplete_not_passed_off_as_whole(
    cabinet, db_path
):
    """Упёрлись в потолок страниц: неделя неполная, и клиент это видит."""

    # страница 2 строки, потолок 1 страница: страница полная, значит впереди
    # осталось ещё что-то, чего мы не забрали
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == LIST:
            return httpx.Response(200, json=AGGREGATE)
        return httpx.Response(200, json=ROWS[:2])

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await collect(cabinet, db_path, http=http, limit=2, max_pages=1)

    assert result.truncated is True
    assert result.pages == 1
    assert int(result) == 2

    report = finance.build(cabinet, "week", today=date(2026, 9, 15), path=db_path)
    week = report.weeks[0]
    assert week.complete is False
    assert week.trustworthy is False
    assert report.incomplete == (week,)

    book = xlsx.read_book(finance.excel_bytes(report))
    assert "неполные" in str(book["Недели"].rows[0].get("Данные за неделю"))
    assert "неполные" in handlers_finance.summary_text(report)


@pytest.mark.asyncio
async def test_token_error_from_the_aggregate_call_is_not_swallowed(cabinet, db_path):
    """401 обязан долететь до очереди: иначе модули не встанут на паузу."""
    with pytest.raises(wbapi.WBAuthError):
        await collect(cabinet, db_path, http=wb_http(aggregate=401))

    # а 403 это не пауза, но тоже не наше дело глушить
    wbapi.reset_limits()
    with pytest.raises(wbapi.WBForbiddenError):
        await collect(cabinet, db_path, http=wb_http(aggregate=403))


@pytest.mark.asyncio
async def test_unavailable_aggregate_is_survived_but_marked_unverified(cabinet, db_path):
    """Wildberries не отдал итоги: это «сверка не выполнена», а не «сошлось»."""
    result = await collect(cabinet, db_path, http=wb_http(aggregate=503))
    assert result.verified is False
    assert result.rows == 3  # строки сохранены, выгрузка не потеряна

    report = finance.build(cabinet, "week", today=date(2026, 9, 15), path=db_path)
    week = report.weeks[0]
    assert week.verified is False
    assert week.checked is False
    assert report.unverified == (week,)
    assert report.mismatches == ()  # это не расхождение, это отсутствие проверки

    book = xlsx.read_book(finance.excel_bytes(report))
    cell = str(book["Недели"].rows[0].get("Сверка с отчётом WB"))
    assert "сверка не выполнена" in cell and "сошлось" not in cell

    text = handlers_finance.summary_text(report)
    assert "не отдал итоги" in text and "сошлось" not in text


# --- деньги ------------------------------------------------------------------


def test_money_parses_the_edges_of_what_wb_sends():
    """Суммы приходят строками: пустая, с запятой, с минусом и мусор."""
    assert finance.money("") == Decimal("0")
    assert finance.money(None) == Decimal("0")
    assert finance.money("1000.50") == Decimal("1000.50")
    assert finance.money("1 000,50") == Decimal("1000.50")
    assert finance.money("-210") == Decimal("-210")
    assert finance.money("не число") == Decimal("0")
    assert isinstance(finance.money("1000.50"), Decimal)


@pytest.mark.asyncio
async def test_kopecks_survive_the_whole_way(cabinet, db_path):
    """1000.50 и 700.25 проходят WB -> fin_rows -> fin_weeks -> Excel -> текст."""
    await collect(cabinet, db_path)

    row = db.repo(cabinet, db_path).one("fin_rows", rrd_id=1)
    assert row["retail_amount_kop"] == 100050  # копейки целым числом
    assert row["ppvz_for_pay_kop"] == 70025

    week_row = db.repo(cabinet, db_path).one("fin_weeks", report_id=900)
    # 70025 + 35000 - 21000 копеек
    assert week_row["for_pay_kop"] == 84025

    report = finance.build(cabinet, "week", today=date(2026, 9, 15), path=db_path)
    assert report.totals.for_pay == Decimal("840.25")

    book = xlsx.read_book(finance.excel_bytes(report))
    assert book["Недели"].rows[0].get("К перечислению, ₽") == 840.25
    assert "840,25" in handlers_finance.summary_text(report)


# --- периоды и свод в месяцы -------------------------------------------------


def test_period_bounds_cover_every_button_and_the_earliest_data():
    """Границы кнопок и отсечка по 29.01.2024, раньше данных у WB нет."""
    today = date(2026, 9, 15)
    assert finance.period_bounds("week", today) == (date(2026, 9, 8), today)
    assert finance.period_bounds("month", today) == (date(2026, 8, 15), today)
    assert finance.period_bounds("quarter", today) == (date(2026, 6, 15), today)
    assert finance.period_bounds("year", today) == (date(2025, 9, 15), today)

    # год назад от июня 2024 это 2023 год, но данных до 29.01.2024 нет
    assert finance.period_bounds("year", date(2024, 6, 1))[0] == date(2024, 1, 29)

    with pytest.raises(ValueError):
        finance.period_bounds("century")


def august_rows() -> list[dict]:
    """Та же неделя, но августовская: другой отчёт и другой месяц."""
    return [
        dict(row, reportId=901, rrdId=number, dateFrom="2026-08-24", dateTo="2026-08-30")
        for number, row in enumerate(ROWS, start=11)
    ]


@pytest.mark.asyncio
async def test_weeks_of_two_months_are_summed_month_by_month(cabinet, db_path):
    """Лист «Месяцы» это свод недель, а не копия листа «Недели»."""
    both = august_rows() + ROWS
    control = [
        dict(AGGREGATE[0], reportId=901, dateFrom="2026-08-24", dateTo="2026-08-30")
    ]
    await collect(cabinet, db_path, http=wb_http(rows=both, aggregate=control + AGGREGATE))

    report = finance.build(cabinet, "month", today=date(2026, 9, 15), path=db_path)
    assert len(report.weeks) == 2
    assert [month.key for month in report.months] == ["2026-08", "2026-09"]
    # в каждом месяце по одной неделе, и суммы у них одинаковые
    assert [month.weeks for month in report.months] == [1, 1]
    assert report.months[0].for_pay == Decimal("840.25")
    # итог периода это сумма двух недель
    assert report.totals.for_pay == Decimal("1680.50")

    book = xlsx.read_book(finance.excel_bytes(report))
    assert len(book["Месяцы"].rows) == 2


# --- повторная выгрузка ------------------------------------------------------


@pytest.mark.asyncio
async def test_second_download_updates_changed_sums(cabinet, db_path):
    """R102: не только «не двоит», но и «заменяет» - новые суммы доезжают."""
    await collect(cabinet, db_path)

    fixed = [dict(ROWS[0], retailAmount="1100.50", forPay="900.25"), ROWS[1], ROWS[2]]
    fixed_control = [dict(AGGREGATE[0], forPaySum="1040.25", retailAmountSum="1900.50")]
    await collect(cabinet, db_path, http=wb_http(rows=fixed, aggregate=fixed_control))

    assert db.repo(cabinet, db_path).count("fin_rows") == 3
    assert db.repo(cabinet, db_path).one("fin_rows", rrd_id=1)["ppvz_for_pay_kop"] == 90025

    report = finance.build(cabinet, "week", today=date(2026, 9, 15), path=db_path)
    # 900.25 + 350 - 210
    assert report.weeks[0].for_pay == Decimal("1040.25")
    assert report.weeks[0].checked is True


# --- границы, от которых зависит знак недели ---------------------------------


def test_doc_kind_knows_two_types_and_does_not_guess_the_rest():
    """Продажа и возврат узнаются целиком, остальное не угадывается.

    Вхождение слова здесь запрещено намеренно. Обоснование «Добровольная
    компенсация при возврате» содержит «возврат», а документ это продажа: по
    вхождению три штуки уехали бы в возвраты. Обратное вхождение уже стоило
    владельцу неверного `/profit`.
    """
    assert finance.doc_kind("Продажа", "100") == finance.SALE
    assert finance.doc_kind("продажа", 100) == finance.SALE
    assert finance.doc_kind(" Возврат ", "800") == finance.RETURN
    assert finance.doc_kind("ВОЗВРАТ", 800) == finance.RETURN
    # служебная строка: логистика, хранение, удержание. Тип пустой
    assert finance.doc_kind("", 0) == finance.SERVICE
    assert finance.doc_kind(None, 0) == finance.SERVICE
    # незнакомое значение не продажа и не услуга, а именно незнакомое
    assert finance.doc_kind("возврат брака", "100") == finance.UNKNOWN
    assert finance.doc_kind("Сторно продаж", 0) == finance.UNKNOWN
    # обоснование для оплаты типом документа не подменяется
    assert finance.row_kind({"sellerOperName": "Возврат", "retailAmount": "0"}) == (
        finance.SERVICE
    )
    assert (
        finance.row_kind(
            {
                "docTypeName": "Продажа",
                "sellerOperName": "Добровольная компенсация при возврате",
                "retailAmount": "1000",
            }
        )
        == finance.SALE
    )


def test_a_sale_without_a_price_is_a_compensation_and_not_a_sale():
    """Продажа с нулевым retailAmount это возмещение за выбывший товар.

    Решение стоит на типе документа и на числе, а не на свободном тексте
    обоснования: по тексту классифицировать нельзя, это уже проверено строкой
    выше. Зато числу верить можно: покупатель не заплатил ничего, значит товар
    не продан.
    """
    row = {
        "docTypeName": "Продажа",
        "sellerOperName": "Добровольная компенсация при возврате",
        "retailAmount": "0",
        "quantity": 1,
        "forPay": "663",
    }
    assert finance.row_kind(row) == finance.COMPENSATION
    # то же самое и у других обоснований: решает не текст
    assert (
        finance.row_kind(
            {
                "docTypeName": "Продажа",
                "sellerOperName": "Компенсация скидки по программе лояльности",
                "retailAmount": 0,
            }
        )
        == finance.COMPENSATION
    )
    # а возврат с нулевой суммой компенсацией не становится: правило написано
    # про продажу, и расширять его наугад мы не будем
    assert finance.doc_kind("Возврат", 0) == finance.RETURN
    # сумма это обязательный аргумент: забыть её нельзя, именно так и
    # появилась компенсация, посчитанная продажей
    with pytest.raises(TypeError):
        finance.doc_kind("Продажа")


# --- штуки: неделя владельца в миниатюре -------------------------------------
#
# Форма строк списана с живого кабинета, где ошибка и нашлась. Там на 71
# строку продаж приходилось 256 служебных, и `quantity` в служебных было
# проставлено: 257 «штук» при 67 настоящих проданных. Здесь те же виды строк
# в тех же пропорциях, только числа мельче.
#
# Посчитано руками: продано 2 штуки, возвращено 1, ещё одна выбыла по
# компенсации. Старое правило «не возврат значит продажа» дало бы
# 2 + 1 + 3 + 0 + 2 = 8, а правило «продажа это любой docTypeName «Продажа»»
# дало бы 3: компенсация считалась бы продажей товара, которого на остатках
# уже нет. Ровно этот вопрос и задала владелец.
MIXED_WEEK = [
    {
        **WEEK,
        "rrdId": 21,
        "nmId": 111,
        "vendorCode": "ART-1",
        "subjectName": "Кружка",
        "docTypeName": "Продажа",
        "sellerOperName": "Продажа",
        "quantity": 2,
        "retailAmount": "2000",
        "retailPriceWithDisc": "2000",
        "forPay": "1400",
        "vw": "300",
        "deliveryService": "50",
    },
    # Продажа с нулевой ценой: возмещение за товар, который покупатель вернул
    # или потерял. Денег по «реализовал товар» нет, товар выбыл, а 663 рубля
    # пришли в forPay и потеряться не должны.
    {
        **WEEK,
        "rrdId": 22,
        "nmId": 111,
        "vendorCode": "ART-1",
        "subjectName": "Кружка",
        "docTypeName": "Продажа",
        "sellerOperName": "Добровольная компенсация при возврате",
        "quantity": 1,
        "retailAmount": "0",
        "retailPriceWithDisc": "0",
        "forPay": "663",
    },
    {
        **WEEK,
        "rrdId": 23,
        "nmId": 111,
        "vendorCode": "ART-1",
        "subjectName": "Кружка",
        "docTypeName": "Возврат",
        "sellerOperName": "Возврат",
        "quantity": 1,
        "retailAmount": "800",
        "retailPriceWithDisc": "800",
        "forPay": "-560",
    },
    # Дальше служебные строки: тип документа пустой, а quantity проставлено.
    {
        **WEEK,
        "rrdId": 24,
        "docTypeName": "",
        "sellerOperName": "Возмещение издержек по перемещению и операционной обработке товара",
        "quantity": 3,
        "retailAmount": "0",
        "deliveryService": "120",
    },
    {
        **WEEK,
        "rrdId": 25,
        "docTypeName": "",
        "sellerOperName": "Хранение",
        "quantity": 0,
        "retailAmount": "0",
        "paidStorage": "90",
    },
    {
        **WEEK,
        "rrdId": 26,
        "nmId": 111,
        "vendorCode": "ART-1",
        "subjectName": "Кружка",
        "docTypeName": "",
        "sellerOperName": "Возмещение издержек по перевозке",
        "quantity": 2,
        "retailAmount": "0",
        "deliveryService": "60",
    },
]


# Тип документа, которого бот не знает. Такого значения у Wildberries сегодня
# нет, и в этом весь смысл: это завтрашний тип, который нельзя посчитать
# продажей наугад.
STRANGE = {
    **WEEK,
    "rrdId": 31,
    "nmId": 111,
    "vendorCode": "ART-1",
    "subjectName": "Кружка",
    "docTypeName": "Сторно продаж",
    "sellerOperName": "Сторно продаж",
    "quantity": 5,
    "retailAmount": "0",
    "deduction": "500",
}


def report_of(db_path, rows, telegram_id=8080):
    """Отчёт по одной неделе: тот же путь, что у сборщика, только без WB."""
    client_id = db.admin_repo(db_path).ensure_client(telegram_id)
    finance.save_rows(client_id, rows, path=db_path)
    for week in finance.aggregate(rows).values():
        finance.save_week(client_id, week, path=db_path)
    return finance.build(client_id, "week", today=date(2026, 9, 15), path=db_path)


def test_pieces_are_counted_only_where_something_was_sold(db_path):
    """Продажи, возврат и служебные строки вперемешку: штук ровно проданные.

    Это та самая ошибка с кабинета владельца. Служебных строк в отчёте больше,
    чем продаж, и Wildberries проставляет в них `quantity`. Пока «продажей»
    считалось всё, что не возврат, себестоимость в `/profit` считалась по
    восьми штукам вместо трёх.
    """
    report = report_of(db_path, MIXED_WEEK)

    week = report.weeks[0]
    assert (week.sales_count, week.returns_count) == (2, 1)
    # и то же самое в разрезе по артикулам, откуда себестоимость и берётся
    article = {item.nm_id: item for item in report.articles}[111]
    assert (article.sales_count, article.returns_count) == (2, 1)
    # «Штук» в книге это проданное плюс возвращённое, служебных штук там нет,
    # и компенсационной тоже: товар по ней выбыл, а не продался
    assert article.quantity == 3
    # выручка только по строкам продаж, возврат отдельной статьёй
    assert week.revenue == Decimal("2000")
    assert week.returns_amount == Decimal("800")


def test_a_compensation_loses_its_piece_but_not_its_money(db_path):
    """Компенсация не продажа, а 663 рубля всё равно на месте.

    Это и есть вопрос владельца: откуда «продано 1 шт» у товара, которого нет
    на остатках. Штуки больше нет, деньги остались в «к перечислению» и названы
    отдельной статьёй, чтобы поступление при нулевой выручке не выглядело
    подарком из воздуха.
    """
    report = report_of(db_path, MIXED_WEEK, telegram_id=8083)

    week = report.weeks[0]
    assert week.compensation == Decimal("663")
    assert week.compensation_units == 1
    # деньги не прибавлены второй раз: 1400 + 663 - 560 как и было
    assert week.for_pay == Decimal("1503")
    # и выручка от компенсации не выросла
    assert week.revenue == Decimal("2000")

    article = {item.nm_id: item for item in report.articles}[111]
    assert article.compensation == Decimal("663")
    assert article.compensation_units == 1
    # обоснование Wildberries видно селлеру, но вид строки решало не оно
    assert article.compensation_reasons == ("Добровольная компенсация при возврате",)


def test_a_service_row_gives_its_money_and_keeps_its_pieces(db_path):
    """Служебная строка отдаёт логистику и хранение, а штуки не отдаёт.

    Деньги в таких строках настоящие, и потерять их нельзя: логистика 120 и 60
    приходят возмещением издержек, хранение 90 - отдельной строкой без
    артикула. Числа руками: логистика 50 + 120 + 60 = 230, хранение 90.
    """
    report = report_of(db_path, MIXED_WEEK, telegram_id=8081)

    week = report.weeks[0]
    assert week.logistics == Decimal("230")
    assert week.storage == Decimal("90")
    assert week.for_pay == Decimal("1503")  # 1400 + 663 - 560
    # при этом пять служебных штук в продажи не попали
    assert week.sales_count == 2

    # логистика служебной строки с артикулом осталась при артикуле
    article = {item.nm_id: item for item in report.articles}[111]
    assert article.logistics == Decimal("110")  # 50 своя и 60 возмещением
    assert article.sales_count == 2


def test_an_unknown_document_type_does_not_become_a_sale_in_silence(db_path):
    """Незнакомый тип документа: деньги берём, штуки нет, находку называем.

    Угадывать нечем: если завтра Wildberries заведёт новый тип, посчитать его
    продажей значит повторить ту же ошибку молча. Строка отдаёт удержание, её
    штуки не считаются ни продажей, ни возвратом, а сам тип видно в отчёте и
    в журнале.
    """
    report = report_of(db_path, MIXED_WEEK + [STRANGE], telegram_id=8082)

    week = report.weeks[0]
    assert (week.sales_count, week.returns_count) == (2, 1)
    assert week.deductions == Decimal("500")
    assert week.unknown_doc_types == ("Сторно продаж",)
    assert report.unknown_doc_types == ("Сторно продаж",)


def test_tolerance_is_one_rouble_on_the_boundary():
    """Рубль это округление копеек, рубль с копейкой это уже расхождение."""
    assert finance.TOLERANCE == Decimal("1")
    assert finance.Check("к перечислению", Decimal("100"), Decimal("101")).matches
    assert finance.Check("к перечислению", Decimal("102"), Decimal("101")).matches
    assert not finance.Check("к перечислению", Decimal("100"), Decimal("101.01")).matches
    assert not finance.Check("к перечислению", Decimal("102.01"), Decimal("101")).matches


def test_week_saved_without_an_aggregate_is_unverified_but_not_incomplete(
    db_path,
):
    """Пустая колонка сверки: неделя не проверена, но и не обрезана."""
    client_id = db.admin_repo(db_path).ensure_client(6060)
    db.repo(client_id, db_path).insert(
        "fin_weeks",
        report_id=700,
        date_from="2026-09-07",
        date_to="2026-09-13",
        for_pay_kop=50000,
    )

    week = finance.weeks_of(
        client_id, date(2026, 9, 1), date(2026, 9, 30), path=db_path
    )[0]
    assert week.verified is False
    assert week.complete is True
    assert week.checks == ()


@pytest.mark.asyncio
async def test_last_page_exactly_full_is_not_a_false_alarm(cabinet, db_path):
    """Ровно полная последняя страница это штатный конец, а не обрезка.

    Признак обрезки берётся у самой пагинации: «страницы кончились по потолку
    при живом курсоре». Здесь потолок не исчерпан, а курсор повторился, то
    есть читать больше нечего, и неделя обязана остаться полной.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == LIST:
            return httpx.Response(200, json=AGGREGATE)
        cursor = json.loads(request.content.decode())["rrdId"]
        if cursor == 0:
            return httpx.Response(200, json=ROWS[:2])
        # страница снова полная, но последний rrdId тот же самый: курсор
        # исчерпан, дальше идти некуда
        return httpx.Response(200, json=[ROWS[2], ROWS[1]])

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    # потолок ровно 2 страницы и обе полные: арифметика по длинам подняла бы
    # здесь ложную тревогу, факт от пагинации не поднимает
    result = await collect(cabinet, db_path, http=http, limit=2, max_pages=2)

    assert result.pages == 2
    assert result.truncated is False
    report = finance.build(cabinet, "week", today=date(2026, 9, 15), path=db_path)
    assert report.weeks[0].complete is True
    assert report.incomplete == ()
    assert "неполные" not in handlers_finance.summary_text(report)


# Строка без nmId: общее удержание недели, которое Wildberries не привязал
# ни к одному товару. Комиссия по ней тоже есть.
#
# Тип документа пустой, как и приходит с живого кабинета: `docTypeName`
# Wildberries заполняет только у продаж и возвратов, а обоснование лежит
# в `sellerOperName`.
FACELESS = {
    **WEEK,
    "rrdId": 4,
    "rrDate": "2026-09-11",
    "docTypeName": "",
    "sellerOperName": "Удержание",
    "quantity": 0,
    "retailAmount": "0",
    "retailPriceWithDisc": "0",
    "vw": "10",
    "forPay": "0",
    "acquiringFee": "0",
    "deliveryService": "0",
    "paidStorage": "0",
    "paidAcceptance": "0",
    "penalty": "0",
    "additionalPayment": "0",
    "deduction": "15",
}


@pytest.mark.asyncio
async def test_commission_by_article_adds_up_to_the_commission_of_the_week(
    cabinet, db_path
):
    """Комиссия артикулов и комиссия недели это одна и та же величина.

    Основание одно: поле vw. Им же считает комиссию отчёт о прибыльности
    артикулов, поэтому /finance и /profit не могут показать за одну неделю
    две разные комиссии. Строка без nmId не выброшена и не разнесена по
    выручке: она отдельной строкой «без артикула», и итог сходится без
    остатка.
    """
    await collect(cabinet, db_path, http=wb_http(rows=ROWS + [FACELESS]))
    report = finance.build(cabinet, "week", today=date(2026, 9, 15), path=db_path)

    # vw по строкам: 150 + 75 + 0 у продаж и возврата, плюс 10 обезлички
    week = report.weeks[0]
    assert week.commission == Decimal("235")

    by_article = {article.nm_id: article.commission for article in report.articles}
    assert by_article == {111: Decimal("150"), 222: Decimal("75"), None: Decimal("10")}
    assert sum(by_article.values()) == week.commission

    # и то же самое в самой книге, колонка в колонку
    book = xlsx.read_book(finance.excel_bytes(report))
    articles = book["По артикулам"].rows
    assert "без артикула" in [row.get("Артикул WB") for row in articles]
    total = sum(Decimal(str(row.get("Комиссия WB, ₽"))) for row in articles)
    assert total == Decimal(str(book["Недели"].rows[0].get("Комиссия WB, ₽")))

    # на «Методологии» названо поле и сказано, почему именно оно
    method = {row.get("Показатель"): row for row in book["Методология"].rows}
    line = method["Комиссия WB, ₽ по артикулу"]
    assert line.get("Поле ответа WB") == "vw"
    assert "/profit" in str(line.get("Пояснение"))


# --- колонка, которой нечем заполниться --------------------------------------
#
# Неделя владельца в миниатюре. Хранение Wildberries отдал одной строкой по
# кабинету и по товарам не разнёс: колонка «Хранение, ₽» стояла в нуле у
# каждого артикула, и расход выглядел забытым, хотя деньги ушли. Приёмку не
# брали вовсе, и там ноль настоящий: такую колонку убирать не за что.

STORAGE_WEEK = [
    {
        **WEEK,
        "rrdId": 11,
        "nmId": 111,
        "vendorCode": "ART-1",
        "subjectName": "Наматрасник",
        "docTypeName": "Продажа",
        "quantity": 1,
        "retailAmount": "1000",
        "retailPriceWithDisc": "1000",
        "forPay": "700",
        "vw": "150",
        "acquiringFee": "20",
        "deliveryService": "50",
        "paidStorage": "0",
        "paidAcceptance": "0",
        "penalty": "0",
        "deduction": "0",
        "additionalPayment": "0",
    },
    {
        **WEEK,
        "rrdId": 12,
        "nmId": 222,
        "vendorCode": "ART-2",
        "subjectName": "Лежанка",
        "docTypeName": "Продажа",
        "quantity": 1,
        "retailAmount": "500",
        "retailPriceWithDisc": "500",
        "forPay": "350",
        "vw": "75",
        "acquiringFee": "10",
        "deliveryService": "25",
        "paidStorage": "0",
        "paidAcceptance": "0",
        "penalty": "0",
        "deduction": "0",
        "additionalPayment": "0",
    },
    {
        **WEEK,
        "rrdId": 13,
        "docTypeName": "",
        "sellerOperName": "Хранение",
        "quantity": 0,
        "retailAmount": "0",
        "retailPriceWithDisc": "0",
        "forPay": "0",
        "vw": "0",
        "acquiringFee": "0",
        "deliveryService": "0",
        "paidStorage": "600",
        "paidAcceptance": "0",
        "penalty": "0",
        "deduction": "0",
        "additionalPayment": "0",
    },
]


def storage_report(db_path, telegram_id=4242):
    """Отчёт по неделе, в которой хранение пришло строкой без артикула."""
    client_id = db.admin_repo(db_path).ensure_client(telegram_id)
    finance.save_rows(client_id, STORAGE_WEEK, path=db_path)
    for week in finance.aggregate(STORAGE_WEEK).values():
        finance.save_week(client_id, week, path=db_path)
    return finance.build(client_id, "week", today=date(2026, 9, 15), path=db_path)


def test_an_article_column_that_can_never_fill_is_absent_from_the_book(db_path):
    """Хранение пришло без артикула: колонки нулей в листе нет.

    Ноль в каждой строке читается как «не платили», а платили 600 рублей.
    Приёмка в этой же неделе ноль настоящий, и её колонка остаётся: правило
    стоит на данных, а не на списке названий статей.
    """
    book = xlsx.read_book(finance.excel_bytes(storage_report(db_path)))
    headers = book["По артикулам"].headers

    assert "Хранение, ₽" not in headers
    assert "Приёмка, ₽" in headers
    assert "Логистика, ₽" in headers
    # Деньги не потеряны: на листе недели хранение на месте, и строк в листе
    # артикулов ровно столько же, сколько было.
    assert book["Недели"].rows[0].get("Хранение, ₽") == 600
    assert [row.get("Артикул WB") for row in book["По артикулам"].rows] == [
        111,
        222,
        "без артикула",
    ]


def test_the_missing_column_is_explained_with_its_own_amount(db_path):
    """Про эти деньги сказано словами и цифрой, а не просто убрано."""
    book = xlsx.read_book(finance.excel_bytes(storage_report(db_path)))
    method = {row.get("Показатель"): row for row in book["Методология"].rows}

    line = str(method["Чего нет в этом отчёте"].get("Пояснение"))
    assert "хранение" in line
    assert "600.00 ₽" in line
    assert "Недели" in line
    # Общее правило названо отдельной строкой, а не только этим периодом.
    assert "Статьи без разреза по товарам" in method


def test_a_week_where_everything_is_spread_keeps_all_its_columns(db_path):
    """Разнесённая статья колонку не теряет: убирается только неразнесённая."""
    client_id = db.admin_repo(db_path).ensure_client(4343)
    finance.save_rows(client_id, ROWS, path=db_path)
    for week in finance.aggregate(ROWS).values():
        finance.save_week(client_id, week, path=db_path)
    report = finance.build(client_id, "week", today=date(2026, 9, 15), path=db_path)

    book = xlsx.read_book(finance.excel_bytes(report))
    # Хранение здесь Wildberries разнёс по товарам (5 и 3 рубля), и колонка
    # обязана остаться на месте.
    assert "Хранение, ₽" in book["По артикулам"].headers
    assert "Чего нет в этом отчёте" not in {
        row.get("Показатель") for row in book["Методология"].rows
    }


# --- сбор и отправка это две разные задачи -----------------------------------


class Task(SimpleNamespace):
    """Задача очереди в том виде, в каком её видит обработчик."""


@pytest.mark.asyncio
async def test_collect_task_fills_the_weeks_and_says_nothing(
    cabinet, db_path, monkeypatch
):
    """R43: недели появляются сами, без просьбы клиента и без сообщений.

    Пока `fin_weeks` наполняла только команда `/finance`, у молчащего клиента
    новых недель не появлялось, и сторож скрытых расходов молчал вместе с ним.
    """
    sent = []
    finance.set_sender(lambda *args: sent.append(args))

    time = FakeTime()
    http = wb_http()
    real_collect = finance.collect

    async def with_recorded_wb(client_id, date_from, date_to, **kw):
        kw.setdefault("http", http)
        kw.setdefault("clock", time.clock)
        kw.setdefault("sleep", time.sleep)
        return await real_collect(client_id, date_from, date_to, **kw)

    monkeypatch.setattr(finance, "collect", with_recorded_wb)

    result = await finance.collect_task(
        Task(client_id=cabinet, payload={"period": "week"}), path=db_path
    )

    # данные легли в базу: сторожу и прибыльности этого достаточно
    assert result.rows == 3
    assert db.repo(cabinet, db_path).count("fin_weeks") == 1
    assert db.repo(cabinet, db_path).count("fin_rows") == 3
    # и клиенту не сказано ни слова
    assert sent == []


@pytest.mark.asyncio
async def test_report_over_collected_data_sends_without_touching_wb(
    cabinet, db_path, monkeypatch
):
    """Повторная отправка не тянет повторную выгрузку: она дороже всего."""
    await collect(cabinet, db_path)

    def no_wb(*args, **kwargs):
        raise AssertionError("отправка отчёта полезла в Wildberries")

    monkeypatch.setattr(finance, "collect", no_wb)
    monkeypatch.setattr(wbapi, "get_wb_client", no_wb)
    # период фиксируем, чтобы тест не зависел от сегодняшней даты
    monkeypatch.setattr(
        finance,
        "period_bounds",
        lambda period, today=None: (date(2026, 9, 1), date(2026, 9, 30)),
    )

    sent = []
    finance.set_sender(lambda client_id, report, data: sent.append((client_id, report, data)))

    report = await finance.deliver(cabinet, "week", path=db_path)

    assert len(sent) == 1
    client_id, delivered, data = sent[0]
    assert client_id == cabinet
    assert delivered is report
    assert delivered.weeks[0].for_pay == Decimal("840.25")
    assert data[:2] == b"PK"  # книга Excel, собранная целиком из базы


def test_the_two_task_kinds_are_registered_under_their_own_names():
    """Имена видов задач это интерфейс: по ним расписание ставит сбор."""
    queue.reset()
    finance.register_jobs()
    handlers = queue.handlers()

    assert finance.TASK_KIND == "finance_report"
    assert finance.COLLECT_KIND == "finance_collect"
    assert handlers[finance.TASK_KIND] is finance.report_task
    assert handlers[finance.COLLECT_KIND] is finance.collect_task


def test_scheduled_collect_does_not_promise_anything_to_the_client(cabinet, db_path):
    """Клиент этой задачи не просил, значит «принято, пришлю» ему не нужно."""
    said = []
    queue.set_notifier(lambda client_id, text: said.append(text))
    try:
        finance.request_collect(cabinet, "week", path=db_path)
        assert said == []

        finance.request_report(cabinet, "week", path=db_path)
        assert said == [queue.ACCEPTED]
    finally:
        queue.set_notifier(None)

    rows = db.admin_repo(db_path).tasks_by_kind(finance.COLLECT_KIND)
    assert [row["kind"] for row in rows] == [finance.COLLECT_KIND]


# --- чужой текст в сводке ---
#
# Границы недели приезжают от Wildberries строкой и попадают в сообщение с
# разметкой: ими бот называет неделю, по которой сверка не сошлась. Одна
# угловая скобка в такой строке, и Telegram сообщение не примет, то есть
# раскладки клиент не получит вовсе.

TRAP = '<a href="http://zlo.example">нажми</a>'


@pytest.mark.asyncio
async def test_a_week_name_from_wildberries_does_not_become_markup(cabinet, db_path):
    await collect(cabinet, db_path)
    report = finance.build(cabinet, "week", today=date(2026, 9, 15), path=db_path)
    spoiled = replace(report.weeks[0], date_from=TRAP, verified=False)

    text = handlers_finance.summary_text(replace(report, weeks=(spoiled,)))

    assert "<a href" not in text
    assert "&lt;a href=&quot;" in text
    assert "<b>" in text  # разметка самого бота при этом на месте
