"""Агент 7, воронка.

Швы те же два, что и у всех: путь к базе (временный файл) и транспорт WB
(`httpx.MockTransport`, сети нет ни байта). Отчёт в Wildberries не ходит
вовсе, и это здесь доказывается отдельно: клиент WB на время сборки ломается
нарочно, и отчёт всё равно собирается.

Ожидаемые проценты посчитаны руками и записаны рядом в комментариях: считать
их тем же способом, что и код, значит не проверить ничего.

Суточная история заводится теми же колонками, которые пишет сбор плана-факта,
и отдельный тест проходит весь путь целиком: ответ Wildberries -> `rnp.collect`
-> `nm_daily` -> отчёт по воронке. Второго сбора у воронки нет, и это не
забывчивость, а решение: те же запросы к WB каждый день у каждого клиента.
"""

from __future__ import annotations

import base64
import copy
import functools
import json
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from agents import funnel, rnp
from bot.handlers import funnel as handler
from bot.handlers import tariffs
from core import access, config, crypto, db, queue, wbapi, xlsx

# Вчера это 24 сентября: последний день отчёта всегда вчерашний.
TODAY = date(2026, 9, 25)
LAST = date(2026, 9, 24)

# Неделя «сейчас» и неделя «раньше». История начинается 11 сентября, ровно на
# две недели: период не укорачивается.
NOW_DAYS = [LAST - timedelta(days=shift) for shift in range(7)]
PAST_DAYS = [LAST - timedelta(days=shift) for shift in range(7, 14)]
SINCE = PAST_DAYS[-1]

MASK_FULL = 8294        # контент, аналитика, статистика, продвижение, финансы
MASK_NO_ANALYTICS = 8290  # то же самое без аналитики
EXP = 1789000000

SEARCH_PATH = "/api/v2/search-report/report"


def make_token(mask: int = MASK_FULL) -> str:
    """JWT с нужным payload. Подпись не проверяется, она тут не нужна."""

    def part(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    body = {"id": "cd" * 8, "sid": "s" * 8, "exp": EXP, "s": mask, "acc": 3}
    return f"{part({'alg': 'HS256', 'typ': 'JWT'})}.{part(body)}.c2lnbmF0dXJl"


class FakeTime:
    """Часы и пауза под контролем теста: бюджет запросов к WB не ждём вживую."""

    def __init__(self) -> None:
        self.now = 1000.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += float(seconds)


def put_day(client_id, db_path, day, nm_id, **fields) -> None:
    """Одни собранные сутки одного товара, теми же колонками, что пишет сбор.

    Непустая колонка `raw` это и есть признак «суточная воронка за эти сутки
    собрана»: её заполняет только разбор воронки, а строка с одними остатками
    про воронку не говорит ничего.
    """
    item = {
        "date": day.isoformat(),
        "openCount": fields.get("opens", 0),
        "cartCount": fields.get("carts", 0),
        "orderCount": fields.get("orders", 0),
        "orderSum": fields.get("order_sum", 0),
        "buyoutCount": fields.get("buyouts", 0),
    }
    db.repo(client_id, db_path).upsert(
        "nm_daily",
        {"date": day.isoformat(), "nm_id": nm_id},
        open_card_count=item["openCount"],
        add_to_cart_count=item["cartCount"],
        orders=item["orderCount"],
        orders_sum_kop=db.to_kop(Decimal(str(item["orderSum"]))),
        buyouts=item["buyoutCount"],
        raw=json.dumps(item, ensure_ascii=False),
    )


def put_days(client_id, db_path, days, nm_id, **fields) -> None:
    for day in days:
        put_day(client_id, db_path, day, nm_id, **fields)


@pytest.fixture
def cabinet(db_path, monkeypatch):
    """Подключённый кабинет с двумя неделями собранной суточной воронки.

    Товар 111 «Кружка»: конверсия в корзину заметно просела.
    Товар 222 «Ложка»:  ровный, просадки нет.
    Товар 333: собирался не каждые сутки, история с дырами.
    Товар 444: история полная, а заказов почти нет.
    """
    monkeypatch.setenv("ENCRYPTION_KEY", crypto.generate_key())
    wbapi.reset_limits()
    queue.reset()
    client_id = db.admin_repo(db_path).ensure_client(9090)
    db.repo(client_id, db_path).insert(
        "wb_tokens", ciphertext=crypto.encrypt(make_token()), exp=str(EXP), scopes="analytics"
    )
    for nm_id, title in ((111, "Кружка синяя"), (222, "Ложка"), (333, "Ковш")):
        db.repo(client_id, db_path).upsert(
            "card_names", {"nm_id": nm_id}, title=title, updated_at="2026-09-20 00:00:00"
        )

    # 111: было 100 заходов, 13 корзин, 7 заказов, 5 выкупов в сутки.
    put_days(client_id, db_path, PAST_DAYS, 111, opens=100, carts=13, orders=7, buyouts=5)
    # Стало 8 корзин, 4 заказа, 3 выкупа при тех же 100 заходах.
    put_days(client_id, db_path, NOW_DAYS, 111, opens=100, carts=8, orders=4, buyouts=3,
             order_sum=4000)

    # 222: ничего не поменялось.
    for days in (PAST_DAYS, NOW_DAYS):
        put_days(client_id, db_path, days, 222, opens=50, carts=10, orders=5, buyouts=4)

    # 333: раньше собирался каждые сутки, теперь только двое. Если сравнить
    # его в лоб, выйдет обвал конверсии, которого не было.
    put_days(client_id, db_path, PAST_DAYS, 333, opens=100, carts=20, orders=10, buyouts=8)
    put_days(client_id, db_path, NOW_DAYS[:2], 333, opens=100, carts=4, orders=10, buyouts=8)

    # 444: сутки собраны все, а заказов за период ноль.
    for days in (PAST_DAYS, NOW_DAYS):
        put_days(client_id, db_path, days, 444, opens=10, carts=1)
    return client_id


def report_of(client_id, db_path, **kw):
    return funnel.build(
        client_id, kw.pop("period", "week"), today=TODAY, path=db_path, **kw
    )


def article(report, nm_id):
    for item in report.articles:
        if item.nm_id == nm_id:
            return item
    raise AssertionError(f"товара {nm_id} нет в отчёте")


# --- чистые расчёты ----------------------------------------------------------


def test_conversion_is_a_share_of_the_previous_step():
    # 56 корзин из 700 заходов это ровно 8 процентов.
    assert funnel.conversion(56, 700) == Decimal("8.00")
    assert funnel.conversion(28, 56) == Decimal("50.00")


def test_conversion_without_the_previous_step_does_not_exist():
    """Никто не заходил: конверсии нет, и это не ноль.

    Ноль прочитался бы как «никто не дошёл до корзины», то есть как провал, а
    на самом деле в карточку просто не заходили.
    """
    assert funnel.conversion(0, 0) is None
    assert funnel.conversion(5, 0) is None


def test_the_thresholds_live_in_the_config_and_not_in_the_code(monkeypatch):
    section = config.settings()["funnel"]
    assert funnel.threshold(funnel.TO_CART) == Decimal(str(section["drop_to_cart"]))
    assert funnel.threshold(funnel.TO_ORDER) == Decimal(str(section["drop_to_order"]))
    assert funnel.threshold(funnel.TO_BUYOUT) == Decimal(str(section["drop_to_buyout"]))
    assert funnel.threshold(funnel.VISIBILITY) == Decimal(str(section["drop_visibility"]))

    patched = copy.deepcopy(config.settings())
    patched["funnel"]["drop_to_cart"] = 50
    monkeypatch.setattr(config, "settings", lambda: patched)
    assert funnel.threshold(funnel.TO_CART) == Decimal("50")


# --- четыре этапа на готовых полях Wildberries --------------------------------


def test_four_steps_are_counted_from_the_ready_fields_of_wildberries(cabinet, db_path):
    """Заход в карточку, корзина, заказ, выкуп. Числа посчитаны руками.

    Товар 111 за семь суток: заходов 7 * 100 = 700, корзин 7 * 8 = 56, заказов
    7 * 4 = 28, выкупов 7 * 3 = 21. Отсюда конверсии: 56 / 700 = 8 процентов,
    28 / 56 = 50 процентов, 21 / 28 = 75 процентов.

    Прошлая неделя: корзин 7 * 13 = 91, заказов 7 * 7 = 49, выкупов 7 * 5 = 35.
    Конверсии: 91 / 700 = 13 процентов, 49 / 91 = 53,85 процента,
    35 / 49 = 71,43 процента.
    """
    report = report_of(cabinet, db_path)
    item = article(report, 111)

    assert (item.now.opens, item.now.carts, item.now.orders, item.now.buyouts) == (
        700,
        56,
        28,
        21,
    )
    assert item.now.to_cart == Decimal("8.00")
    assert item.now.to_order == Decimal("50.00")
    assert item.now.to_buyout == Decimal("75.00")

    assert (item.past.opens, item.past.carts, item.past.orders, item.past.buyouts) == (
        700,
        91,
        49,
        35,
    )
    assert item.past.to_cart == Decimal("13.00")
    assert item.past.to_order == Decimal("53.85")
    assert item.past.to_buyout == Decimal("71.43")


def test_the_periods_are_equal_and_go_one_right_after_the_other(cabinet, db_path):
    report = report_of(cabinet, db_path)
    assert report.date_to == LAST
    assert report.date_from == LAST - timedelta(days=6)
    assert report.past_to == report.date_from - timedelta(days=1)
    assert (report.date_to - report.date_from) == (report.past_to - report.past_from)
    assert report.span == 7
    assert report.shortened is False
    assert report.since == SINCE


def test_the_cabinet_sums_up_only_the_goods_it_may_compare(cabinet, db_path):
    """Свод кабинета: 111 и 222. Числа руками.

    Заходы 700 + 350 = 1050, корзины 56 + 70 = 126, заказы 28 + 35 = 63,
    выкупы 21 + 28 = 49. Конверсии: 126 / 1050 = 12 процентов,
    63 / 126 = 50 процентов, 49 / 63 = 77,78 процента.

    Товара 333 тут нет: у него дыры в истории, и он занизил бы свод.
    Товара 444 тоже нет: заказов у него нет вовсе.
    """
    report = report_of(cabinet, db_path)
    assert {item.nm_id for item in report.compared} == {111, 222}
    assert report.now.opens == 1050
    assert report.now.carts == 126
    assert report.now.to_cart == Decimal("12.00")
    assert report.now.to_order == Decimal("50.00")
    assert report.now.to_buyout == Decimal("77.78")


# --- поиск просевшего этапа ---------------------------------------------------


def test_the_drop_is_found_and_the_advice_belongs_to_that_very_step(cabinet, db_path):
    """У товара 111 просело всё, что могло, и назван самый большой провал.

    В корзину: было 13, стало 8, это 5 процентных пунктов при пороге 2.
    В заказ: было 53,85, стало 50, это 3,85 пункта при пороге 3.
    В выкуп: было 71,43, стало 75, то есть выросло.

    Значит, назван шаг «из карточки в корзину», и подсказка тоже его.
    """
    report = report_of(cabinet, db_path)
    drop = article(report, 111).drop

    assert drop is not None
    assert drop.stage == funnel.TO_CART
    assert drop.was == Decimal("13.00")
    assert drop.now == Decimal("8.00")
    assert drop.points == Decimal("5.00")
    assert drop.advice == funnel.ADVICE[funnel.TO_CART]
    assert drop.advice != funnel.ADVICE[funnel.TO_ORDER]

    # И в тексте селлер видит и наблюдение, и ту же самую подсказку.
    text = handler.summary_text(report)
    assert "из карточки в корзину" in text
    assert funnel.ADVICE[funnel.TO_CART] in text
    assert funnel.ADVICE[funnel.TO_BUYOUT] not in text


def test_a_steady_product_has_no_drop(cabinet, db_path):
    assert article(report_of(cabinet, db_path), 222).drop is None


def test_the_cabinet_drop_is_found_too(cabinet, db_path):
    """По кабинету: было 15,33 процента в корзину, стало 12, это 3,33 пункта.

    Корзина в заказ просела с 52,17 до 50, это 2,17 пункта, меньше порога в 3.
    Значит, назван тот же шаг, что и у товара.
    """
    report = report_of(cabinet, db_path)
    drop = report.drop

    assert drop is not None
    assert drop.stage == funnel.TO_CART
    assert drop.was == Decimal("15.33")
    assert drop.now == Decimal("12.00")
    assert drop.points == Decimal("3.33")


def test_the_advice_is_offered_as_a_guess_and_not_as_a_diagnosis():
    """Наблюдение наше, причина это предположение, и так и написано."""
    for stage, text in funnel.ADVICE.items():
        assert "редположени" in text, stage


def test_a_drop_below_the_threshold_is_not_reported(cabinet, db_path, monkeypatch):
    """Порог живёт в конфиге: подняли его, и просадки не стало."""
    patched = copy.deepcopy(config.settings())
    patched["funnel"]["drop_to_cart"] = 20
    patched["funnel"]["drop_to_order"] = 20
    patched["funnel"]["drop_to_buyout"] = 20
    monkeypatch.setattr(config, "settings", lambda: patched)

    report = report_of(cabinet, db_path)
    assert article(report, 111).drop is None
    assert report.drop is None
    assert report.troubled == ()
    assert "Ни один шаг не просел" in handler.summary_text(report)


# --- дыры в истории -----------------------------------------------------------


def test_holes_in_the_history_are_not_passed_off_as_a_falling_conversion(
    cabinet, db_path
):
    """Товар 333 собирался двое суток из семи, и это не просадка.

    В лоб его цифры выглядят обвалом: было 140 корзин на 700 заходов (20
    процентов), стало 8 на 200 (4 процента). Но собрано двое суток из семи, а
    меньше собранных суток это меньше заходов и заказов, а не упавшая
    конверсия.
    """
    report = report_of(cabinet, db_path)
    item = article(report, 333)

    # Цифры на месте, их никто не прячет.
    assert item.now.days == 2 and item.past.days == 7
    assert item.now.to_cart == Decimal("4.00")
    assert item.past.to_cart == Decimal("20.00")

    # А выводов по ним не делается ни одного.
    assert item.enough_days is False
    assert item.comparable is False
    assert item.drop is None
    assert item not in report.compared
    assert item in report.partial
    assert 333 not in {row.nm_id for row in report.troubled}


def test_the_seller_learns_about_the_holes_from_the_report_itself(cabinet, db_path):
    """Про предел суточного сбора селлер узнаёт из отчёта, а не гадает."""
    text = handler.summary_text(report_of(cabinet, db_path))

    assert "не за каждые сутки" in text
    assert str(funnel.daily_articles()) in text
    assert "20 товарам за запрос" in text


def test_too_few_orders_are_set_aside_instead_of_being_called_a_drop(cabinet, db_path):
    """Товар 444: сутки собраны все, а заказов нет. Процентам верить нечему."""
    report = report_of(cabinet, db_path)
    item = article(report, 444)

    assert item.enough_days is True
    assert item.enough_orders is False
    assert item.comparable is False
    assert item in report.quiet
    assert "меньше" in handler.summary_text(report)


def test_a_cabinet_without_any_history_is_told_so_calmly(db_path):
    client_id = db.admin_repo(db_path).ensure_client(9191)
    report = report_of(client_id, db_path)

    assert report.span == 0
    assert report.since is None
    text = handler.summary_text(report)
    assert "не накопилось" in text
    assert "каждый день" in text


def test_a_short_history_shortens_both_periods_evenly(db_path, monkeypatch):
    """Истории пять суток: месяц с месяцем не сравнить, а двое суток с двумя да."""
    monkeypatch.setenv("ENCRYPTION_KEY", crypto.generate_key())
    client_id = db.admin_repo(db_path).ensure_client(9292)
    days = [LAST - timedelta(days=shift) for shift in range(5)]
    put_days(client_id, db_path, days, 111, opens=100, carts=10, orders=6, buyouts=5)

    report = report_of(client_id, db_path, period="month")
    assert report.span == 2
    assert report.shortened is True
    assert report.date_from == LAST - timedelta(days=1)
    assert report.past_to == LAST - timedelta(days=2)
    assert "Период укорочен" in handler.summary_text(report)


# --- отчёт не ходит в Wildberries ---------------------------------------------


def test_the_report_never_goes_to_wildberries(cabinet, db_path, monkeypatch):
    """Клиент WB сломан нарочно, и отчёт всё равно собирается целиком.

    Правило проекта: в WB ходят только сборщики. Отчёт стоит на `nm_daily`,
    которую каждый день наполняет сбор плана-факта.
    """

    def forbidden(*args, **kwargs):
        raise AssertionError("отчёт по воронке пошёл в Wildberries")

    monkeypatch.setattr(wbapi, "get_wb_client", forbidden)
    monkeypatch.setattr(wbapi.client, "get_wb_client", forbidden)

    report = report_of(cabinet, db_path)
    assert report.drop is not None
    assert handler.summary_text(report)
    assert funnel.excel_bytes(report)

    # И ни одного вызова WB в учёте.
    assert db.repo(cabinet, db_path).count("api_calls") == 0


def test_the_funnel_has_no_daily_collector_of_its_own(db_path, monkeypatch):
    """Второго суточного сбора у воронки нет: те же данные копит план-факт.

    Работ у неё две: разбор по просьбе клиента и утренняя чистка. Чистка это
    не сбор, и доказывается это тем же способом, что и в отчёте: клиент WB на
    время сломан нарочно, а работа всё равно проходит.
    """
    queue.reset()
    funnel.register_jobs()
    assert set(queue.handlers()) == {funnel.TASK_KIND, funnel.CLEANUP}

    def forbidden(*args, **kwargs):
        raise AssertionError("чистка сходила в Wildberries")

    monkeypatch.setattr(wbapi, "get_wb_client", forbidden)
    monkeypatch.setattr(wbapi.client, "get_wb_client", forbidden)
    assert funnel.cleanup(None, path=db_path) == 0


@pytest.mark.asyncio
async def test_the_report_stands_on_what_the_plan_fact_collector_saves(db_path, monkeypatch):
    """Путь целиком: ответ Wildberries -> rnp.collect -> nm_daily -> воронка.

    Колонки у сбора и у отчёта должны сходиться, а проверять это на данных,
    которые тест же и разложил по колонкам, значит не проверять ничего.
    """
    monkeypatch.setenv("ENCRYPTION_KEY", crypto.generate_key())
    wbapi.reset_limits()
    client_id = db.admin_repo(db_path).ensure_client(9393)
    db.repo(client_id, db_path).insert(
        "wb_tokens", ciphertext=crypto.encrypt(make_token()), exp=str(EXP), scopes="analytics"
    )

    history = [
        {
            "product": {"nmId": 111},
            "history": [
                {
                    "date": day.isoformat(),
                    "openCount": 100,
                    "cartCount": 8,
                    "orderCount": 4,
                    "orderSum": 4000,
                    "buyoutCount": 3,
                    "addToCartConversion": 8,
                    "cartToOrderConversion": 50,
                    "buyoutPercent": 75,
                }
                for day in NOW_DAYS
            ],
        }
    ]

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/sales-funnel/products/history"):
            return httpx.Response(200, json={"data": history})
        if path.endswith("/sales-funnel/products"):
            return httpx.Response(
                200,
                json={"data": {"products": [{"product": {"nmId": 111}}]}},
            )
        if path.endswith("/stocks-report/wb-warehouses"):
            return httpx.Response(200, json={"data": []})
        if path.endswith("/adv/v1/promotion/count"):
            return httpx.Response(200, json={"adverts": []})
        return httpx.Response(200, json={})

    clock = FakeTime()
    await rnp.collect(
        client_id,
        day=LAST,
        path=db_path,
        http=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        clock=clock.clock,
        sleep=clock.sleep,
    )

    # Собрана одна неделя, поэтому период укоротился до трёх суток против
    # трёх: сравнивать неделю с неделей ещё не на чем.
    report = report_of(client_id, db_path)
    assert report.span == 3 and report.shortened is True
    item = article(report, 111)
    assert item.now.opens == 300
    assert item.now.carts == 24
    assert item.now.to_cart == Decimal("8.00")


# --- пятый этап: видимость в поиске -------------------------------------------

# Ответ поискового отчёта в той форме, в какой его описывает спецификация
# Wildberries: группы товаров, а внутри каждой сами товары.
SEARCH_REPORT = {
    "data": {
        "currency": "RUB",
        "groups": [
            {
                "subjectName": "Посуда",
                "items": [
                    {
                        "nmId": 111,
                        "name": "Кружка синяя",
                        "visibility": {"current": 7, "dynamics": -30},
                        "openCard": {"current": 700, "dynamics": -10},
                        "avgPosition": {"current": 84, "dynamics": 12},
                    },
                    {
                        "nmId": 222,
                        "name": "Ложка",
                        "visibility": {"current": 20, "dynamics": 5},
                        "openCard": {"current": 350, "dynamics": 2},
                        "avgPosition": {"current": 12, "dynamics": -1},
                    },
                ],
            }
        ],
    }
}


def search_client(code: int | None = None) -> httpx.AsyncClient:
    def handle(request: httpx.Request) -> httpx.Response:
        if code:
            return httpx.Response(code, json={"title": "нет подписки"})
        return httpx.Response(200, json=SEARCH_REPORT)

    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


async def fetch_visibility(client_id, db_path, *, code=None):
    clock = FakeTime()
    return await funnel.collect_visibility(
        client_id,
        NOW_DAYS[-1],
        LAST,
        (PAST_DAYS[-1], PAST_DAYS[0]),
        path=db_path,
        http=search_client(code),
        clock=clock.clock,
        sleep=clock.sleep,
    )


@pytest.mark.asyncio
async def test_the_visibility_keeps_only_the_newest_snapshot(cabinet, db_path):
    """Видимость это срез, а не история.

    Границы периода входят в ключ, а период считается от вчера и каждый день
    другой: без уборки каждый /funnel оставлял бы в таблице новый набор строк
    навсегда.
    """
    repo = db.repo(cabinet, db_path)
    stale = {"date_from": "2026-01-01", "date_to": "2026-01-07"}
    repo.upsert("funnel_visibility", {**stale, "nm_id": 111}, visibility=3.0)

    assert await fetch_visibility(cabinet, db_path) == funnel.OK

    ends = {str(row["date_to"]) for row in repo.rows("funnel_visibility")}
    assert ends == {LAST.isoformat()}, "прошлый срез остался лежать в базе"


def test_the_report_reads_only_the_period_it_builds(cabinet, db_path, monkeypatch):
    """Отчёт за неделю не поднимает в память всю историю кабинета.

    `sqlite3` тут синхронный и живёт в одном процессе с ботом: долгое чтение
    это пауза у всех клиентов сразу. Индекс по дате в схеме стоит, значит
    границы периода обязаны быть в запросе.
    """
    # Прошлогодние сутки: в период они не входят, но историю удлиняют.
    put_day(cabinet, db_path, LAST - timedelta(days=300), 111, opens=9000, carts=9000)

    whole = db.ClientRepo.rows

    def guard(self, table, *args, **kwargs):
        assert table != "nm_daily", "суточная история прочитана целиком"
        return whole(self, table, *args, **kwargs)

    monkeypatch.setattr(db.ClientRepo, "rows", guard)

    report = report_of(cabinet, db_path)

    # Те же числа, что и без прошлогодней строки: 700 + 350 заходов.
    assert report.now.opens == 1050
    assert report.span == 7


def test_the_old_days_are_cleaned_up_and_the_term_lives_in_the_config(
    cabinet, db_path, monkeypatch
):
    """Срок хранения суточной истории это настройка владельца, а не число в коде."""
    repo = db.repo(cabinet, db_path)
    ancient = LAST - timedelta(days=500)
    put_day(cabinet, db_path, ancient, 111, opens=10)
    repo.upsert(
        "funnel_visibility",
        {"date_from": "2024-01-01", "date_to": "2024-01-07", "nm_id": 111},
        visibility=3.0,
    )
    before = repo.count("nm_daily")

    patched = copy.deepcopy(config.settings())
    patched["storage"]["daily_history_days"] = 60
    monkeypatch.setattr(config, "settings", lambda: patched)
    assert funnel.history_days() == 60

    task = SimpleNamespace(client_id=None, payload={"date": TODAY.isoformat()})
    assert funnel.cleanup(task, path=db_path) == 2

    assert repo.count("nm_daily") == before - 1
    assert repo.count("funnel_visibility") == 0
    assert ancient.isoformat() not in {str(row["date"]) for row in repo.rows("nm_daily")}


def test_without_jem_the_module_works_in_full_and_speaks_calmly(cabinet, db_path):
    """Нет подписки Джем: этапов четыре, и ни одного пугающего слова.

    Это нормальное состояние кабинета, а не поломка: четыре этапа это всё, что
    Wildberries вообще отдаёт, показов у него нет ни в каком виде.
    """
    report = report_of(cabinet, db_path)
    text = handler.summary_text(report)

    assert report.jem is False
    # Отчёт полный: и шаги, и конверсии, и просадка, и подсказка.
    assert report.drop is not None
    assert "Зашли в карточку" in text and "Положили в корзину" in text
    assert "Из карточки в корзину" in text
    assert "Этапов четыре" in text
    assert "показов Wildberries не отдаёт никому" in text
    # Про Джем сказано как про услугу Wildberries, а не как про нашу поломку.
    assert "Джем" in text
    for scary in ("ошибка", "сбой", "не работает", "недоступ"):
        assert scary not in text.lower(), scary


@pytest.mark.asyncio
async def test_with_jem_the_fifth_step_appears_and_is_named_for_what_it_is(
    cabinet, db_path
):
    """Пятый этап есть, и он назван вероятностью, а не показами."""
    assert await fetch_visibility(cabinet, db_path) == funnel.OK

    report = report_of(cabinet, db_path)
    item = article(report, 111)

    assert report.jem is True
    assert item.visibility is not None
    assert item.visibility.percent == Decimal("7")
    assert item.visibility.dynamics == Decimal("-30")
    # Порог 15 процентов, упало на 30: это просадка.
    assert item.visibility.fell is True
    assert article(report, 222).visibility.fell is False

    text = handler.summary_text(report)
    assert "Видимость в поиске" in text
    assert "не показы и не штуки" in text
    assert "вероятность в процентах" in text
    assert funnel.ADVICE[funnel.VISIBILITY] in text
    # А четыре основных этапа никуда не делись.
    assert "Зашли в карточку" in text


@pytest.mark.asyncio
async def test_the_visibility_is_asked_the_way_wildberries_asks_for_it(cabinet, db_path):
    """Пять обязательных полей тела запроса стоят на месте."""
    seen: list[dict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content.decode()))
        return httpx.Response(200, json=SEARCH_REPORT)

    clock = FakeTime()
    await funnel.collect_visibility(
        cabinet,
        NOW_DAYS[-1],
        LAST,
        (PAST_DAYS[-1], PAST_DAYS[0]),
        path=db_path,
        http=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        clock=clock.clock,
        sleep=clock.sleep,
    )

    body = seen[0]
    assert body["currentPeriod"] == {
        "start": NOW_DAYS[-1].isoformat(),
        "end": LAST.isoformat(),
    }
    assert body["pastPeriod"]["end"] == PAST_DAYS[0].isoformat()
    assert body["positionCluster"] == "all"
    assert body["orderBy"] == {"field": "openCard", "mode": "desc"}
    assert body["limit"] and body["offset"] == 0


@pytest.mark.asyncio
async def test_a_refusal_without_jem_is_a_normal_state_and_not_a_failure(
    cabinet, db_path
):
    """Категория токена есть, а подписки нет: причина названа своим именем."""
    assert await fetch_visibility(cabinet, db_path, code=403) == funnel.NO_JEM

    report = report_of(cabinet, db_path, trouble=funnel.NO_JEM)
    text = handler.summary_text(report)

    assert report.jem is False
    assert report.drop is not None, "четыре этапа обязаны работать без Джема"
    assert "Этапов четыре" in text
    # Про категорию токена речи нет: с ней всё в порядке.
    assert "Аналитика" not in text


@pytest.mark.asyncio
async def test_without_the_analytics_category_the_funnel_does_not_promise_to_wait(
    db_path, monkeypatch
):
    """Истории нет и не будет: сбор невозможен без категории «Аналитика».

    Раньше такой клиент получал «через пару дней сравнение появится» столько
    раз, сколько набирал команду, хотя не появилось бы никогда. Отчёт платный,
    и обещание вместо причины это прямая неправда.
    """
    monkeypatch.setenv("ENCRYPTION_KEY", crypto.generate_key())
    wbapi.reset_limits()
    queue.reset()
    client_id = db.admin_repo(db_path).ensure_client(9595)
    db.repo(client_id, db_path).insert(
        "wb_tokens",
        ciphertext=crypto.encrypt(make_token(MASK_NO_ANALYTICS)),
        exp=str(EXP),
        scopes="",
    )

    assert funnel.history_trouble(client_id, path=db_path) == funnel.NO_CATEGORY

    sent: list = []
    funnel.set_sender(lambda cid, report, data: sent.append(report))
    try:
        report = await funnel.report_task(
            SimpleNamespace(
                client_id=client_id,
                payload={"period": "week", "date": TODAY.isoformat()},
            ),
            path=db_path,
        )
    finally:
        funnel.set_sender(None)

    assert report.span == 0
    assert report.trouble == funnel.NO_CATEGORY
    text = handler.summary_text(report)
    assert "Аналитика" in text and "/connect" in text
    assert "Через пару дней" not in text, "бот снова обещает то, чего не будет"
    assert sent, "отчёт клиенту не ушёл"


@pytest.mark.asyncio
async def test_without_a_connected_cabinet_the_funnel_says_exactly_that(
    db_path, monkeypatch
):
    """Кабинета нет вовсе: это не «подождите», и не «ключ не приняли».

    Токена нет, значит и отказа Wildberries не было: модули на паузу тут
    вставать не должны, а клиент должен услышать про /connect.
    """
    monkeypatch.setenv("ENCRYPTION_KEY", crypto.generate_key())
    wbapi.reset_limits()
    queue.reset()
    client_id = db.admin_repo(db_path).ensure_client(9696)

    assert funnel.history_trouble(client_id, path=db_path) == funnel.NO_CABINET

    funnel.set_sender(lambda cid, report, data: None)
    try:
        report = await funnel.report_task(
            SimpleNamespace(
                client_id=client_id,
                payload={"period": "week", "date": TODAY.isoformat()},
            ),
            path=db_path,
        )
    finally:
        funnel.set_sender(None)

    text = handler.summary_text(report)
    assert report.trouble == funnel.NO_CABINET
    assert "/connect" in text and "Аналитика" in text
    assert "Через пару дней" not in text


@pytest.mark.asyncio
async def test_a_refusal_without_the_token_category_names_the_category(db_path, monkeypatch):
    """Отказ тот же самый, а причина другая, и селлеру она нужна разная."""
    monkeypatch.setenv("ENCRYPTION_KEY", crypto.generate_key())
    wbapi.reset_limits()
    client_id = db.admin_repo(db_path).ensure_client(9494)
    db.repo(client_id, db_path).insert(
        "wb_tokens",
        ciphertext=crypto.encrypt(make_token(MASK_NO_ANALYTICS)),
        exp=str(EXP),
        scopes="",
    )
    put_days(client_id, db_path, PAST_DAYS, 111, opens=100, carts=13, orders=7, buyouts=5)
    put_days(client_id, db_path, NOW_DAYS, 111, opens=100, carts=8, orders=4, buyouts=3)

    assert await fetch_visibility(client_id, db_path, code=403) == funnel.NO_CATEGORY

    text = handler.summary_text(report_of(client_id, db_path, trouble=funnel.NO_CATEGORY))
    assert "Аналитика" in text and "/connect" in text


@pytest.mark.asyncio
async def test_a_silent_wildberries_does_not_break_the_four_steps(cabinet, db_path):
    assert await fetch_visibility(cabinet, db_path, code=503) == funnel.UNAVAILABLE

    report = report_of(cabinet, db_path, trouble=funnel.UNAVAILABLE)
    text = handler.summary_text(report)
    assert report.drop is not None
    assert "не ответил" in text
    assert "они посчитаны полностью" in text


@pytest.mark.asyncio
async def test_the_queued_task_asks_for_visibility_and_then_delivers(
    cabinet, db_path, monkeypatch
):
    """Задача очереди целиком: сходить за пятым этапом и отдать отчёт.

    Поход в Wildberries тут единственный и только за видимостью: сам отчёт
    стоит на базе, и оттуда в Wildberries не ходят.
    """
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json=SEARCH_REPORT)

    real = wbapi.get_wb_client

    def patched(client_id, **kw):
        clock = FakeTime()
        return real(
            client_id,
            http=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
            path=db_path,
            clock=clock.clock,
            sleep=clock.sleep,
        )

    monkeypatch.setattr(wbapi, "get_wb_client", patched)

    sent: list[tuple] = []
    funnel.set_sender(lambda client_id, report, data: sent.append((client_id, report, data)))
    try:
        report = await funnel.report_task(
            SimpleNamespace(
                client_id=cabinet,
                payload={"period": "week", "date": TODAY.isoformat()},
            ),
            path=db_path,
        )
    finally:
        funnel.set_sender(None)

    assert seen == [SEARCH_PATH], "в Wildberries сходили не только за видимостью"
    assert sent and sent[0][0] == cabinet
    assert report.jem is True
    assert sent[0][2].startswith(b"PK"), "книга Excel не собралась"


# --- книга Excel --------------------------------------------------------------


def test_excel_holds_goods_days_and_the_method(cabinet, db_path):
    book = xlsx.read_book(funnel.excel_bytes(report_of(cabinet, db_path)))

    assert set(book.titles) == {
        funnel.ARTICLES_SHEET,
        funnel.DAYS_SHEET,
        funnel.METHOD_SHEET,
    }

    rows = {row.get("Артикул WB"): row for row in book[funnel.ARTICLES_SHEET].rows}
    first = rows[111]
    assert first.get("Товар") == "Кружка синяя"
    assert first.get("Суток в истории") == 7
    assert first.get("Заходы в карточку") == 700
    assert first.get("Из карточки в корзину, %") == 8
    assert first.get("В корзину было, %") == 13
    assert first.get("Просевший этап") == "из карточки в корзину"
    assert first.get("Сравнение") == "сравнивается"

    # Товар с дырами в файле остался, и причина названа прямо в строке.
    assert "неполная история" in rows[333].get("Сравнение")
    assert not rows[333].get("Просевший этап")

    # По дням: семь суток нынешнего периода, и только по сравнимым товарам.
    days = book[funnel.DAYS_SHEET].rows
    assert len(days) == 7
    assert days[0].get("Заходы в карточку") == 150   # 100 у 111 плюс 50 у 222
    assert days[0].get("Товаров собрано") == 2

    # Методология называет и Джем, и то, что видимость это не показы.
    method = " ".join(
        str(row.get("Пояснение")) for row in book[funnel.METHOD_SHEET].rows
    )
    assert "Джем" in method
    assert "НЕ количество показов" in method


# --- команда бота -------------------------------------------------------------


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
async def test_funnel_offers_periods_and_only_queues_the_work(cabinet, db_path):
    """В WB из хендлера никто не ходит: команда ставит задачу и отвечает."""
    queue.reset()
    telegram_id = int(db.admin_repo(db_path).client(cabinet)["telegram_id"])

    message = FakeMessage()
    await handler.funnel_command(FakeUpdate(telegram_id, message), None, path=db_path)
    buttons = message.sent[0][1]["reply_markup"].inline_keyboard
    assert [row[0].callback_data for row in buttons] == [
        "funnel:week",
        "funnel:month",
        "funnel:quarter",
    ]

    query = FakeQuery("funnel:month", FakeMessage())
    await handler.period_chosen(
        FakeUpdate(telegram_id, query.message, query), None, path=db_path
    )
    assert query.answered == "Принято"

    tasks = db.admin_repo(db_path).tasks_by_kind(funnel.TASK_KIND)
    assert len(tasks) == 1
    assert json.loads(tasks[0]["payload"])["period"] == "month"


@pytest.mark.asyncio
async def test_a_made_up_period_puts_nothing_in_the_queue(cabinet, db_path):
    queue.reset()
    telegram_id = int(db.admin_repo(db_path).client(cabinet)["telegram_id"])

    for data in ("funnel:year", "funnel:", "funnel:всё"):
        query = FakeQuery(data, FakeMessage())
        await handler.period_chosen(
            FakeUpdate(telegram_id, query.message, query), None, path=db_path
        )
    assert db.admin_repo(db_path).tasks_by_kind(funnel.TASK_KIND) == []


@pytest.mark.asyncio
async def test_the_command_without_the_module_offers_to_buy_it(db_path):
    """Нет доступа - предложение вместо отказа, и кнопка «Оформить»."""
    queue.reset()
    client_id = db.admin_repo(db_path).ensure_client(5353)
    guard = tariffs.require_module(funnel.MODULE, path=db_path)

    message = FakeMessage()
    command = guard(functools.partial(handler.funnel_command, path=db_path))
    await command(FakeUpdate(5353, message), None)

    text, kwargs = message.sent[-1]
    assert "Воронка" in text and "490" in text
    assert kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "buy:funnel"
    assert access.has_access(client_id, funnel.MODULE, path=db_path) is False


@pytest.mark.asyncio
async def test_a_forged_callback_does_not_run_the_paid_report_either(db_path):
    """Нарисованная кнопка прав не даёт, и выдуманная тоже.

    Клиент этой клавиатуры не видел: `callback_data` он сочинил сам. Проверка
    стоит в самой команде, а не в том, нарисовали мы кнопку или нет.
    """
    queue.reset()
    db.admin_repo(db_path).ensure_client(5454)
    guard = tariffs.require_module(funnel.MODULE, path=db_path)

    message = FakeMessage()
    query = FakeQuery("funnel:quarter", message)
    pressed = guard(functools.partial(handler.period_chosen, path=db_path))
    await pressed(FakeUpdate(5454, message, query), None)

    text, kwargs = message.sent[-1]
    assert "Воронка" in text
    assert kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "buy:funnel"
    assert db.admin_repo(db_path).tasks_by_kind(funnel.TASK_KIND) == [], (
        "платный отчёт встал в очередь по выдуманной кнопке"
    )


# --- чужой текст в сообщении --------------------------------------------------

# Ловушка короткая нарочно: длинное название в строке топа режется по ширине
# экрана, и проверка «текст не потерян» проверяла бы срез, а не экранирование.
TRAP = '<a href="u">нажми</a>'


def test_a_product_name_from_the_cabinet_does_not_become_markup(cabinet, db_path):
    """Название товара пишет селлер, и это чужой текст.

    Сводка уходит с ParseMode.HTML: осмысленная угловая скобка стала бы
    ссылкой от имени бота, случайная ошибкой Telegram, и тогда отчёта клиент
    не увидит вовсе.
    """
    db.repo(cabinet, db_path).upsert("card_names", {"nm_id": 111}, title=TRAP)
    text = handler.summary_text(report_of(cabinet, db_path))

    assert "<a href" not in text
    assert "&lt;a href=&quot;u&quot;&gt;" in text
