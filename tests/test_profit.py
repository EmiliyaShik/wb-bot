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
from core import audit, config, crypto, db, queue, tax, wbapi, xlsx

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
    assert parts == {"storage": Decimal("600"), "other_deductions": Decimal("300")}
    assert sum(parts.values(), Decimal("0")) == report.unallocated == Decimal("900")
    # Хранение и удержания Wildberries по товарам не разнёс вовсе, логистику
    # разнёс: правило одно на все статьи и стоит на данных, а не на списке
    # имён в коде.
    assert {item.key for item in report.unshared_items} == {
        "storage",
        "other_deductions",
    }


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


# Та же неделя, но строки с названием товара: Wildberries присылает его полем
# `title` в самом отчёте о реализации. На живом кабинете владельца это поле
# заполнено во всех 22 088 строках, у которых есть артикул, и пусто только в
# служебных, где артикула нет вовсе.
NAMED_ROWS = [
    dict(row, title=f"Товар {row['nmId']}") if row.get("nmId") else dict(row)
    for row in ROWS
]


def test_the_name_comes_with_the_money_and_not_from_the_catalogue(db_path):
    """Название берётся из отчёта о реализации, а справочник не нужен вовсе.

    Раньше за именем товара бот ходил в каталог карточек отдельным методом, и
    это был лишний поход в Wildberries у каждого клиента на каждом отчёте.
    Имя приезжает вместе с деньгами, той же строкой.
    """
    client_id = seed(db_path, NAMED_ROWS, {111: Decimal("500")}, telegram_id=6161)

    report = report_of(client_id, db_path)

    assert profit.names_of(client_id, path=db_path) == {}
    assert article(report, 111).title == "Товар 111"
    assert article(report, 222).title == "Товар 222"
    assert article(report, 111).display_name == "Товар 111"


def test_a_renamed_product_keeps_the_name_it_sells_under_now(db_path):
    """Товар переименовали - в книге стоит нынешнее имя, а не первое.

    На живом кабинете владельца так и есть у одного артикула из десяти: в
    отчёте за год лежат два названия. Нынешнее это то, под которым товар
    продаётся сейчас, и именно его селлер видит в кабинете.
    """
    # Новое имя лежит в строке с меньшим номером: так проверяется, что решает
    # дата операции, а не порядок строк в базе.
    new = dict(ROWS[0], rrdId=71, rrDate="2026-09-05", title="Новое имя")
    old = dict(ROWS[0], rrdId=72, rrDate="2026-09-02", title="Старое имя")
    client_id = seed(db_path, [new, old], {111: Decimal("500")}, telegram_id=6262)

    report = report_of(client_id, db_path)

    assert article(report, 111).title == "Новое имя"


@pytest.mark.asyncio
async def test_only_the_unnamed_articles_send_the_bot_to_the_catalogue(
    cabinet, db_path
):
    """В каталог карточек бот идёт только за безымянным артикулом.

    Так работает обработчик задачи: он спрашивает названия ровно по тем
    артикулам, которых отчёт о реализации не назвал. Названия есть у всех -
    похода в Wildberries нет вовсе, и категория токена «Контент» для отчёта
    больше не нужна.
    """
    finance.save_rows(cabinet, NAMED_ROWS, path=db_path)
    for week in finance.aggregate(NAMED_ROWS).values():
        finance.save_week(cabinet, week, path=db_path)
    report = report_of(cabinet, db_path)
    http, seen = counting_http({CARDS_PATH: CARDS})

    unnamed = [item.nm_id for item in report.articles if not item.title]
    names = await names_for(cabinet, db_path, unnamed, http)

    assert unnamed == []
    assert names == {}
    assert seen == []


@pytest.mark.asyncio
async def test_a_row_without_the_name_still_goes_to_the_catalogue(cabinet, db_path):
    """Строка без названия оставляет справочник запасным путём.

    Так лежат строки, записанные прежней версией бота: `raw` у них нет, и
    названия взять негде. Отнять у таких артикулов имя значило бы испортить
    уже выданные отчёты ради чистоты.
    """
    finance.save_rows(cabinet, ROWS, path=db_path)
    for week in finance.aggregate(ROWS).values():
        finance.save_week(cabinet, week, path=db_path)
    report = report_of(cabinet, db_path)
    http, seen = counting_http({CARDS_PATH: CARDS})

    unnamed = [item.nm_id for item in report.articles if not item.title]
    names = await names_for(cabinet, db_path, unnamed, http)

    assert sorted(unnamed) == [111, 222]
    assert names == {111: "Наматрасник на резинке 160х200", 222: ""}
    assert seen == [CARDS_PATH]
    # И это имя доезжает до книги тем же путём, что и раньше.
    assert article(profit.with_names(report, names), 111).title == (
        "Наматрасник на резинке 160х200"
    )


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
    # Про налог у этого селлера уже спрашивали: вопрос стоит перед периодом,
    # и без отметки экран периодов до него просто не дошёл бы.
    tax.mark_asked(seller, path=db_path)

    message = FakeMessage()
    await handlers_profit.profit_command(FakeUpdate(telegram_id, message), None, path=db_path)
    buttons = message.sent[0][1]["reply_markup"].inline_keyboard
    # Относительные кнопки стоят первыми и считаются как раньше. Названные
    # месяцы и «свои даты» идут ниже: у них свой, точный способ счёта.
    assert [row[0].callback_data for row in buttons[:4]] == [
        "profit:week",
        "profit:month",
        "profit:quarter",
        "profit:year",
    ]
    assert buttons[-1][0].callback_data == "profit:custom"

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


# --- рекламный счёт внутри удержаний -----------------------------------------
#
# Wildberries выставляет за рекламу счёт и удерживает его из отчёта о
# реализации: строка приходит без nmId, sellerOperName у неё «Удержание», а
# за что удержали, написано в bonusTypeName словами самого Wildberries.
# Те же деньги приходят в отчёт вторым путём, расходом по товарам из
# статистики кампаний, и пока удержание лежало в общей куче расходов без
# артикула, одни и те же рубли вычитались дважды.
#
# Неделя ниже собрана так, чтобы двойной счёт было видно в лоб: рекламное
# удержание 400 ровно равно сумме расхода по товарам (300 плюс 100), и больше
# рекламных удержаний в ней нет.
AD_DEDUCTION_ROWS = ROWS + [
    {
        "reportId": 1,
        "rrdId": 7,
        "dateFrom": "2026-09-01",
        "dateTo": "2026-09-07",
        # Так выглядит живая строка: типа документа нет, артикула нет,
        # обоснование названо Wildberries.
        "docTypeName": "",
        "sellerOperName": "Удержание",
        "bonusTypeName": "Оказание услуг «WB Продвижение», документ №313728640",
        "deduction": 400,
    },
    {
        "reportId": 1,
        "rrdId": 8,
        "dateFrom": "2026-09-01",
        "dateTo": "2026-09-07",
        # Удержание с другим обоснованием. Оно обязано остаться расходом:
        # вычесть рекламу один раз это не повод потерять транзитную доставку.
        "docTypeName": "",
        "sellerOperName": "Удержание",
        "bonusTypeName": "Услуги доставки транзитных поставок. Поставка №39328480",
        "deduction": 50,
    },
]

AD_DEDUCTION_COSTS = {111: Decimal("500"), 222: Decimal("900")}


@pytest.mark.parametrize(
    "reason, expected",
    [
        ("Оказание услуг «WB Продвижение»", True),
        ("Оказание услуг «WB Продвижение», документ №313728640", True),
        # Регистр у Wildberries не постоянен, а продукт тот же.
        ("оказание услуг «wb продвижение»", True),
        ("Услуги доставки транзитных поставок. Поставка №39328480", False),
        ("Добровольная выплата за товары", False),
        # Пустое обоснование это «не знаем, за что», а не реклама: неизвестное
        # удержание обязано остаться расходом.
        ("", False),
        (None, False),
    ],
)
def test_wb_names_the_ad_invoice_itself_and_nothing_is_guessed(reason, expected):
    """Рекламное удержание узнаётся по словам Wildberries, а не по сумме.

    Проверено на живом кабинете: «документ №N» из обоснования это updNum из
    GET /adv/v1/upd, и по всем 38 документам с номером суммы совпали до рубля.
    Поэтому признак берётся из обоснования, а не выводится из величины или из
    близости к расходу кампаний.
    """
    assert finance.is_ad_deduction(reason) is expected


def test_full_deductions_stay_whole_for_the_reconciliation(db_path):
    """Сверка /finance смотрит на удержания целиком, и делить их нельзя.

    Числа руками: удержания недели это 300 служебных плюс 400 рекламных плюс
    50 транзитных, всего 750. Из них рекламных 400, прочих 350. Wildberries
    отдаёт в итогах отчёта одно число deductionSum, и если наша сумма
    перестанет быть полной, сверка начнёт расходиться на рекламу каждую
    неделю.
    """
    client_id = seed(db_path, AD_DEDUCTION_ROWS, AD_DEDUCTION_COSTS, telegram_id=8181)

    # Путь первый: ответ Wildberries как он есть.
    fresh = finance.aggregate(AD_DEDUCTION_ROWS)[1].amounts
    assert fresh.deductions == Decimal("750")
    assert fresh.ad_deductions == Decimal("400")
    assert fresh.other_deductions == Decimal("350")
    # Рекламная часть это часть удержаний, а не прибавка к ним: сложение
    # обязано сойтись, иначе сверка потеряет или удвоит рекламу.
    assert fresh.ad_deductions + fresh.other_deductions == Decimal("750")

    # Путь второй: те же строки, прочитанные из базы. Два пути сбора не имеют
    # права разойтись в том, что считать рекламой.
    weeks = finance.weeks_of(client_id, date(2026, 9, 1), TODAY, path=db_path)
    stored = finance.articles_of(client_id, weeks, path=db_path)
    faceless = [item for item in stored if item.nm_id is None][0]
    assert faceless.amounts.ad_deductions == Decimal("400")
    assert faceless.amounts.deductions == Decimal("750")
    # Сверка /finance берёт удержания целиком, и рекламы из них не вычитает:
    # у Wildberries в итогах отчёта одно число deductionSum.
    assert weeks[0].amounts.deductions == Decimal("750")


def test_the_ad_invoice_is_subtracted_once_not_twice(db_path):
    """Одни и те же рекламные рубли вычитаются из прибыли ровно один раз.

    Числа руками. Расход рекламы по товарам: 300 у 111 и 100 у 222, всего 400.
    Рекламное удержание той же недели тоже 400: это и есть те же деньги,
    пришедшие вторым путём.

    Расходы без артикула теперь только 300 служебных плюс 50 транзитных, то
    есть 350, а не 750. Разносятся они по выручке за вычетом возвратов:
    у 111 это 4000 - 800 = 3200, у 222 это 1000, вес всего 4200.
    Доля 111: 350 * 3200 / 4200 = 266.67 (округление до копейки).
    Доля 222: 350 * 1000 / 4200 = 83.33.

    Прибыль 111: 3200 - 500 (себестоимость одной проданной за вычетом
    возврата) - 460 (комиссия 560 - 100) - 60 эквайринга - 200 логистики
    - 40 хранения - 10 приёмки - 300 рекламы - 266.67 обезлички = 1363.33.
    Прибыль 222: 1000 - 900 - 170 - 20 - 100 - 10 хранения - 30 штрафов
    - 100 рекламы - 83.33 = -413.33.

    Если бы реклама вычиталась дважды, обезличка осталась бы 750, доли вышли
    бы 571.43 и 178.57, и прибыль упала бы ровно на эти же 400 рублей.
    """
    client_id = seed(db_path, AD_DEDUCTION_ROWS, AD_DEDUCTION_COSTS, telegram_id=8282)
    report = report_of(client_id, db_path)

    assert report.ad_deductions == Decimal("400")
    assert report.ad_deductions_counted is False
    assert report.total_ad_spend == Decimal("400")
    # Обезличка без рекламного счёта: 300 служебных плюс 50 транзитных.
    assert report.unallocated == Decimal("350")

    assert article(report, 111).unallocated == Decimal("266.67")
    assert article(report, 222).unallocated == Decimal("83.33")
    assert article(report, 111).profit == Decimal("1363.33")
    assert article(report, 222).profit == Decimal("-413.33")
    # Колонка «прочие удержания» показывает удержания без рекламы, и по этим
    # артикулам она пуста: строки пришли без nmId.
    assert article(report, 111).deductions == Decimal("0")
    assert article(report, 111).ad_deductions == Decimal("0")

    # Та же проверка со стороны денег: расходы, вошедшие в прибыль, содержат
    # рекламу один раз. Прибыль по товарам, посчитанная сложением, обязана
    # сойтись с выручкой минус себестоимость минус расходы.
    spent = sum(
        (
            item.wb_costs
            + (item.ad_spend or Decimal("0"))
            + item.unallocated
            for item in report.priced
        ),
        Decimal("0"),
    )
    # 460 + 60 + 200 + 40 + 10 комиссии и услуг у 111 это 770, плюс
    # 170 + 20 + 100 + 10 + 30 у 222 это 330, плюс 400 рекламы, плюс 350
    # обезлички. Рекламных денег здесь 400, а не 800.
    assert spent == Decimal("1850")


def test_without_campaign_stats_the_ad_invoice_is_still_an_expense(db_path):
    """Нет расхода по товарам - вычитается удержание, а не ничего.

    Иначе починка двойного счёта подарила бы селлеру без категории
    «Продвижение» прибыль, которой у него нет: рекламный счёт Wildberries
    забрал по-настоящему.

    Числа руками: обезличка снова полная, 300 + 400 + 50 = 750. Доля 111:
    750 * 3200 / 4200 = 571.43, доля 222: 750 * 1000 / 4200 = 178.57.
    Прибыль 111: 3200 - 500 - 460 - 60 - 200 - 40 - 10 - 571.43 = 1358.57
    (расхода рекламы по товарам нет вовсе).
    Прибыль 222: 1000 - 900 - 170 - 20 - 100 - 10 - 30 - 178.57 = -408.57.
    """
    client_id = seed(db_path, AD_DEDUCTION_ROWS, AD_DEDUCTION_COSTS, telegram_id=8383)
    report = report_of(
        client_id,
        db_path,
        ads=profit.AdSpend({}, available=False, reason=profit.ADS_NO_CATEGORY),
    )

    assert report.ad_deductions_counted is True
    assert report.unallocated == Decimal("750")
    assert article(report, 111).profit == Decimal("1358.57")
    assert article(report, 222).profit == Decimal("-408.57")


def test_the_faceless_breakdown_still_adds_up_to_the_spread_money(db_path):
    """Расшифровка обезлички сходится с ней самой и после правки.

    Статья, которой в обезличке нет, в расшифровке не появляется: иначе
    сложение строк дало бы 750 против 350 в колонке, и селлер увидел бы две
    разные правды об одних деньгах.
    """
    client_id = seed(db_path, AD_DEDUCTION_ROWS, AD_DEDUCTION_COSTS, telegram_id=8484)
    report = report_of(client_id, db_path)

    parts = {item.key: item.faceless for item in report.faceless_parts}
    assert parts == {"other_deductions": Decimal("350")}
    assert sum(parts.values(), Decimal("0")) == report.unallocated
    assert profit.AD_DEDUCTIONS_KEY not in {item.key for item in report.unshared_items}

    # Без статистики кампаний статья возвращается в обезличку целиком.
    blind = report_of(
        client_id,
        db_path,
        ads=profit.AdSpend({}, available=False, reason=profit.ADS_UNAVAILABLE),
    )
    parts = {item.key: item.faceless for item in blind.faceless_parts}
    assert parts == {
        profit.AD_DEDUCTIONS_KEY: Decimal("400"),
        "other_deductions": Decimal("350"),
    }
    assert sum(parts.values(), Decimal("0")) == blind.unallocated == Decimal("750")


def test_the_book_and_the_message_name_the_ad_invoice_by_its_own_name(db_path):
    """Деньги, которых нет в расходах, обязаны быть названы.

    Крупная сумма, исчезнувшая из расходов без объяснения, читается как
    потеря отчёта, а не как исправленный двойной счёт.
    """
    client_id = seed(db_path, AD_DEDUCTION_ROWS, AD_DEDUCTION_COSTS, telegram_id=8585)
    report = report_of(client_id, db_path)

    book = xlsx.read_book(profit.excel_bytes(report))
    named = [
        row
        for row in book[profit.PROBLEMS_SHEET].rows
        if row.get("Блок") == profit.AD_DEDUCTION_BLOCK
    ]
    assert len(named) == 1
    assert named[0].get("Сумма, ₽") == 400

    # Методология говорит, каким путём реклама вычтена.
    method = [row.get("Показатель") for row in book[profit.METHOD_SHEET].rows]
    assert "Реклама, удержано Wildberries" in method

    text = str(handlers_profit.summary_text(report))
    assert "удержал 400" in text


# --- счёт и статистика живут в разных днях ------------------------------------
#
# Wildberries выставляет счёт рекламного кабинета одной неделей, а показы, за
# которые он выставлен, были в другие дни. На годе два числа почти сходятся, и
# замена счёта расходом по товарам работала. На коротком периоде счёт больше
# отработавшей в нём рекламы, и пока из расходов убирался весь счёт, разница
# исчезала из отчёта совсем: расходы без артикула уходили в минус, а селлер
# видел прибыль, которой у него нет. На живом кабинете за неделю это выглядело
# как прибыль 1094.70 при настоящем убытке 1628.30.
#
# Строка добровольной выплаты ниже это не выдумка для теста: Wildberries
# возвращает деньги за испорченный товар тем же полем `deduction`, но с
# минусом. У владельца за квартал это 28 тысяч, и именно из-за них расходы без
# артикула у неё отрицательные по-настоящему, а не из-за рекламы.
GOODWILL_ROW = {
    "reportId": 1,
    "rrdId": 10,
    "dateFrom": "2026-09-01",
    "dateTo": "2026-09-07",
    "docTypeName": "",
    "sellerOperName": "Удержание",
    "bonusTypeName": (
        "Добровольная выплата за товары, пострадавшие в результате "
        "обстоятельств непреодолимой силы, документ №2298944"
    ),
    "deduction": -1000,
}


def test_on_a_short_period_the_invoice_is_not_removed_beyond_what_was_replaced(
    db_path,
):
    """Счёт больше статистики: остаток счёта остаётся расходом.

    Числа руками. Счёт 400, а статистика кампаний за тот же период дала
    только 100 (у артикула 111): остальные показы были в другие дни, и в этот
    период они не попали. Заменить можно ровно 100, остаток 300 остаётся
    расходом и разносится вместе с обезличкой: 300 служебных + 50 транзитных
    + 300 остатка = 650.

    Доли: 650 * 3200 / 4200 = 495.24 у 111 и 650 * 1000 / 4200 = 154.76 у 222.
    Прибыль 111: 3200 - 500 - 460 - 60 - 200 - 40 - 10 - 100 рекламы
    - 495.24 = 1334.76. Прибыль 222: 1000 - 900 - 170 - 20 - 100 - 10 - 30
    - 154.76 = -384.76.

    Главное здесь не доли, а сумма: рекламных денег в расходах 400, то есть
    ровно столько, сколько Wildberries удержал. Старое правило убирало весь
    счёт и возвращало 100, и 300 рублей настоящего расхода исчезали из отчёта.
    """
    client_id = seed(db_path, AD_DEDUCTION_ROWS, AD_DEDUCTION_COSTS, telegram_id=8686)
    report = report_of(client_id, db_path, ads=profit.AdSpend({111: Decimal("100")}))

    assert report.ad_deductions == Decimal("400")
    assert report.total_ad_spend == Decimal("100")
    assert report.ad_replaced == Decimal("100")
    assert report.ad_uncovered == Decimal("300")

    # Обезличка положительная, и рекламная механика её вниз не тянет: в неё
    # вошёл остаток счёта, а не минус от него.
    assert report.unallocated == Decimal("650")
    assert article(report, 111).unallocated == Decimal("495.24")
    assert article(report, 222).unallocated == Decimal("154.76")
    assert article(report, 111).profit == Decimal("1334.76")
    assert article(report, 222).profit == Decimal("-384.76")

    # Рекламных денег в расходах ровно столько, сколько удержал Wildberries:
    # не 100 (тогда 300 потерялись бы) и не 500 (тогда 100 вычлись бы дважды).
    assert (report.total_ad_spend or Decimal("0")) + report.ad_uncovered == Decimal(
        "400"
    )
    # Расшифровка обезлички сходится с ней самой, а рекламная строка в ней
    # показывает остаток счёта, а не весь счёт.
    parts = {item.key: item.in_pool for item in report.faceless_parts}
    assert parts == {
        profit.AD_DEDUCTIONS_KEY: Decimal("300"),
        "other_deductions": Decimal("350"),
    }
    assert sum(parts.values(), Decimal("0")) == report.unallocated


def test_the_faceless_minus_from_wildberries_paying_the_seller_stays_a_minus(db_path):
    """Законный минус остаётся минусом: его делает не реклама, а выплата WB.

    Добровольная выплата за испорченный товар приходит удержанием с минусом,
    и расходы без артикула от неё отрицательные по-настоящему: Wildberries
    вернул больше, чем забрал. Трогать такой минус нельзя, иначе отчёт
    спрячет деньги, которые селлер получил.

    Числа руками: 300 служебных + 50 транзитных - 1000 выплаты = -650. Счёт
    400 статистика объяснила целиком (300 + 100), поэтому остатка нет, и в
    обезличке рекламы нет вовсе: весь её минус до копейки это выплата.
    """
    client_id = seed(
        db_path,
        AD_DEDUCTION_ROWS + [GOODWILL_ROW],
        AD_DEDUCTION_COSTS,
        telegram_id=8787,
    )
    report = report_of(client_id, db_path)

    assert report.ad_replaced == Decimal("400")
    assert report.ad_uncovered == Decimal("0")
    assert report.unallocated == Decimal("-650")
    parts = {item.key: item.in_pool for item in report.faceless_parts}
    assert parts == {"other_deductions": Decimal("-650")}
    assert profit.AD_DEDUCTIONS_KEY not in parts

    # И отчёт об этом минусе говорит вслух, вместе с составом: молчание
    # оставило бы селлера с необъяснённой прибавкой к прибыли.
    text = str(handlers_profit.summary_text(report))
    assert "-650" in text
    assert "прочие удержания -650" in text

    # Та же неделя, но статистика объяснила из счёта только 100. Минус от
    # выплаты остался, а реклама его не углубила: её вклад в обезличку это
    # остаток счёта, то есть плюс 300, и ниже -650 обезличка не опускается
    # ни на каком расходе по товарам.
    partly = report_of(client_id, db_path, ads=profit.AdSpend({111: Decimal("100")}))
    assert partly.ad_uncovered == Decimal("300")
    assert partly.unallocated == Decimal("-350")
    assert partly.unallocated > report.unallocated


def test_on_a_long_period_the_statistics_cover_the_invoice_and_it_is_counted_once(
    db_path,
):
    """Статистика больше счёта: счёт заменён целиком и второй раз не вычтен.

    Так выглядит год живого кабинета: статистика кампаний 531 359.55 против
    счёта 497 539, и поведение обязано остаться прежним. Здесь то же в малом:
    статистика 600 (500 у 111 и 100 у 222) против счёта 400.

    Числа руками: обезличка только служебная, 300 + 50 = 350. Доли 266.67 и
    83.33. Прибыль 111: 3200 - 500 - 460 - 60 - 200 - 40 - 10 - 500 рекламы
    - 266.67 = 1163.33. Прибыль 222: 1000 - 900 - 170 - 20 - 100 - 10 - 30
    - 100 - 83.33 = -413.33.

    Рекламных денег в расходах 600: больше, чем удержано счётом, потому что
    часть показов этого периода Wildberries выставит счётом позже. Дважды не
    вычтено ничего.
    """
    client_id = seed(db_path, AD_DEDUCTION_ROWS, AD_DEDUCTION_COSTS, telegram_id=8888)
    report = report_of(
        client_id,
        db_path,
        ads=profit.AdSpend({111: Decimal("500"), 222: Decimal("100")}),
    )

    assert report.total_ad_spend == Decimal("600")
    assert report.ad_replaced == Decimal("400")
    assert report.ad_uncovered == Decimal("0")
    assert report.unallocated == Decimal("350")
    assert article(report, 111).profit == Decimal("1163.33")
    assert article(report, 222).profit == Decimal("-413.33")

    # Сумма расходов, вошедших в прибыль: 770 по 111, 330 по 222, 600 рекламы
    # и 350 обезлички. Рекламных денег здесь 600, а не 1000.
    spent = sum(
        (
            item.wb_costs + (item.ad_spend or Decimal("0")) + item.unallocated
            for item in report.priced
        ),
        Decimal("0"),
    )
    assert spent == Decimal("2050")


# --- вопрос про налог перед отчётом ---


@pytest.mark.asyncio
async def test_profit_asks_about_tax_before_the_period(seller, db_path):
    """Режим не выбран: сначала спрашиваем, иначе прибыль выйдет до налога."""
    telegram_id = int(db.admin_repo(db_path).client(seller)["telegram_id"])
    message = FakeMessage()

    await handlers_profit.profit_command(
        FakeUpdate(telegram_id, message), None, path=db_path
    )

    text, kwargs = message.sent[0]
    assert "налог" in text.lower()
    actions = [
        button.callback_data
        for row in kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert actions == ["profit:tax:usn_income", "profit:tax:usn_income_minus", "profit:taxoff"]


@pytest.mark.asyncio
async def test_chosen_mode_is_saved_and_the_period_follows(seller, db_path):
    telegram_id = int(db.admin_repo(db_path).client(seller)["telegram_id"])
    message = FakeMessage()
    query = FakeQuery("profit:tax:usn_income", message)

    await handlers_profit.period_chosen(
        FakeUpdate(telegram_id, message, query), None, path=db_path
    )

    rule = tax.rule_of(seller, path=db_path)
    assert rule.known and rule.mode == tax.USN_INCOME
    text, kwargs = message.sent[-1]
    assert "период" in text.lower()
    assert kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "profit:week"


@pytest.mark.asyncio
async def test_refusal_is_remembered_and_asked_only_once(seller, db_path):
    """Отказ от налога это ответ, а не молчание: второй раз не спрашиваем."""
    telegram_id = int(db.admin_repo(db_path).client(seller)["telegram_id"])
    message = FakeMessage()
    query = FakeQuery("profit:taxoff", message)

    await handlers_profit.period_chosen(
        FakeUpdate(telegram_id, message, query), None, path=db_path
    )
    assert not tax.rule_of(seller, path=db_path).known

    second = FakeMessage()
    await handlers_profit.profit_command(
        FakeUpdate(telegram_id, second), None, path=db_path
    )
    actions = [
        button.callback_data
        for row in second.sent[0][1]["reply_markup"].inline_keyboard
        for button in row
    ]
    assert "profit:week" in actions, "про налог спросили второй раз"


@pytest.mark.asyncio
async def test_a_made_up_mode_from_a_button_is_refused(seller, db_path):
    """`callback_data` приходит от клиента: чужой режим в настройки не ляжет."""
    telegram_id = int(db.admin_repo(db_path).client(seller)["telegram_id"])
    message = FakeMessage()
    query = FakeQuery("profit:tax:osno_vymyshlennaya", message)

    await handlers_profit.period_chosen(
        FakeUpdate(telegram_id, message, query), None, path=db_path
    )

    assert not tax.rule_of(seller, path=db_path).known
    assert not tax.was_asked(seller, path=db_path)
    assert message.sent == []


@pytest.mark.asyncio
async def test_chosen_mode_stops_the_question(seller, db_path):
    """Режим выбран в настройках: спрашивать уже нечего."""
    tax.set_rule(seller, tax.USN_INCOME, path=db_path)
    telegram_id = int(db.admin_repo(db_path).client(seller)["telegram_id"])
    message = FakeMessage()

    await handlers_profit.profit_command(
        FakeUpdate(telegram_id, message), None, path=db_path
    )

    assert message.sent[0][1]["reply_markup"].inline_keyboard[0][0].callback_data == (
        "profit:week"
    )
