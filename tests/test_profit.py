"""Агент 3, прибыльность артикулов.

Швы те же два, что и у всех: путь к базе (временный файл) и транспорт WB
(`httpx.MockTransport` с записанными ответами, сети нет ни байта). Расчёты -
маржинальность, доля, разнесение обезлички - чистые и проверяются напрямую.

Ожидаемые числа посчитаны руками и записаны рядом в комментариях: считать их
тем же способом, что и код, значит не проверить ничего.
"""

from __future__ import annotations

import base64
import json
from datetime import date
from decimal import Decimal

import httpx
import pytest

from types import SimpleNamespace

from agents import finance, profit
from bot.handlers import profit as handlers_profit
from core import costs as costs_module
from core import crypto, db, queue, wbapi, xlsx

TODAY = date(2026, 9, 7)

# Одна неделя отчёта о реализации в том виде, в каком её отдаёт Wildberries.
# Строка без nmId это обезличенное удержание, ради него и придумано правило
# разнесения. Строка «Возврат» здесь тоже не для красоты: без неё выручка и
# выручка за вычетом возвратов неотличимы, и ошибка в базе маржи прошла бы
# мимо теста.
#
# Рядом с `vw` намеренно лежит `ppvzSalesCommission`: это разные величины
# Wildberries, и тест ловит подмену одной другой.
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
        "quantity": 2,
        "retailAmount": 4000,
        "retailPriceWithDisc": 2000,
        "forPay": 3000,
        "vw": 560,
        "ppvzSalesCommission": 500,
        "acquiringFee": 60,
        "deliveryService": 200,
        "paidStorage": 40,
        "paidAcceptance": 10,
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
        "quantity": 1,
        "retailAmount": 1000,
        "retailPriceWithDisc": 1000,
        "forPay": 700,
        "vw": 170,
        "ppvzSalesCommission": 150,
        "acquiringFee": 20,
        "deliveryService": 100,
        "paidStorage": 10,
        "penalty": 30,
    },
    {
        "reportId": 1,
        "rrdId": 3,
        "dateFrom": "2026-09-01",
        "dateTo": "2026-09-07",
        "docTypeName": "Удержание",
        "quantity": 0,
        "deduction": 300,
    },
    {
        "reportId": 1,
        "rrdId": 5,
        "dateFrom": "2026-09-01",
        "dateTo": "2026-09-07",
        "nmId": 111,
        "vendorCode": "A-1",
        "subjectName": "Кружка",
        "docTypeName": "Возврат",
        "quantity": 1,
        "retailAmount": 800,
        "retailPriceWithDisc": 800,
        "vw": -100,
        "ppvzSalesCommission": -90,
    },
]

ADS = {111: Decimal("300"), 222: Decimal("100")}


@pytest.fixture
def seller(db_path):
    """Клиент с неделей агента 1 и загруженной себестоимостью."""
    client_id = db.admin_repo(db_path).ensure_client(5050)
    finance.save_rows(client_id, ROWS, path=db_path)
    for week in finance.aggregate(ROWS).values():
        finance.save_week(client_id, week, path=db_path)
    costs_module.save_costs(
        client_id, {111: Decimal("500"), 222: Decimal("900")}, path=db_path
    )
    return client_id


def report_of(client_id, db_path, **kw):
    ads = kw.pop("ads", profit.AdSpend(dict(ADS)))
    return profit.build(client_id, "week", today=TODAY, ads=ads, path=db_path, **kw)


def article(report, nm_id):
    for item in report.articles:
        if item.nm_id == nm_id:
            return item
    raise AssertionError(f"артикула {nm_id} нет в отчёте")



# --- чистые расчёты ---


def test_margin_is_profit_share_of_net_revenue():
    # 2500 прибыли с 10000 выручки это 25 процентов.
    assert profit.margin(Decimal("2500"), Decimal("10000")) == 25.0


def test_margin_without_revenue_is_unknown_not_zero():
    assert profit.margin(Decimal("-500"), Decimal("0")) is None
    assert profit.margin(Decimal("-500"), Decimal("-10")) is None


def test_share_of_total_profit():
    # 3000 из 12000 это четверть общей прибыли.
    assert profit.share(Decimal("3000"), Decimal("12000")) == 25.0
    assert profit.share(Decimal("3000"), Decimal("0")) is None


def test_unallocated_is_spread_in_proportion_to_revenue():
    # 100 рублей обезлички при выручке 60 и 40 делятся как 60 и 40 процентов.
    parts = profit.spread(Decimal("100"), {1: Decimal("60"), 2: Decimal("40")})
    assert parts == {1: Decimal("60.00"), 2: Decimal("40.00")}


def test_spread_gives_away_everything_it_got():
    # Три равные доли от 100 рублей не делятся нацело: копейка остатка
    # обязана достаться кому-то, иначе рубль исчезнет из отчёта.
    parts = profit.spread(Decimal("100"), {1: Decimal("1"), 2: Decimal("1"), 3: Decimal("1")})
    assert sum(parts.values()) == Decimal("100")


def test_spread_without_revenue_gives_nothing_to_anyone():
    assert profit.spread(Decimal("100"), {1: Decimal("0")}) == {}
    assert profit.spread(Decimal("100"), {}) == {}


# --- шов «база»: полный набор данных ---


def test_full_data_gives_every_line_the_brief_asks_for(seller, db_path):
    report = report_of(seller, db_path)
    item = article(report, 111)

    # Посчитано руками. Продано 2 штуки, одна вернулась: выручка 4000,
    # возвраты 800, значит база 3200, а себестоимость только за одну штуку,
    # 500 рублей. Комиссия 560 - 100 = 460, расходы WB 460 + 60 + 200 + 40 +
    # 10 = 770, реклама 300, обезличка 300 * 3200 / 4200 = 228,57.
    assert item.vendor_code == "A-1"
    assert item.subject == "Кружка"
    assert item.units == 2
    assert item.returns_count == 1
    assert item.revenue == Decimal("4000")
    assert item.returns_amount == Decimal("800")
    assert item.net_revenue == Decimal("3200")
    assert item.cost == Decimal("500")
    assert item.commission == Decimal("460")
    assert item.acquiring == Decimal("60")
    assert item.logistics == Decimal("200")
    assert item.storage == Decimal("40")
    assert item.penalties == Decimal("0")
    assert item.ad_spend == Decimal("300")
    assert item.unallocated == Decimal("228.57")
    # 3200 - 500 - 770 - 300 - 228,57 = 1401,43
    assert item.profit == Decimal("1401.43")
    # 1401,43 / 3200 = 43,7946875 процента
    assert item.margin == 43.79


def test_the_whole_period_adds_up_and_shares_are_counted(seller, db_path):
    report = report_of(seller, db_path)

    # 1000 - 900 - 330 - 100 - 71,43 = -401,43
    assert article(report, 222).profit == Decimal("-401.43")
    # Прибыль периода 1401,43 - 401,43 = 1000.
    assert report.total_profit == Decimal("1000.00")
    # 1401,43 / 1000 = 140,143 процента, у убыточного доля отрицательная.
    assert article(report, 111).share == 140.14
    assert article(report, 222).share == -40.14


def test_article_commission_is_the_same_wb_reward_as_in_the_finance_report(seller, db_path):
    """Комиссия артикула и комиссия недели это одна величина, а не две похожих.

    У Wildberries есть и `vw`, и `ppvzSalesCommission`, и это разные деньги.
    Агент 1 показывает `vw`; если бы прибыль стояла на втором поле, сумма по
    артикулам не сошлась бы с неделей в /finance, и объяснить разницу селлеру
    было бы нечем.
    """
    report = report_of(seller, db_path)
    week = finance.build(seller, "week", today=TODAY, path=db_path).weeks[0]

    # vw строк первого артикула: 560 продажа и -100 возврат.
    assert article(report, 111).commission == Decimal("460")
    assert article(report, 222).commission == Decimal("170")
    # Ни 500, ни 150 из ppvzSalesCommission в отчёт не попали.
    assert week.amounts.commission == Decimal("630")
    assert sum(item.commission for item in report.articles) == week.amounts.commission


def test_articles_are_sorted_from_the_most_profitable_to_the_losses(seller, db_path):
    report = report_of(seller, db_path)
    assert [item.nm_id for item in report.articles] == [111, 222]
    assert [item.nm_id for item in report.top] == [111]
    assert [item.nm_id for item in report.bottom] == [222]
    assert [item.nm_id for item in report.losses] == [222]


# --- штатная нехватка данных: себестоимость, реклама, нулевая выручка ---


def seed(db_path, rows, costs, telegram_id=6060):
    """Клиент с произвольной неделей агента 1 и произвольной себестоимостью."""
    client_id = db.admin_repo(db_path).ensure_client(telegram_id)
    finance.save_rows(client_id, rows, path=db_path)
    for week in finance.aggregate(rows).values():
        finance.save_week(client_id, week, path=db_path)
    if costs:
        costs_module.save_costs(client_id, costs, path=db_path)
    return client_id


def test_row_without_raw_leaves_the_commission_unknown_not_guessed(db_path):
    """Пустой raw это «комиссия неизвестна», а не «возьмём соседнее поле».

    Подстановка ppvz_sales_commission_kop дала бы третье основание комиссии:
    часть недель посчиталась бы иначе, и селлер никогда не узнал бы, какая
    именно.
    """
    client_id = seed(
        db_path, ROWS, {111: Decimal("500"), 222: Decimal("900")}, telegram_id=4444
    )
    db.repo(client_id, db_path).update("fin_rows", {"rrd_id": 2}, raw=None)
    report = report_of(client_id, db_path)
    item = article(report, 222)

    assert item.commission is None
    assert item.commission_known is False
    # 150 рублей из ppvz_sales_commission_kop в отчёт не подставились, и
    # прибыль по такому артикулу не считается: считать её было бы враньём.
    assert item.profit is None
    assert [row.nm_id for row in report.without_commission] == [222]
    # Второй артикул не пострадал.
    assert article(report, 111).commission == Decimal("460")
    assert "комиссия" in handlers_profit.summary_text(report).lower()


def test_article_without_cost_is_marked_and_profit_is_not_counted(db_path):
    # Себестоимость есть только у первого артикула. R168: /profit работает,
    # а второй артикул помечен.
    client_id = seed(db_path, ROWS, {111: Decimal("500")})
    report = report_of(client_id, db_path)

    assert [item.nm_id for item in report.without_cost] == [222]
    assert article(report, 222).profit is None
    assert article(report, 222).margin is None
    assert article(report, 222).share is None
    # Остальное по этому артикулу посчитано: выручка и расходы на месте.
    assert article(report, 222).revenue == Decimal("1000")
    assert article(report, 222).ad_spend == Decimal("100")
    # Отчёт не пустой, и прибыль по первому артикулу посчитана.
    assert article(report, 111).profit == Decimal("1401.43")
    assert report.total_profit == Decimal("1401.43")


def test_without_promotion_category_the_ad_column_says_no_data(seller, db_path):
    report = report_of(
        seller, db_path, ads=profit.AdSpend({}, available=False, reason=profit.ADS_NO_CATEGORY)
    )

    # Ноль рекламы приписал бы селлеру чужую прибыль, поэтому тут None.
    assert article(report, 111).ad_spend is None
    assert report.total_ad_spend is None
    assert report.ads.reason == profit.ADS_NO_CATEGORY
    # Прибыль посчитана без рекламы: 3200 - 500 - 770 - 228,57 = 1701,43.
    assert article(report, 111).profit == Decimal("1701.43")


def test_article_without_revenue_does_not_break_the_report(db_path):
    rows = ROWS + [
        {
            "reportId": 1,
            "rrdId": 4,
            "dateFrom": "2026-09-01",
            "dateTo": "2026-09-07",
            "nmId": 333,
            "vendorCode": "C-3",
            "docTypeName": "Продажа",
            "quantity": 0,
            "retailAmount": 0,
            "paidStorage": 50,
        }
    ]
    client_id = seed(db_path, rows, {333: Decimal("100")}, telegram_id=7070)
    report = report_of(client_id, db_path)
    item = article(report, 333)

    # Ни выручки, ни продаж: хранение 50 рублей это чистый убыток, а
    # маржинальности без выручки не существует.
    assert item.revenue == Decimal("0")
    assert item.profit == Decimal("-50.00")
    assert item.margin is None
    # Доли обезлички артикул без выручки не получает: делить не по чему.
    assert item.unallocated == Decimal("0")


def test_unallocated_expenses_are_spread_and_nothing_is_lost(seller, db_path):
    report = report_of(seller, db_path)
    # 300 рублей удержания без артикула делятся по выручке за вычетом
    # возвратов, 3200 и 1000: 300 * 3200 / 4200 = 228,57 и 300 * 1000 / 4200 =
    # 71,43. Равное деление пополам такую проверку не пройдёт.
    assert report.unallocated == Decimal("300")
    assert article(report, 111).unallocated == Decimal("228.57")
    assert article(report, 222).unallocated == Decimal("71.43")
    assert report.unallocated_left == Decimal("0")


# --- шов «транспорт»: расход рекламы ---

# Маска из документации: Контент 1, Аналитика 2, Статистика 5, Продвижение 6,
# Финансы 13 - то есть 2 + 4 + 32 + 64 + 8192 = 8294. Посчитана руками.
MASK_FIVE = 8294
EXP = 1789000000


def make_token() -> str:
    """JWT с нужным payload. Подпись не проверяется, она тут не нужна."""

    def part(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    body = {"id": "cd" * 8, "sid": "s" * 8, "exp": EXP, "s": MASK_FIVE, "acc": 3}
    return f"{part({'alg': 'HS256', 'typ': 'JWT'})}.{part(body)}.c2lnbmF0dXJl"


CAMPAIGNS = {"adverts": [{"advert_list": [{"advertId": 777}]}]}

# Расход на артикул лежит в days[].apps[].nms[].sum и суммируется по
# площадкам и дням: 500 + 250 за первый день и 300 за второй, итого 1050.
FULLSTATS = [
    {
        "advertId": 777,
        "days": [
            {
                "date": "2026-09-01",
                "apps": [
                    {"appType": 1, "nms": [{"nmId": 111, "sum": 500}]},
                    {"appType": 32, "nms": [{"nmId": 111, "sum": 250}, {"nmId": 222, "sum": 40}]},
                ],
            },
            {"date": "2026-09-02", "apps": [{"appType": 1, "nms": [{"nmId": 111, "sum": 300}]}]},
        ],
    }
]


class FakeTime:
    """Часы и пауза под контролем теста: бюджет запросов к WB не ждём вживую."""

    def __init__(self) -> None:
        self.now = 1000.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += float(seconds)


def wb_http(**overrides) -> httpx.AsyncClient:
    """Записанные ответы WB, разложенные по путям. Сети в тестах нет."""
    answers = {"/adv/v1/promotion/count": CAMPAIGNS, "/adv/v3/fullstats": FULLSTATS}
    answers.update(overrides)

    def handler(request: httpx.Request) -> httpx.Response:
        found = answers.get(request.url.path)
        if isinstance(found, int):
            return httpx.Response(found, json={"title": "нет"})
        return httpx.Response(200, json=found if found is not None else {})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture
def cabinet(db_path, monkeypatch):
    """Подключённый кабинет: клиент в базе и зашифрованный токен рядом."""
    monkeypatch.setenv("ENCRYPTION_KEY", crypto.generate_key())
    wbapi.reset_limits()
    queue.reset()
    client_id = db.admin_repo(db_path).ensure_client(8080)
    db.repo(client_id, db_path).insert(
        "wb_tokens", ciphertext=crypto.encrypt(make_token()), exp=str(EXP), scopes="promotion"
    )
    return client_id


async def ads_of(client_id, db_path, **kw):
    time = FakeTime()
    return await profit.collect_ads(
        client_id,
        date(2026, 9, 1),
        date(2026, 9, 7),
        path=db_path,
        http=kw.pop("http", None) or wb_http(),
        clock=time.clock,
        sleep=time.sleep,
    )


@pytest.mark.asyncio
async def test_ad_spend_per_article_is_summed_over_apps_and_days(cabinet, db_path):
    ads = await ads_of(cabinet, db_path)

    assert ads.available is True
    assert ads.spend == {111: Decimal("1050"), 222: Decimal("40")}


@pytest.mark.asyncio
async def test_ads_without_promotion_category_are_no_data_not_zero(cabinet, db_path):
    ads = await ads_of(cabinet, db_path, http=wb_http(**{"/adv/v1/promotion/count": 403}))

    assert ads.available is False
    assert ads.reason == profit.ADS_NO_CATEGORY
    assert ads.of(111) is None


@pytest.mark.asyncio
async def test_unavailable_fullstats_does_not_break_the_report(cabinet, db_path):
    ads = await ads_of(cabinet, db_path, http=wb_http(**{"/adv/v3/fullstats": 503}))

    assert ads.available is False
    assert ads.reason == profit.ADS_UNAVAILABLE


# --- книга Excel ---


def test_excel_holds_the_full_table_and_names_the_rule(seller, db_path):
    report = report_of(seller, db_path)
    book = xlsx.read_book(profit.excel_bytes(report))

    assert profit.ARTICLES_SHEET in book.titles
    assert profit.METHOD_SHEET in book.titles
    # Полная таблица это все артикулы, а не только топ.
    assert len(book[profit.ARTICLES_SHEET].rows) == 2
    # Правило разнесения обезлички названо словами, а не подразумевается.
    method = "\n".join(
        " ".join(str(value) for value in row.values) for row in book[profit.METHOD_SHEET].rows
    )
    assert "Расходы без артикула" in method
    assert "пропорционально выручке" in method


def test_excel_keeps_a_separate_block_for_losses_and_missing_costs(db_path):
    client_id = seed(db_path, ROWS, {111: Decimal("500")}, telegram_id=9090)
    book = xlsx.read_book(profit.excel_bytes(report_of(client_id, db_path)))

    rows = book[profit.PROBLEMS_SHEET].rows
    kinds = {str(row.values[0]) for row in rows}
    assert profit.PROBLEMS_SHEET in book.titles
    # Убыточных при этой себестоимости нет, а обезличка и артикул без
    # себестоимости в блоке быть обязаны.
    assert "нет себестоимости" in kinds
    assert "обезличка" in kinds


# --- команда бота ---


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
async def test_profit_offers_periods_and_only_queues_the_work(seller, db_path):
    """В WB из хендлера никто не ходит: команда ставит задачу и отвечает."""
    queue.reset()
    telegram_id = int(db.admin_repo(db_path).client(seller)["telegram_id"])

    message = FakeMessage()
    await handlers_profit.profit_command(FakeUpdate(telegram_id, message), None, path=db_path)
    buttons = message.sent[0][1]["reply_markup"].inline_keyboard
    assert [row[0].callback_data for row in buttons] == [
        "profit:week",
        "profit:month",
        "profit:quarter",
        "profit:year",
    ]

    query = FakeQuery("profit:month", FakeMessage())
    await handlers_profit.period_chosen(
        FakeUpdate(telegram_id, query.message, query), None, path=db_path
    )
    assert query.answered == "Принято"

    tasks = db.admin_repo(db_path).tasks_by_kind(profit.TASK_KIND)
    assert len(tasks) == 1
    assert json.loads(tasks[0]["payload"])["period"] == "month"


def test_message_shows_who_feeds_and_who_eats(seller, db_path):
    text = handlers_profit.summary_text(report_of(seller, db_path))

    assert "Кто кормит" in text
    assert "Кто ест" in text
    # Прибыльный артикул в топе, убыточный в антитопе.
    top, _, bottom = text.partition("Кто ест")
    assert "111" in top and "A-1" in top
    assert "222" in bottom and "B-2" in bottom


def test_message_says_out_loud_that_there_is_no_ad_data(seller, db_path):
    text = handlers_profit.summary_text(
        report_of(
            seller,
            db_path,
            ads=profit.AdSpend({}, available=False, reason=profit.ADS_NO_CATEGORY),
        )
    )

    assert "Продвижение" in text
    assert "нет данных" in text


def test_incomplete_week_is_named_by_both_its_dates(db_path):
    """Сторож расходов такие недели пропускает, а тут они посчитаны.

    Чтобы два ответа читались как два взгляда на одну неделю, а не как
    ошибка, неделя должна быть названа датами, а не «некоторые недели».
    """
    client_id = db.admin_repo(db_path).ensure_client(3333)
    finance.save_rows(client_id, ROWS, path=db_path)
    for week in finance.aggregate(ROWS).values():
        finance.save_week(client_id, week, complete=False, path=db_path)
    costs_module.save_costs(client_id, {111: Decimal("500")}, path=db_path)

    text = handlers_profit.summary_text(report_of(client_id, db_path))

    assert "01.09.2026 - 07.09.2026" in text
    assert "сторож" in text.lower()


def test_message_keeps_the_separate_block_for_problems(db_path):
    client_id = seed(db_path, ROWS, {111: Decimal("500")}, telegram_id=1111)
    text = handlers_profit.summary_text(report_of(client_id, db_path))

    # Блок назван теми же словами, что в задании: «убыточные, обезличка,
    # без себестоимости».
    assert "без себестоимости" in text
    assert "обезличка" in text
    assert "/costs" in text
