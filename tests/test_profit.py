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
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from types import SimpleNamespace

from agents import finance, profit
from bot.handlers import profit as handlers_profit
from core import costs as costs_module
from core import audit, config, crypto, db, queue, wbapi, xlsx

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
        # Служебная строка живого WB: тип документа пуст, а quantity
        # при этом проставлено. Имени «Удержание» у неё не бывает.
        "docTypeName": "",
        "quantity": 1,
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


# Подписи строк, которых в листе артикулов быть обязано, а артикулами они не
# являются: итог по кабинету и строка под ним про налог.
SERVICE_LABELS = {
    profit.TOTAL_LABEL,
    profit.TAX_NOTE_LABEL,
    profit.TAX_OFF_LABEL,
}


def article_rows(sheet):
    """Только строки товаров: без итога и без строк налога под ним."""
    return [row for row in sheet.rows if str(row.get("Артикул WB")) not in SERVICE_LABELS]


def totals_row(sheet):
    """Итоговая строка по подписи, а не по месту в листе.

    Под итогом стоят ещё строки (налог и прибыль после него), и `rows[-1]`
    перестал быть итогом. Искать по подписи честнее: лист растёт, а подпись
    та же.
    """
    for row in sheet.rows:
        if str(row.get("Артикул WB")) == profit.TOTAL_LABEL:
            return row
    raise AssertionError("итоговой строки в листе нет")


def labelled_row(sheet, label):
    """Строка листа по подписи в первой колонке."""
    for row in sheet.rows:
        if str(row.get("Артикул WB")) == label:
            return row
    raise AssertionError(f"строки «{label}» в листе нет")



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
    # Полная таблица это все артикулы, а не только топ, плюс итоговая строка и
    # строка налога под ней.
    sheet = book[profit.ARTICLES_SHEET]
    assert len(article_rows(sheet)) == 2
    assert str(totals_row(sheet).values[0]) == profit.TOTAL_LABEL
    # Правило разнесения обезлички названо словами, а не подразумевается.
    method = "\n".join(
        " ".join(str(value) for value in row.values) for row in book[profit.METHOD_SHEET].rows
    )
    assert "Расходы без артикула" in method
    assert "пропорционально выручке" in method


# Неделя владельца в миниатюре: хранение Wildberries отдал одной строкой по
# кабинету, без артикула. Ровно на этом сломалось чтение отчёта: колонка
# «Хранение» стояла в нуле у каждого товара, и хранение выглядело забытым.
FACELESS_STORAGE = [
    {
        "reportId": 7,
        "rrdId": 71,
        "dateFrom": "2026-09-01",
        "dateTo": "2026-09-07",
        "nmId": 111,
        "vendorCode": "A-1",
        "subjectName": "Наматрасник",
        "docTypeName": "Продажа",
        "quantity": 2,
        "retailAmount": 4000,
        "retailPriceWithDisc": 2000,
        "forPay": 3000,
        "vw": 560,
        "acquiringFee": 60,
        "deliveryService": 200,
    },
    {
        "reportId": 7,
        "rrdId": 72,
        "dateFrom": "2026-09-01",
        "dateTo": "2026-09-07",
        "nmId": 222,
        "vendorCode": "B-2",
        "subjectName": "Лежанка для животных",
        "docTypeName": "Продажа",
        "quantity": 1,
        "retailAmount": 1000,
        "retailPriceWithDisc": 1000,
        "forPay": 700,
        "vw": 170,
        "acquiringFee": 20,
        "deliveryService": 100,
    },
    {
        "reportId": 7,
        "rrdId": 73,
        "dateFrom": "2026-09-01",
        "dateTo": "2026-09-07",
        # Хранение приходит служебной строкой: тип пуст, штуки есть.
        "docTypeName": "",
        "quantity": 3,
        "paidStorage": 600,
    },
    {
        "reportId": 7,
        "rrdId": 74,
        "dateFrom": "2026-09-01",
        "dateTo": "2026-09-07",
        # Служебная строка живого WB: тип документа пуст, а quantity
        # при этом проставлено. Имени «Удержание» у неё не бывает.
        "docTypeName": "",
        "quantity": 1,
        "deduction": 300,
    },
]


def test_faceless_expenses_are_broken_down_and_the_parts_add_up(db_path):
    """Обезличка расшифрована, и расшифровка сходится с ней самой.

    Числа руками: хранение 600 и удержание 300 приходят строками без
    артикула, обезличка 900. Если расшифровка разойдётся с обезличкой хоть на
    копейку, селлеру покажут две разные правды об одних и тех же деньгах.
    """
    client_id = seed(
        db_path,
        FACELESS_STORAGE,
        {111: Decimal("500"), 222: Decimal("400")},
        telegram_id=7171,
    )
    report = report_of(client_id, db_path, ads=profit.AdSpend({}))

    parts = {item.key: item.faceless for item in report.faceless_parts}
    assert parts == {"storage": Decimal("600"), "deductions": Decimal("300")}
    assert sum(parts.values(), Decimal("0")) == report.unallocated == Decimal("900")
    # Хранение и удержания Wildberries по товарам не разнёс вовсе, логистику
    # разнёс: правило одно на все статьи и стоит на данных, а не на списке
    # имён в коде.
    assert {item.key for item in report.unshared_items} == {"storage", "deductions"}


def test_a_column_that_can_never_fill_is_absent_and_the_money_is_named(db_path):
    client_id = seed(
        db_path,
        FACELESS_STORAGE,
        {111: Decimal("500"), 222: Decimal("400")},
        telegram_id=7272,
    )
    report = report_of(client_id, db_path, ads=profit.AdSpend({}))
    book = xlsx.read_book(profit.excel_bytes(report))

    headers = book[profit.ARTICLES_SHEET].headers
    # Колонки, которой нечем заполниться, в книге нет: ноль в каждой строке
    # читается как «не платили», а платили.
    assert "Хранение, ₽" not in headers
    assert "Прочие удержания, ₽" not in headers
    # Логистику Wildberries разнёс, её колонка на месте.
    assert "Логистика, ₽" in headers

    # Деньги названы в расшифровке, и сумма расшифровки равна обезличке.
    problems = book[profit.PROBLEMS_SHEET].rows
    named = {
        str(row.values[0]): row.values[3]
        for row in problems
        if str(row.values[0]).startswith("обезличка")
    }
    assert named["обезличка"] == 900
    assert named["обезличка: хранение"] == 600
    assert named["обезличка: прочие удержания"] == 300
    assert named["обезличка: хранение"] + named["обезличка: прочие удержания"] == (
        named["обезличка"]
    )

    # И сказано словами, почему колонки нет.
    method = "\n".join(
        " ".join(str(value) for value in row.values)
        for row in book[profit.METHOD_SHEET].rows
    )
    assert "хранение" in method
    assert "не разнёс по товарам" in method


# --- название товара ---

CARDS_PATH = "/content/v2/get/cards/list"

# Ответ метода карточек. Название пишет сам продавец, поэтому в книгу оно
# едет как есть, а в сообщение бота только через bot.texts.fill.
CARDS = {
    "cards": [
        {"nmID": 111, "title": "Наматрасник на резинке 160х200", "vendorCode": "A-1"},
        {"nmID": 333, "title": "Чужой товар", "vendorCode": "C-3"},
    ],
    "cursor": {"updatedAt": "", "nmID": 333, "total": 2},
}


def counting_http(answers):
    """Транспорт, который считает походы в WB. Сети в тестах нет."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        found = answers.get(request.url.path)
        if isinstance(found, int):
            return httpx.Response(found, json={"title": "нет"})
        return httpx.Response(200, json=found if found is not None else {})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen


async def names_for(client_id, db_path, nm_ids, http):
    time = FakeTime()
    return await profit.collect_names(
        client_id, nm_ids, path=db_path, http=http, clock=time.clock, sleep=time.sleep
    )


def test_product_name_gets_into_the_book_and_a_missing_card_leaves_the_number(
    seller, db_path
):
    """Название товара видно в отчёте, а без карточки остаётся артикул.

    Второго имени у нас нет: выдумать его нельзя, а пустая клетка прочиталась
    бы как потерянные данные.
    """
    profit.remember_names(seller, CARDS["cards"], asked=[111, 222], path=db_path)
    report = report_of(seller, db_path)

    assert article(report, 111).title == "Наматрасник на резинке 160х200"
    assert article(report, 111).display_name == "Наматрасник на резинке 160х200"
    # Карточки 222 у Wildberries нет: товар удалён из кабинета.
    assert article(report, 222).title == ""
    assert article(report, 222).display_name == "222"

    sheet = xlsx.read_book(profit.excel_bytes(report))[profit.ARTICLES_SHEET]
    names = {
        row.get("Артикул WB"): row.get("Название товара")
        for row in article_rows(sheet)
    }
    assert names == {111: "Наматрасник на резинке 160х200", 222: "222"}
    # Предмет остался предметом: это категория, а не название.
    assert sheet.rows[0].get("Предмет") == "Кружка"


@pytest.mark.asyncio
async def test_names_are_asked_once_and_the_answer_is_remembered(cabinet, db_path):
    http, seen = counting_http({CARDS_PATH: CARDS})

    first = await names_for(cabinet, db_path, [111, 222], http)
    second = await names_for(cabinet, db_path, [111, 222], http)

    assert first == {111: "Наматрасник на резинке 160х200", 222: ""}
    assert second == first
    # Второй отчёт в Wildberries не пошёл: про оба артикула уже спрашивали, а
    # удалённый товар не повод дёргать WB на каждом отчёте.
    assert seen == [CARDS_PATH]
    # Новый артикул в периоде отправляет за названиями снова.
    await names_for(cabinet, db_path, [111, 222, 444], http)
    assert seen == [CARDS_PATH, CARDS_PATH]


@pytest.mark.asyncio
async def test_without_content_category_the_report_keeps_the_article_number(
    cabinet, db_path
):
    http, seen = counting_http({CARDS_PATH: 403})

    names = await names_for(cabinet, db_path, [111], http)

    assert names == {}
    assert seen == [CARDS_PATH]
    # Пометки «спрашивали» не осталось: категорию токена селлер может выдать,
    # и тогда названия приедут сами.
    assert profit.names_of(cabinet, path=db_path) == {}
    # Даже у объяснимого отказа есть след: иначе на вопрос «почему у меня нет
    # названий» ответить нечем.
    assert any(
        "Контент" in record["message"]
        for record in audit.recent(limit=10, client_id=cabinet, path=db_path)
    )


@pytest.mark.asyncio
async def test_refusal_of_wildberries_does_not_look_like_a_missing_card(
    cabinet, db_path
):
    """Отказ WB и удалённая карточка снаружи одинаковы, в журнале нет.

    Ровно на этом бот и обжёгся: запрос каталога уходил неверным, WB отвечал
    400, названий не было ни разу, а отчёт выглядел штатным, потому что
    артикул вместо имени это предусмотренный случай.
    """
    # Успешный ответ без нужной карточки: это «карточки нет», и это тишина.
    good, _ = counting_http({CARDS_PATH: CARDS})
    await names_for(cabinet, db_path, [222], good)
    quiet = audit.recent(limit=20, client_id=cabinet, path=db_path)
    assert not [record for record in quiet if record["level"] == "error"]

    # Отказ Wildberries: отчёт по-прежнему не падает, но молчания больше нет.
    bad, seen = counting_http({CARDS_PATH: 400})
    names = await names_for(cabinet, db_path, [999], bad)

    assert names == {}
    assert seen == [CARDS_PATH]
    loud = [
        record
        for record in audit.recent(limit=20, client_id=cabinet, path=db_path)
        if record["level"] == "error"
    ]
    assert loud, "отказ Wildberries обязан быть виден в журнале"
    assert "названия товаров не получены" in loud[0]["message"]
    assert "400" in loud[0]["message"]
    # Артикул, о котором спросили неудачно, не помечен как «карточки нет»:
    # иначе после починки запроса название не приехало бы никогда.
    assert 999 not in profit.names_of(cabinet, path=db_path)


# --- компенсация Wildberries ---
#
# Кабинет владельца в миниатюре. Товар «Свечи новогодние» стоял в отчёте как
# «продано 4 шт при выручке 0,00 ₽», хотя на остатках его давно нет: все четыре
# строки это возмещение за товар, который покупатель вернул или потерял. Тип
# документа у них «Продажа», quantity проставлено, деньги приходят в forPay, а
# retailAmount ровно ноль. Рядом стоит настоящая продажа другого товара, чтобы
# было видно: обычный счёт не поехал.
#
# Посчитано руками: компенсаций 663 + 652 + 645 + 1212 = 3172 рубля за 4 штуки.
COMPENSATED = [
    {
        "reportId": 9,
        "rrdId": 91,
        "dateFrom": "2026-09-01",
        "dateTo": "2026-09-07",
        "nmId": 111,
        "vendorCode": "A-1",
        "subjectName": "Кружка",
        "docTypeName": "Продажа",
        "sellerOperName": "Продажа",
        "quantity": 2,
        "retailAmount": 4000,
        "retailPriceWithDisc": 2000,
        "forPay": 3000,
        "vw": 560,
        "acquiringFee": 60,
        "deliveryService": 200,
    },
] + [
    {
        "reportId": 9,
        "rrdId": 92 + number,
        "dateFrom": "2026-09-01",
        "dateTo": "2026-09-07",
        "nmId": 999,
        "vendorCode": "арт. 19",
        "subjectName": "Свечи",
        "docTypeName": "Продажа",
        "sellerOperName": "Добровольная компенсация при возврате",
        "quantity": 1,
        "retailAmount": 0,
        "retailPriceWithDisc": 0,
        "forPay": amount,
        "vw": 0,
    }
    for number, amount in enumerate((663, 652, 645, 1212))
]

COMPENSATION_COSTS = {111: Decimal("500"), 999: Decimal("454")}


def compensated(db_path, telegram_id=8181):
    client_id = seed(db_path, COMPENSATED, COMPENSATION_COSTS, telegram_id=telegram_id)
    return report_of(client_id, db_path, ads=profit.AdSpend({}))


def test_a_compensation_is_not_a_sold_piece_and_its_money_is_not_lost(db_path):
    """Штук по компенсации нет, а 3172 рубля на месте и названы статьёй.

    Это тот самый вопрос владельца: откуда «продано 4 шт» у товара, которого
    нет на остатках. Продажи товара не было, поэтому штук нет и выручки нет.
    Деньги настоящие, и пропасть они не имеют права.
    """
    report = compensated(db_path)
    candles = article(report, 999)

    assert candles.units == 0
    assert candles.returns_count == 0
    assert candles.revenue == Decimal("0")
    assert candles.compensation == Decimal("3172")
    assert candles.compensation_units == 4
    # Себестоимость выбывшего товара посчитана, но в колонку себестоимости не
    # попала: она считается по проданным штукам, и селлер проверяет её этим
    # умножением.
    assert candles.compensation_cost == Decimal("1816")
    assert candles.cost == Decimal("0")

    assert report.total_compensation == Decimal("3172")
    assert report.total_compensation_units == 4
    assert report.total_compensation_cost == Decimal("1816")
    # Итог по кабинету: продано ровно две штуки настоящей продажи.
    assert report.totals.units == 2
    assert report.totals.compensation == Decimal("3172")
    assert report.totals.compensation_units == 4
    # И те же деньги видны в «к перечислению» недели: 3000 продажи и 3172
    # компенсации.
    assert report.period_amounts.for_pay == Decimal("6172")
    assert report.period_amounts.compensation == Decimal("3172")


def test_a_normal_sale_and_a_return_are_counted_the_way_they_were(seller, db_path):
    """Обычная продажа и возврат от правки не поехали.

    Выручка 4000 и возврат 800 по первому артикулу, 1000 по второму; штуки 2 и
    1 проданных, 1 возвращённая. Числа те же, что были до разбора компенсаций:
    правка касается только продаж без цены.
    """
    report = report_of(seller, db_path)
    first, second = article(report, 111), article(report, 222)

    assert (first.units, first.returns_count) == (2, 1)
    assert first.revenue == Decimal("4000")
    assert first.returns_amount == Decimal("800")
    # Себестоимость по проданным минус возвращённым: 500 * (2 - 1).
    assert first.cost == Decimal("500")
    assert first.compensation == Decimal("0")
    assert first.compensation_cost is None

    assert (second.units, second.returns_count) == (1, 0)
    assert second.revenue == Decimal("1000")
    assert second.cost == Decimal("900")

    assert report.totals.units == 3
    assert report.totals.returns_count == 1
    assert report.totals.revenue == Decimal("5000")
    assert report.totals.returns_amount == Decimal("800")
    assert report.total_compensation == Decimal("0")
    # Компенсаций нет вовсе: ни блока в книге, ни строки в сообщении.
    assert report.compensations == ()
    assert "Компенсации Wildberries" not in handlers_profit.summary_text(report)


def test_the_seller_sees_the_compensation_by_name_and_not_a_vanished_product(db_path):
    """Товар не исчезает из отчёта молча: компенсация названа по имени.

    Если просто убрать штуки, селлер увидит, что товар пропал, и спросит то же
    самое второй раз. Поэтому деньги и причина стоят в блоке листа проблем, а
    итог названы в сообщении.
    """
    report = compensated(db_path, telegram_id=8182)
    book = xlsx.read_book(profit.excel_bytes(report))

    rows = [
        row
        for row in book[profit.PROBLEMS_SHEET].rows
        if str(row.values[0]) == profit.COMPENSATION_BLOCK
    ]
    assert len(rows) == 1
    assert rows[0].get("Артикул WB") == 999
    assert rows[0].get("Сумма, ₽") == 3172
    note = str(rows[0].get("Что это значит"))
    # Сказано и сколько штук выбыло, и куда ушли деньги, и чем Wildberries
    # выплату обосновал.
    assert "4 шт" in note
    assert "к перечислению" in note
    assert "Добровольная компенсация при возврате" in note
    # Себестоимость выбывшего товара названа рядом: 454 * 4 штуки.
    assert "1816.00 ₽" in note

    # Методология объясняет и правило, и решение про себестоимость.
    method = " ".join(
        str(value) for row in book[profit.METHOD_SHEET].rows for value in row.values
    )
    assert "retailAmount" in method
    assert "выбыл, а не продался" in method
    assert "не списывается" in method

    # И то же самое словами в сообщении: селлер читает его раньше файла.
    text = handlers_profit.summary_text(report)
    assert "Компенсации Wildberries: 3 172 ₽ за 4 шт" in text
    assert "выбыл, а не продался" in text
    assert "Себестоимость выбывшего товара 1 816 ₽" in text


# --- незнакомый тип документа -------------------------------------------------


def test_an_unknown_doc_type_is_named_in_the_message_and_in_the_book(db_path):
    """Новый тип документа WB перестал быть делом одного журнала.

    Деньги по такой строке в раскладку входят, а штуки нет. Прежде селлер
    видел прибыль с тихо непосчитанными штуками и не знал, о чём спрашивать:
    запись была только у владельца.
    """
    rows = [dict(row) for row in ROWS]
    rows.append(
        {
            "reportId": 1,
            "rrdId": 9,
            "dateFrom": "2026-09-01",
            "dateTo": "2026-09-07",
            "nmId": 111,
            "vendorCode": "A-1",
            "subjectName": "Кружка",
            "docTypeName": "Передача на реализацию",
            "quantity": 5,
            "retailAmount": 2000,
            "forPay": 1500,
        }
    )
    client_id = seed(
        db_path, rows, {111: Decimal("500"), 222: Decimal("900")}, telegram_id=4747
    )
    report = report_of(client_id, db_path, ads=profit.AdSpend({}))

    assert report.unknown_doc_types == ("Передача на реализацию",)
    # Штуки по такой строке не посчитаны: было 2 проданных, 5 не прибавились.
    assert article(report, 111).units == 2

    text = handlers_profit.summary_text(report)
    assert "Передача на реализацию" in text
    assert "штуки нет" in text

    method = " ".join(
        str(value)
        for row in xlsx.read_book(profit.excel_bytes(report))[
            profit.METHOD_SHEET
        ].rows
        for value in row.values
    )
    assert "Передача на реализацию" in method


def test_a_familiar_week_says_nothing_about_unknown_types(seller, db_path):
    """Разговора на пустом месте нет: все типы знакомы, строки в тексте нет."""
    report = report_of(seller, db_path)

    assert report.unknown_doc_types == ()
    assert "не знает" not in handlers_profit.summary_text(report)


# --- итог по кабинету ---


def test_totals_add_up_what_adds_up_and_leave_percents_alone(seller, db_path):
    """Итоговая строка: деньги складываются, проценты нет.

    Числа руками: выручка 4000 + 1000, возвраты 800, себестоимость 500 + 900,
    комиссия 560 + 170 - 100, прибыль 1401,43 - 401,43. Маржинальность по
    кабинету это 1000 / (3200 + 1000) = 23,81 процента, а не сумма 43,79 и
    минус 40,14.
    """
    report = report_of(seller, db_path)
    totals = report.totals

    assert totals.revenue == Decimal("5000")
    assert totals.returns_amount == Decimal("800")
    assert totals.units == 3
    assert totals.returns_count == 1
    assert totals.cost == Decimal("1400")
    assert totals.commission == Decimal("630")
    assert totals.ad_spend == Decimal("400")
    assert totals.unallocated == Decimal("300")
    assert totals.profit == Decimal("1000.00")
    assert totals.margin == 23.81
    assert totals.complete is True

    row = totals_row(xlsx.read_book(profit.excel_bytes(report))[profit.ARTICLES_SHEET])
    assert row.get("Артикул WB") == profit.TOTAL_LABEL
    assert row.get("Выручка, ₽") == 5000
    assert row.get("Чистая прибыль, ₽") == 1000
    assert row.get(profit.MARGIN_COLUMN) == 23.81
    # Доля в прибыли в итоге это всегда сто процентов, а цена за штуку не
    # сумма: обе клетки говорят это словами, а не числом.
    assert row.get("Доля в прибыли, %") == profit.NOT_SUMMABLE
    assert row.get("Себестоимость за штуку, ₽") == profit.NOT_SUMMABLE


def test_articles_without_profit_do_not_become_zeros_in_the_total(db_path):
    """Артикул без себестоимости не превращается в итоге в ноль молча.

    Себестоимость есть только у 111. Его прибыль 1401,43, у 222 прибыли нет
    вовсе. Итог обязан показать 1401,43 и сказать, что сложены не все.
    """
    client_id = seed(db_path, ROWS, {111: Decimal("500")}, telegram_id=7373)
    report = report_of(client_id, db_path)
    totals = report.totals

    assert totals.articles == 2
    assert totals.priced == 1
    assert totals.complete is False
    assert totals.profit == Decimal("1401.43")
    # Себестоимость сложена по тому артикулу, у которого она есть, а выручка
    # и расходы по обоим: выручка известна и там, где прибыль не посчитана.
    assert totals.cost == Decimal("500")
    assert totals.revenue == Decimal("5000")

    row = totals_row(xlsx.read_book(profit.excel_bytes(report))[profit.ARTICLES_SHEET])
    note = str(row.values[1])
    assert "1 артикулам из 2" in note
    assert profit.PROBLEMS_SHEET in note


def test_a_total_over_nothing_says_no_data_instead_of_zero(db_path):
    """Себестоимости нет ни у одного артикула: в итоге не ноль, а «нет данных».

    Ноль в клетке прибыли прочитался бы как «отработали в ноль», а мы просто
    не знаем: сумма пустого множества это не результат.
    """
    client_id = seed(db_path, ROWS, {}, telegram_id=7575)
    report = report_of(client_id, db_path)

    assert report.totals.priced == 0
    assert report.totals.costed == 0
    row = totals_row(xlsx.read_book(profit.excel_bytes(report))[profit.ARTICLES_SHEET])
    assert row.get("Чистая прибыль, ₽") == profit.NO_DATA
    assert row.get("Себестоимость, ₽") == profit.NO_DATA
    assert row.get(profit.MARGIN_COLUMN) == profit.NO_DATA
    # Выручка известна и здесь: её складывать ничто не мешает.
    assert row.get("Выручка, ₽") == 5000


def test_unknown_commission_is_named_next_to_the_total(db_path):
    client_id = seed(
        db_path, ROWS, {111: Decimal("500"), 222: Decimal("900")}, telegram_id=7474
    )
    db.repo(client_id, db_path).update("fin_rows", {"rrd_id": 2}, raw=None)
    report = report_of(client_id, db_path)

    assert report.totals.no_commission == 1
    row = totals_row(xlsx.read_book(profit.excel_bytes(report))[profit.ARTICLES_SHEET])
    assert "комиссия неизвестна по 1 артикулам" in str(row.values[1])


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


@pytest.mark.asyncio
async def test_the_same_period_pressed_twice_answers_that_the_work_is_already_going(
    seller, db_path
):
    """Вторая такая же задача не ставится, и кнопка не говорит «принято» дважды."""
    queue.reset()
    telegram_id = int(db.admin_repo(db_path).client(seller)["telegram_id"])

    answers = []
    for _ in range(2):
        query = FakeQuery("profit:quarter", FakeMessage())
        await handlers_profit.period_chosen(
            FakeUpdate(telegram_id, query.message, query), None, path=db_path
        )
        answers.append(query.answered)

    assert answers[0] == "Принято"
    assert answers[1] == queue.ALREADY_QUEUED
    assert len(db.admin_repo(db_path).tasks_by_kind(profit.TASK_KIND)) == 1


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


# --- чужой текст в сообщении о прибыли ---
#
# Артикул продавца селлер вписывает сам в кабинете Wildberries, и оттуда он
# приезжает прямо в строку топа. Сводка уходит с ParseMode.HTML: осмысленная
# угловая скобка стала бы ссылкой от имени бота, случайная - ошибкой
# Telegram, и тогда отчёта о прибыли клиент не увидит вообще, ни с первой
# попытки, ни с любой следующей.

TRAP = '<a href="http://zlo.example">нажми</a>'


def test_a_vendor_code_from_the_cabinet_does_not_become_markup(db_path):
    rows = [dict(row) for row in ROWS]
    for row in rows:
        if row.get("vendorCode") == "A-1":
            row["vendorCode"] = TRAP
    client_id = seed(
        db_path, rows, {111: Decimal("500"), 222: Decimal("900")}, telegram_id=7171
    )

    text = handlers_profit.summary_text(report_of(client_id, db_path))

    assert "<a href" not in text
    assert "&lt;a href=&quot;" in text
    assert "нажми" in text  # текст не потерян, он просто не ссылка
    assert "<b>" in text  # а разметка самого бота на месте


def test_a_product_name_from_the_cabinet_does_not_become_markup_either(seller, db_path):
    """Название карточки пишет продавец, и это такой же чужой текст."""
    profit.remember_names(seller, [{"nmID": 111, "title": TRAP}], asked=[111], path=db_path)

    text = handlers_profit.summary_text(report_of(seller, db_path))

    assert "<a href" not in text
    assert "&lt;a href=&quot;" in text
    assert "<b>" in text  # разметка самого бота на месте


# --- название товара в сообщении ---


def top_line(text: str) -> str:
    """Первая строка топа: та, что начинается с номера."""
    for line in str(text).splitlines():
        if line.startswith("1. "):
            return line
    raise AssertionError("в сообщении нет строки топа")


def test_the_message_names_the_product_and_keeps_the_article(seller, db_path):
    """В топе видно название товара, а не только номер.

    Артикул из строки не исчезает: по нему селлер ищет товар в кабинете и в
    файле, а названия у соседних карточек бывают почти одинаковые.
    """
    profit.remember_names(seller, CARDS["cards"], asked=[111, 222], path=db_path)

    line = top_line(handlers_profit.summary_text(report_of(seller, db_path)))

    assert "Наматрасник" in line
    assert "111" in line
    assert "A-1" in line


def test_a_long_name_does_not_spread_the_line_over_three_screens(seller, db_path):
    """Длинное название режется, и артикул из строки не пропадает.

    Название карточки на Wildberries бывает в сотню знаков, а в строке топа
    стоят ещё прибыль, маржа и доля: без предела она разъезжается на телефоне.
    """
    long_name = "Наматрасник на резинке 160х200 непромокаемый с бортами хлопок"
    profit.remember_names(seller, [{"nmID": 111, "title": long_name}], asked=[111], path=db_path)

    line = top_line(handlers_profit.summary_text(report_of(seller, db_path)))

    assert len(line) < len(long_name) + 20
    assert long_name not in line          # название обрезано
    assert "Наматрасник" in line          # но узнаваемо
    assert "…" in line                    # и срез виден
    assert "111" in line and "A-1" in line


def test_without_a_card_the_line_stays_the_way_it_was(seller, db_path):
    """Названия нет вовсе: в строке остаются одни артикулы, как раньше."""
    line = top_line(handlers_profit.summary_text(report_of(seller, db_path)))

    assert line.startswith("1. 111 (A-1): ")


def test_the_message_says_what_the_faceless_money_is_made_of(db_path):
    """«Расходы без артикула: 900 ₽» без состава читается как отговорка."""
    client_id = seed(
        db_path,
        FACELESS_STORAGE,
        {111: Decimal("500"), 222: Decimal("400")},
        telegram_id=7676,
    )
    text = handlers_profit.summary_text(
        report_of(client_id, db_path, ads=profit.AdSpend({}))
    )

    assert "хранение 600 ₽" in text
    assert "прочие удержания 300 ₽" in text
    # И сказано, почему этих колонок нет в файле.
    assert "колонок под них в файле нет" in text


# --- срок жизни названий ---


def test_the_name_lifetime_is_a_setting_and_not_a_number_in_the_code():
    assert profit.names_ttl_days() == int(config.settings()["cards"]["name_ttl_days"])
    assert profit.names_ttl_days() > 0


def backdate(client_id, db_path, nm_id, days):
    """Сдвигает отметку «спрашивали» назад: карточку переименовали давно."""
    when = datetime.now(timezone.utc) - timedelta(days=days)
    db.repo(client_id, db_path).update(
        profit.CARDS_TABLE, {"nm_id": nm_id}, updated_at=when.strftime("%Y-%m-%d %H:%M:%S")
    )


RENAMED = {
    "cards": [{"nmID": 111, "title": "Наматрасник премиум 160х200", "vendorCode": "A-1"}],
    "cursor": {"updatedAt": "", "nmID": 111, "total": 1},
}


@pytest.mark.asyncio
async def test_a_renamed_card_reaches_the_report_when_the_name_goes_stale(
    cabinet, db_path
):
    """Без срока жизни переименование не доехало бы до отчёта никогда."""
    http, seen = counting_http({CARDS_PATH: CARDS})
    first = await names_for(cabinet, db_path, [111], http)
    assert first == {111: "Наматрасник на резинке 160х200"}

    # Пока имя свежее, в Wildberries не ходим ни разу.
    await names_for(cabinet, db_path, [111], http)
    assert seen == [CARDS_PATH]

    backdate(cabinet, db_path, 111, profit.names_ttl_days() + 1)
    later, asked = counting_http({CARDS_PATH: RENAMED})
    names = await names_for(cabinet, db_path, [111], later)

    assert asked == [CARDS_PATH]
    assert names == {111: "Наматрасник премиум 160х200"}


@pytest.mark.asyncio
async def test_a_deleted_card_does_not_send_the_report_to_wb_every_time(
    cabinet, db_path
):
    """Срок жизни не превращается в поход в WB на каждый отчёт.

    Карточки 222 у Wildberries нет, и имени для неё не будет никогда. Если бы
    отметка «спрашивали» не освежалась, один удалённый товар гонял бы бота за
    каталогом всякий раз, а платит за это токен клиента.
    """
    http, seen = counting_http({CARDS_PATH: CARDS})
    await names_for(cabinet, db_path, [111, 222], http)
    for nm_id in (111, 222):
        backdate(cabinet, db_path, nm_id, profit.names_ttl_days() + 1)

    await names_for(cabinet, db_path, [111, 222], http)
    await names_for(cabinet, db_path, [111, 222], http)

    # Два похода: первый и тот, что случился по сроку. Третий не понадобился.
    assert seen == [CARDS_PATH, CARDS_PATH]
    assert profit.names_of(cabinet, path=db_path)[222] == ""
