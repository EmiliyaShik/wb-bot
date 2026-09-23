"""Агент 4, план-факт: накопление суток, утренний отчёт, остатки.

Швы те же два, что и у всех: транспорт WB (httpx.MockTransport с записанными
ответами, сети нет ни байта) и путь к базе (временный файл). Расчёты -
прогноз, ДРР, дни остатка - проверяются напрямую, они чистые.

Ожидаемые числа посчитаны руками и записаны в комментариях рядом: считать их
тем же способом, что и код, значит не проверить ничего.
"""

from __future__ import annotations

import base64
import json
from datetime import date
from decimal import Decimal

import httpx
import pytest

from agents import rnp
from core import access, crypto, db, queue, wbapi

# Маска из документации: Контент 1, Аналитика 2, Статистика 5, Продвижение 6,
# Финансы 13 - то есть 2 + 4 + 32 + 64 + 8192 = 8294. Посчитана руками.
MASK_FIVE = 8294
EXP = 1789000000
DAY = "2026-09-15"


def make_token() -> str:
    """JWT с нужным payload. Подпись не проверяется, она тут не нужна."""

    def part(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    body = {"id": "ab" * 8, "sid": "s" * 8, "exp": EXP, "s": MASK_FIVE, "acc": 3}
    return f"{part({'alg': 'HS256', 'typ': 'JWT'})}.{part(body)}.c2lnbmF0dXJl"


TOKEN = make_token()


class FakeTime:
    """Часы и пауза под контролем теста: бюджет запросов к WB не ждём вживую."""

    def __init__(self) -> None:
        self.now = 1000.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += float(seconds)


FUNNEL = {
    "data": {
        "products": [
            {
                "nmID": 111,
                "history": [
                    {
                        "date": DAY,
                        "openCount": 1000,
                        "cartCount": 80,
                        "orderCount": 5,
                        "orderSum": 10000,
                        "buyoutCount": 3,
                        "buyoutSum": 6000,
                        "buyoutPercent": 60,
                        "cartToOrderConversion": 6.25,
                    }
                ],
            }
        ]
    }
}

STOCKS = {
    "data": {
        "items": [
            {"nmId": 111, "quantity": 50, "inWayToClient": 10, "inWayFromClient": 0},
            {"nmId": 111, "quantity": 30, "inWayToClient": 0, "inWayFromClient": 0},
        ]
    }
}

CAMPAIGNS = {"adverts": [{"advert_list": [{"advertId": 777}]}]}

FULLSTATS = [
    {
        "advertId": 777,
        "days": [
            {
                "date": DAY,
                "apps": [
                    {"appType": 1, "nms": [{"nmId": 111, "sum": 500, "views": 1000, "clicks": 50}]},
                    {"appType": 32, "nms": [{"nmId": 111, "sum": 250, "views": 100, "clicks": 5}]},
                ],
            }
        ],
    }
]


def wb_http(**overrides) -> httpx.AsyncClient:
    """Записанные ответы WB, разложенные по путям. Сети в тестах нет."""
    answers = {
        "/api/analytics/v3/sales-funnel/products/history": FUNNEL,
        "/api/analytics/v1/stocks-report/wb-warehouses": STOCKS,
        "/adv/v1/promotion/count": CAMPAIGNS,
        "/adv/v3/fullstats": FULLSTATS,
    }
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
    # Бюджет запросов к WB общий на процесс: без сброса соседний тест ждал бы
    # чужую паузу.
    wbapi.reset_limits()
    queue.reset()
    client_id = db.admin_repo(db_path).ensure_client(4040)
    db.repo(client_id, db_path).insert(
        "wb_tokens", ciphertext=crypto.encrypt(TOKEN), exp=str(EXP), scopes="analytics"
    )
    return client_id


async def collect(client_id, db_path, **kw):
    time = FakeTime()
    return await rnp.collect(
        client_id,
        day=date(2026, 9, 15),
        path=db_path,
        http=kw.pop("http", None) or wb_http(),
        clock=time.clock,
        sleep=time.sleep,
        **kw,
    )


# --- чистые расчёты ---


def test_drr_is_ad_spend_share_of_revenue():
    # 1500 рублей рекламы при выручке 30000 это 5 процентов.
    assert rnp.drr(Decimal("1500"), Decimal("30000")) == Decimal("5")


def test_drr_without_revenue_is_not_a_division_by_zero():
    assert rnp.drr(Decimal("1500"), Decimal("0")) is None
    assert rnp.drr(Decimal("0"), Decimal("0")) is None


def test_forecast_multiplies_current_pace_to_the_end_of_month():
    # 100000 за 10 дней это 10000 в день, за 30 дней выходит 300000.
    assert rnp.forecast(Decimal("100000"), 10, 30) == Decimal("300000")


def test_forecast_on_the_first_day_of_month_does_not_divide_by_zero():
    assert rnp.forecast(Decimal("0"), 0, 30) == Decimal("0")


def test_stock_days_counts_how_long_the_stock_lasts():
    # 90 штук при 10 заказах в день это 9 дней.
    assert rnp.stock_days(90, Decimal("10")) == 9


def test_stock_days_without_sales_is_unknown_not_infinity():
    assert rnp.stock_days(90, Decimal("0")) is None


def test_percent_of_plan_is_none_without_a_target():
    # 250000 из 500000 это половина плана.
    assert rnp.percent(Decimal("250000"), Decimal("500000")) == Decimal("50")
    assert rnp.percent(Decimal("250000"), Decimal("0")) is None
    assert rnp.percent(Decimal("250000"), None) is None


# --- шов «транспорт»: сбор суток ---


@pytest.mark.asyncio
async def test_collect_stores_a_day_per_article_from_three_sources(db_path, cabinet):
    await collect(cabinet, db_path)

    row = db.repo(cabinet, db_path).one("nm_daily", date=DAY, nm_id=111)
    assert row is not None
    assert row["orders"] == 5
    assert row["orders_sum_kop"] == 1000000  # 10000 рублей это миллион копеек
    assert row["buyouts"] == 3
    assert row["open_card_count"] == 1000
    assert row["add_to_cart_count"] == 80
    # Остаток по артикулу это сумма по складам: (50 + 10) + 30 = 90.
    assert row["stocks_wb"] == 90
    # Расход рекламы суммируется по площадкам: 500 + 250 = 750 рублей.
    assert row["ad_spend_kop"] == 75000
    assert row["views"] == 1100
    assert row["clicks"] == 55
    assert row["raw"]


@pytest.mark.asyncio
async def test_collect_survives_a_token_without_promotion_category(db_path, cabinet):
    """Без категории «Продвижение» пропадает только реклама, а не весь день."""
    await collect(cabinet, db_path, http=wb_http(**{"/adv/v1/promotion/count": 403}))

    row = db.repo(cabinet, db_path).one("nm_daily", date=DAY, nm_id=111)
    assert row is not None
    assert row["orders"] == 5
    assert row["ad_spend_kop"] == 0


def test_collection_goes_to_everyone_connected_even_without_a_subscription(db_path, cabinet):
    """Выключенная рассылка это «не присылай отчёт», а не «не копи историю»."""
    assert access.has_access(cabinet, rnp.MODULE, path=db_path) is False

    queued = rnp.fan_out_collect(rnp_task({"date": DAY}), path=db_path)
    tasks = db.repo(cabinet, db_path).rows("tasks", kind=rnp.COLLECT_ONE)

    assert len(queued) == 1
    assert len(tasks) == 1
    # А вот утренний отчёт без доступа не рассылается.
    assert rnp.fan_out_report(rnp_task({"date": DAY}), path=db_path) == []


def rnp_task(payload):
    return queue.Task(id=1, client_id=None, kind=rnp.COLLECT_ALL, payload=payload, attempts=0)


# --- утренний отчёт: шов «путь к базе» ---

TODAY = date(2026, 9, 16)  # сентябрь, 30 дней; вчера это 15-е, прошло 15 дней


def put_day(client_id, db_path, day, *, nm_id=111, orders=10, revenue="1000", ad="0", stock=None):
    """Одни накопленные сутки по артикулу, как их сложил бы утренний сбор."""
    db.repo(client_id, db_path).upsert(
        "nm_daily",
        {"date": day, "nm_id": nm_id},
        orders=orders,
        orders_sum_kop=db.to_kop(Decimal(revenue)),
        ad_spend_kop=db.to_kop(Decimal(ad)),
        stocks_wb=stock,
    )


@pytest.fixture
def half_a_month(db_path, cabinet):
    """Полмесяца накопленных суток: по 10 заказов и 1000 рублей в день."""
    for number in range(1, 16):
        day = date(2026, 9, number).isoformat()
        put_day(cabinet, db_path, day, stock=90 if number == 15 else None)
    return cabinet


def test_daily_puts_yesterday_against_the_week_average(db_path, half_a_month):
    report = rnp.daily(half_a_month, today=TODAY, path=db_path)

    assert report.has_data is True
    assert report.date == date(2026, 9, 15)
    assert report.orders == 10
    assert report.revenue == Decimal("1000")
    # Неделя до вчера это 8-14 сентября: по 10 заказов и 1000 рублей в день.
    assert report.avg_orders == Decimal("10")
    assert report.avg_revenue == Decimal("1000")


def test_daily_counts_plan_fact_and_forecast_by_current_pace(db_path, half_a_month):
    rnp.set_plan(half_a_month, "2026-09", revenue=Decimal("60000"), orders=600, path=db_path)

    report = rnp.daily(half_a_month, today=TODAY, path=db_path)

    # С 1 по 15 сентября накопилось 150 заказов и 15000 рублей.
    assert report.month_orders == 150
    assert report.month_revenue == Decimal("15000")
    # 15000 из плана 60000 это 25 процентов.
    assert report.revenue_percent == Decimal("25")
    # Темп 1000 рублей в день, в сентябре 30 дней: к концу месяца 30000.
    assert report.forecast_revenue == Decimal("30000")
    assert report.forecast_orders == 300


def test_daily_without_a_plan_keeps_the_rest_of_the_report(db_path, half_a_month):
    """R125: план не задан - отчёт приходит без блока план-факт и не падает."""
    report = rnp.daily(half_a_month, today=TODAY, path=db_path)

    assert report.plan is None
    assert report.revenue_percent is None
    assert report.orders_percent is None
    # Всё остальное на месте: факт месяца и прогноз считаются и без плана.
    assert report.month_revenue == Decimal("15000")
    assert report.forecast_revenue == Decimal("30000")


def test_daily_names_articles_that_run_out_in_less_than_two_weeks(db_path, half_a_month):
    report = rnp.daily(half_a_month, today=TODAY, path=db_path)

    # Остаток 90 штук при 10 заказах в день это 9 дней, меньше порога в 14.
    assert [(risk.nm_id, risk.days) for risk in report.risks] == [(111, 9)]


def test_daily_keeps_quiet_about_articles_with_stock_for_a_long_time(db_path, half_a_month):
    put_day(half_a_month, db_path, "2026-09-15", stock=900)

    report = rnp.daily(half_a_month, today=TODAY, path=db_path)

    assert report.risks == ()


def test_daily_without_yesterday_data_does_not_crash(db_path, cabinet):
    report = rnp.daily(cabinet, today=TODAY, path=db_path)

    assert report.has_data is False
    assert report.orders == 0
    assert report.revenue == Decimal("0")
    assert report.avg_orders is None
    assert report.drr is None
    assert report.risks == ()


def test_daily_with_zero_revenue_does_not_divide_by_zero(db_path, cabinet):
    put_day(cabinet, db_path, "2026-09-15", orders=0, revenue="0", ad="500", stock=90)

    report = rnp.daily(cabinet, today=TODAY, path=db_path)

    assert report.has_data is True
    assert report.ad_spend == Decimal("500")
    assert report.drr is None
    assert report.risks == ()


def test_daily_on_the_first_day_of_month_does_not_divide_by_zero(db_path, cabinet):
    put_day(cabinet, db_path, "2026-08-31", orders=3, revenue="300")

    report = rnp.daily(cabinet, today=date(2026, 9, 1), path=db_path)

    assert report.days_passed == 0
    assert report.month_revenue == Decimal("0")
    assert report.forecast_revenue == Decimal("0")


def test_plan_is_stored_per_month_and_can_be_changed(db_path, cabinet):
    rnp.set_plan(cabinet, "2026-09", revenue=Decimal("60000"), orders=600, path=db_path)
    rnp.set_plan(cabinet, "2026-09", revenue=Decimal("70000"), orders=700, path=db_path)

    plan = rnp.plan_of(cabinet, "2026-09", path=db_path)

    assert plan.revenue == Decimal("70000")
    assert plan.orders == 700
    assert rnp.plan_of(cabinet, "2026-10", path=db_path) is None
    assert db.repo(cabinet, db_path).count("plans") == 1


# --- хендлер: тексты и команды ---

from types import SimpleNamespace  # noqa: E402

from bot.handlers import rnp as handler  # noqa: E402


class FakeMessage:
    def __init__(self):
        self.sent: list[str] = []

    async def reply_text(self, text, **kwargs):
        self.sent.append(text)
        return self

    @property
    def last(self) -> str:
        return self.sent[-1] if self.sent else ""


class FakeUpdate:
    def __init__(self, telegram_id, message):
        self.effective_user = SimpleNamespace(id=telegram_id)
        self.effective_message = message
        self.message = message


def test_report_text_without_a_plan_comes_without_the_plan_fact_block(db_path, half_a_month):
    """R125 дословно: без плана отчёт приходит, просто без этого блока."""
    text = handler.report_text(rnp.daily(half_a_month, today=TODAY, path=db_path))

    assert "План" not in text
    assert "процент" not in text.lower()
    # Всё остальное на месте: вчерашний день, реклама, остатки.
    assert "Заказы" in text
    assert "9 дней" in text


def test_report_text_with_a_plan_shows_percent_and_forecast(db_path, half_a_month):
    rnp.set_plan(half_a_month, "2026-09", revenue=Decimal("60000"), orders=600, path=db_path)

    text = handler.report_text(rnp.daily(half_a_month, today=TODAY, path=db_path))

    assert "План на сентябрь" in text
    assert "25%" in text  # 15000 из 60000
    assert "300 заказов" in text  # прогноз по темпу 10 заказов в день


def test_report_text_says_plainly_that_there_is_no_data_yet(db_path, cabinet):
    text = handler.report_text(rnp.daily(cabinet, today=TODAY, path=db_path))

    assert "данных" in text.lower()
    assert "9 дней" not in text


@pytest.mark.asyncio
async def test_plan_command_stores_the_plan_for_the_current_month(db_path, cabinet):
    message = FakeMessage()

    await handler.plan_command(
        FakeUpdate(4040, message),
        SimpleNamespace(args=["500000", "300"]),
        path=db_path,
        today=TODAY,
    )

    plan = rnp.plan_of(cabinet, "2026-09", path=db_path)
    assert plan.revenue == Decimal("500000")
    assert plan.orders == 300
    assert "500" in message.last


@pytest.mark.asyncio
async def test_plan_command_without_numbers_explains_how_to_set_it(db_path, cabinet):
    message = FakeMessage()

    await handler.plan_command(
        FakeUpdate(4040, message), SimpleNamespace(args=[]), path=db_path, today=TODAY
    )

    assert rnp.plan_of(cabinet, "2026-09", path=db_path) is None
    assert "/plan" in message.last


@pytest.mark.asyncio
async def test_rnp_command_puts_a_task_in_the_queue_instead_of_calling_wb(db_path, cabinet):
    message = FakeMessage()

    await handler.rnp_command(
        FakeUpdate(4040, message), SimpleNamespace(args=[]), path=db_path, today=TODAY
    )

    tasks = db.repo(cabinet, db_path).rows("tasks", kind=rnp.REPORT_ONE)
    assert len(tasks) == 1


class FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


class FakeApp:
    def __init__(self):
        self.bot = FakeBot()
        self.added: list = []

    def add_handler(self, handler, group=0):
        self.added.append(handler)


def test_register_puts_commands_and_both_daily_jobs_in_place(db_path, cabinet):
    from core import scheduler

    scheduler.reset()
    app = FakeApp()

    handler.register(app, path=db_path)

    assert len(app.added) == 2
    # Сбор и рассылка это две разные утренние работы: одна идёт всем,
    # вторая только подписчикам.
    assert set(scheduler.daily_names()) == {rnp.COLLECT_ALL}
    assert rnp.COLLECT_ONE in queue.handlers()
    assert rnp.REPORT_ONE in queue.handlers()


@pytest.mark.asyncio
async def test_morning_report_reaches_the_client_as_text(db_path, half_a_month):
    app = FakeApp()
    handler.register(app, path=db_path)
    task = queue.Task(
        id=7, client_id=half_a_month, kind=rnp.REPORT_ONE, payload={"date": "2026-09-16"}, attempts=0
    )

    await rnp.report_client(task, path=db_path)

    chat_id, text = app.bot.sent[0]
    assert chat_id == 4040
    assert "15 сентября" in text
    assert "9 дней" in text


def test_report_text_does_not_pass_two_days_off_as_a_week(db_path, cabinet):
    """Короткая база сравнения называется своим числом дней, а не неделей."""
    # Неделя до вчера это 8-14 сентября, собрано из неё только два дня.
    put_day(cabinet, db_path, "2026-09-13")
    put_day(cabinet, db_path, "2026-09-14")
    put_day(cabinet, db_path, "2026-09-15", stock=900)

    text = handler.report_text(rnp.daily(cabinet, today=TODAY, path=db_path))

    assert "неделю" not in text
    assert "2 дня" in text


# --- как отчёт выглядит для человека ---

NB = "\u00a0"  # неразрывный пробел: им rubles() разделяет разряды и отбивает знак


def test_report_text_reads_exactly_as_a_human_would_write_it(db_path, half_a_month):
    """Целая строка, а не наличие чисел: склейки видно только так.

    Знак рубля ставит сама rubles(), поэтому второго знака рядом быть не может.
    """
    rnp.set_plan(half_a_month, "2026-09", revenue=Decimal("60000"), orders=600, path=db_path)

    text = handler.report_text(rnp.daily(half_a_month, today=TODAY, path=db_path))

    assert text == "\n".join(
        [
            "📈 <b>Отчёт за 15 сентября</b>",
            "",
            "Заказы: 10, в среднем 10 заказов в день за прошлую неделю",
            f"Выручка: 1{NB}000{NB}₽, в среднем 1{NB}000{NB}₽ в день за прошлую неделю",
            "",
            "<b>План на сентябрь</b>",
            f"Выручка: 15{NB}000{NB}₽ из 60{NB}000{NB}₽, это 25%",
            "Заказы: 150 из 600, это 25%",
            f"По нынешнему темпу к концу месяца выйдет 30{NB}000{NB}₽ и 300 заказов.",
            "",
            "<b>Реклама за вчера</b>",
            "Расхода не было.",
            "",
            "<b>Скоро закончится</b>",
            "Артикул 111: осталось 90 штук, при нынешней скорости хватит на 9 дней.",
        ]
    )


def test_ad_spend_line_shows_one_rouble_sign_and_the_drr(db_path, cabinet):
    put_day(cabinet, db_path, "2026-09-15", orders=1, revenue="10000", ad="700", stock=1)

    text = handler.report_text(rnp.daily(cabinet, today=TODAY, path=db_path))

    # 700 рублей рекламы при выручке 10000 это ДРР 7 процентов.
    assert f"Расход: 700{NB}₽, ДРР 7%" in text
    # Один остаток при одном заказе в день это ровно один день, и он в отчёте.
    assert "осталось 1 штука, при нынешней скорости хватит на 1 день." in text


@pytest.mark.parametrize(
    "case",
    ["с планом", "без плана", "без данных", "нулевая выручка"],
)
def test_no_report_ever_shows_a_doubled_rouble_sign(db_path, half_a_month, case):
    if case == "с планом":
        rnp.set_plan(half_a_month, "2026-09", revenue=Decimal("60000"), path=db_path)
    if case == "нулевая выручка":
        put_day(half_a_month, db_path, "2026-09-15", orders=0, revenue="0", ad="500")
    today = date(2027, 1, 2) if case == "без данных" else TODAY

    text = handler.report_text(rnp.daily(half_a_month, today=today, path=db_path))

    assert f"₽{NB}₽" not in text
    assert "₽ ₽" not in text
    assert "  " not in text
    assert "\n\n\n" not in text
    for mark in (" ,", " .", " :"):
        assert mark not in text


@pytest.mark.asyncio
async def test_plan_confirmation_shows_one_rouble_sign(db_path, cabinet):
    message = FakeMessage()

    await handler.plan_command(
        FakeUpdate(4040, message),
        SimpleNamespace(args=["500000", "300"]),
        path=db_path,
        today=TODAY,
    )

    assert message.last == "\n".join(
        [
            "План на сентябрь принят.",
            f"Выручка: 500{NB}000{NB}₽",
            "Заказы: 300",
            "",
            "Буду считать выполнение каждое утро.",
        ]
    )


def test_stock_running_out_today_is_not_called_zero_days(db_path, cabinet):
    """«Хватит на 0 дней» это не по-русски и не помогает: так не пишем."""
    put_day(cabinet, db_path, "2026-09-14", orders=4, revenue="400")
    put_day(cabinet, db_path, "2026-09-15", orders=4, revenue="400", stock=1)

    text = handler.report_text(rnp.daily(cabinet, today=TODAY, path=db_path))

    assert "0 дней" not in text
    assert "меньше дня" in text


def test_empty_stock_says_the_goods_ran_out(db_path, cabinet):
    put_day(cabinet, db_path, "2026-09-14", orders=4, revenue="400")
    put_day(cabinet, db_path, "2026-09-15", orders=4, revenue="400", stock=0)

    text = handler.report_text(rnp.daily(cabinet, today=TODAY, path=db_path))

    assert "0 штук" not in text
    assert "закончился" in text


def test_average_says_plainly_that_it_is_a_daily_average(db_path, half_a_month):
    """«В среднем за неделю 10 заказов» читается и как недельный итог. Так нельзя."""
    text = handler.report_text(rnp.daily(half_a_month, today=TODAY, path=db_path))

    assert "в среднем 10 заказов в день за прошлую неделю" in text


@pytest.mark.asyncio
async def test_a_plan_beyond_reason_never_reaches_the_base(db_path, cabinet):
    """Плохой план, попавший в базу, ломает отчёт клиента навсегда.

    Тридцатидвузначная выручка спокойно легла бы в plans, а падало бы уже
    форматирование, каждое утро и по требованию. Селлер такое не чинит:
    отчёт умирает раньше, чем покажет ему план. Значит, отсекать надо до
    записи, и проверять надо состояние базы, а не текст отказа.
    """
    message = FakeMessage()

    await handler.plan_command(
        FakeUpdate(4040, message),
        SimpleNamespace(args=["9" * 32, "300"]),
        path=db_path,
        today=TODAY,
    )

    assert rnp.plan_of(cabinet, "2026-09", path=db_path) is None
    assert db.repo(cabinet, db_path).count("plans") == 0


@pytest.mark.asyncio
async def test_a_refused_plan_leaves_the_daily_report_alive(db_path, cabinet):
    """Главное последствие: после отказа отчёт по-прежнему собирается."""
    put_day(cabinet, db_path, "2026-09-15", orders=4, revenue="400")
    message = FakeMessage()

    await handler.plan_command(
        FakeUpdate(4040, message),
        SimpleNamespace(args=["1" + "0" * 40]),
        path=db_path,
        today=TODAY,
    )

    text = handler.report_text(rnp.daily(cabinet, today=TODAY, path=db_path))
    assert "Заказы: 4" in text


@pytest.mark.asyncio
async def test_a_huge_orders_target_is_refused_too(db_path, cabinet):
    message = FakeMessage()

    await handler.plan_command(
        FakeUpdate(4040, message),
        SimpleNamespace(args=["500000", "9" * 20]),
        path=db_path,
        today=TODAY,
    )

    assert db.repo(cabinet, db_path).count("plans") == 0


@pytest.mark.asyncio
async def test_a_big_but_believable_plan_is_still_accepted(db_path, cabinet):
    """Граница отсекает опечатку, а не крупного селлера."""
    message = FakeMessage()

    await handler.plan_command(
        FakeUpdate(4040, message),
        SimpleNamespace(args=["900000000", "40000"]),
        path=db_path,
        today=TODAY,
    )

    plan = rnp.plan_of(cabinet, "2026-09", path=db_path)
    assert plan.revenue == Decimal("900000000")
    assert plan.orders == 40000


# --- чужой текст в утреннем отчёте ---
#
# Артикул приходит из базы, а туда из ответа Wildberries, и в отчёте он
# стоит рядом с разметкой бота. Сегодня это число, но подстановка тут не про
# сегодняшнее поле: она про то, что следующее поле в этом отчёте числом уже
# может и не быть, а автор шаблона про экранирование не вспомнит.

TRAP = '<a href="http://zlo.example">нажми</a>'


def test_an_article_from_wildberries_does_not_become_markup(db_path, half_a_month):
    from dataclasses import replace

    report = rnp.daily(half_a_month, today=TODAY, path=db_path)
    spoiled = replace(report.risks[0], nm_id=TRAP)

    text = handler.report_text(replace(report, risks=(spoiled,)))

    assert "<a href" not in text
    assert "&lt;a href=&quot;" in text
    assert "<b>" in text  # разметка самого бота при этом на месте
