"""Агент 2, сторож скрытых расходов: пять показателей, пороги, алерты в рублях.

Шов здесь один - путь к базе. Второго (транспорт WB) быть не должно вовсе:
сторож работает на данных агента 1 и в Wildberries не ходит. Это проверяется
отдельно, подставным транспортом, который обязан остаться нетронутым.

Ожидаемые числа посчитаны руками и записаны рядом: считать их тем же
способом, что и код, значит не проверить ничего.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from agents import finance, watchdog
from bot.handlers import dynamics as handler
from core import access, config, db, queue, scheduler, wbapi

TODAY = date(2026, 9, 15)


# --- чистые расчёты ----------------------------------------------------------


def test_share_of_revenue_is_a_percent():
    # 21 000 ₽ при выручке 840 000 ₽ это ровно 2,5 процента.
    assert watchdog.share(Decimal("21000"), Decimal("840000")) == Decimal("2.5")


def test_share_without_revenue_is_not_a_division_by_zero():
    """Нулевая выручка не делится: показателя нет, падения тоже."""
    assert watchdog.share(Decimal("500"), Decimal("0")) is None


def week(number: int, **values) -> watchdog.WeekMetrics:
    """Готовая неделя с показателями. Выручка по умолчанию 840 000 ₽."""
    return watchdog.WeekMetrics(
        report_id=number,
        date_from=f"2026-0{number}-01",
        date_to=f"2026-0{number}-07",
        revenue=Decimal(values.pop("revenue", "840000")),
        **{name: Decimal(str(value)) for name, value in values.items()},
    )


def test_small_change_stays_below_the_threshold_and_says_nothing():
    """Эквайринг 1,4% -> 1,8% это +0,4 п.п., порог 0,5 п.п. не пройден."""
    baseline = [week(n, acquiring="1.4") for n in range(1, 5)]

    alerts = watchdog.compare(week(5, acquiring="1.8"), baseline)

    assert alerts == ()


def test_acquiring_over_the_threshold_costs_rubles_not_points():
    """Пример из ТЗ: 1,4% -> 2,1% при выручке 840 000 ₽.

    Средний эквайринг четырёх недель (1,2 + 1,4 + 1,4 + 1,6) / 4 = 1,4.
    Рост 2,1 - 1,4 = 0,7 п.п., порог 0,5 пройден.
    0,7% от 840 000 = 5 880 ₽ за неделю. Посчитано руками.
    """
    baseline = [
        week(1, acquiring="1.2"),
        week(2, acquiring="1.4"),
        week(3, acquiring="1.4"),
        week(4, acquiring="1.6"),
    ]

    alerts = watchdog.compare(week(5, acquiring="2.1"), baseline)

    assert [alert.metric for alert in alerts] == ["acquiring"]
    alert = alerts[0]
    assert alert.was == Decimal("1.4")
    assert alert.now == Decimal("2.1")
    assert alert.delta == Decimal("0.7")
    assert alert.rubles == Decimal("5880.00")


def test_spp_falls_and_that_is_an_alert_too():
    """Порог СПП отрицательный: тревожит падение, а не рост.

    Среднее 15%, стало 9%: изменение -6 п.п., порог -5 пройден.
    6% от 840 000 = 50 400 ₽ за неделю.
    """
    baseline = [week(n, spp="15") for n in range(1, 5)]

    alerts = watchdog.compare(week(5, spp="9"), baseline)

    assert [alert.metric for alert in alerts] == ["spp"]
    assert alerts[0].delta == Decimal("-6")
    assert alerts[0].rubles == Decimal("50400.00")


def test_thresholds_come_from_the_config_and_not_from_the_code(monkeypatch):
    """Тот же рост эквайринга при пороге 5 п.п. молчит."""
    monkeypatch.setattr(
        config, "settings", lambda: {"alerts": {"acquiring": 5.0, "baseline_weeks": 4}}
    )
    baseline = [week(n, acquiring="1.4") for n in range(1, 5)]

    assert watchdog.compare(week(5, acquiring="2.1"), baseline) == ()
    assert watchdog.baseline_weeks() == 4


# --- на данных агента 1 ------------------------------------------------------


def put_week(
    client_id,
    db_path,
    number: int,
    first_day: str,
    *,
    revenue="100000",
    commission="15000",
    acquiring="1400",
    logistics="8000",
    storage="1000",
    spp=15.0,
    complete=True,
):
    """Неделя в `fin_weeks` и одна строка в `fin_rows` ради процента СПП."""
    start = date.fromisoformat(first_day)
    end = start + timedelta(days=6)
    finance.save_week(
        client_id,
        finance.Week(
            report_id=number,
            date_from=start.isoformat(),
            date_to=end.isoformat(),
            amounts=finance.Amounts(
                revenue=Decimal(revenue),
                commission=Decimal(commission),
                acquiring=Decimal(acquiring),
                logistics=Decimal(logistics),
                storage=Decimal(storage),
            ),
        ),
        complete=complete,
        path=db_path,
    )
    db.repo(client_id, db_path).upsert(
        "fin_rows",
        {"rrd_id": number},
        report_id=number,
        quantity=1,
        retail_amount_kop=db.to_kop(Decimal(revenue)),
        retail_price_withdisc_kop=db.to_kop(Decimal(revenue)),
        ppvz_spp_prc=spp,
    )


@pytest.fixture
def client(db_path):
    return db.admin_repo(db_path).ensure_client(7070)


@pytest.fixture
def five_weeks(db_path, client):
    """Четыре спокойные недели и пятая с выросшей комиссией.

    Комиссия первых четырёх: 15 000 из 100 000 это 15%.
    В последней 18 000 из 100 000 это 18%, рост 3 п.п. при пороге 2.
    3% от 100 000 = 3 000 ₽ за неделю. Посчитано руками.
    """
    days = ["2026-08-10", "2026-08-17", "2026-08-24", "2026-08-31", "2026-09-07"]
    for number, day in enumerate(days, start=1):
        put_week(
            client,
            db_path,
            number,
            day,
            commission="18000" if number == 5 else "15000",
        )
    return client


def test_five_weeks_are_enough_to_name_the_grown_commission(db_path, five_weeks):
    watch = watchdog.check(five_weeks, today=TODAY, path=db_path)

    assert watch.enough
    assert [alert.metric for alert in watch.alerts] == ["commission"]
    assert watch.alerts[0].was == Decimal("15")
    assert watch.alerts[0].now == Decimal("18")
    assert watch.alerts[0].rubles == Decimal("3000")


def test_three_weeks_say_how_many_are_still_missing_instead_of_crashing(db_path, client):
    for number, day in enumerate(["2026-08-24", "2026-08-31", "2026-09-07"], start=1):
        put_week(client, db_path, number, day)

    watch = watchdog.check(client, today=TODAY, path=db_path)

    assert not watch.enough
    assert watch.alerts == ()
    assert watch.have == 3
    assert watch.needed == 5
    assert watch.missing == 2


def test_empty_history_does_not_crash(db_path, client):
    watch = watchdog.check(client, today=TODAY, path=db_path)

    assert not watch.enough
    assert watch.have == 0
    assert watch.alerts == ()


def test_zero_revenue_week_is_not_a_division_by_zero(db_path, client):
    days = ["2026-08-10", "2026-08-17", "2026-08-24", "2026-08-31"]
    for number, day in enumerate(days, start=1):
        put_week(client, db_path, number, day)
    put_week(client, db_path, 5, "2026-09-07", revenue="0", commission="18000")

    watch = watchdog.check(client, today=TODAY, path=db_path)

    assert watch.week.revenue == Decimal("0")
    assert watch.week.commission is None
    assert watch.alerts == ()


def test_dynamics_gives_a_row_per_week(db_path, five_weeks):
    table = watchdog.dynamics(five_weeks, "quarter", today=TODAY, path=db_path)

    assert len(table.weeks) == 5
    assert table.weeks[0].date_from == "2026-08-10"
    assert table.weeks[-1].commission == Decimal("18")


# --- в Wildberries не ходим --------------------------------------------------


@pytest.fixture
def no_wb(monkeypatch):
    """Подставной транспорт WB, который обязан остаться нетронутым."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        raise AssertionError("сторож постучался в Wildberries")

    session = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(wbapi, "shared_session", lambda: session)

    def client(*args, **kwargs):
        calls.append("get_wb_client")
        raise AssertionError("сторож запросил клиент Wildberries")

    monkeypatch.setattr(wbapi, "get_wb_client", client)
    return calls


@pytest.mark.asyncio
async def test_nothing_reaches_wildberries(db_path, five_weeks, no_wb):
    """R103 дословно: работает на данных агента 1, новых запросов не делает."""
    watchdog.check(five_weeks, today=TODAY, path=db_path)
    watchdog.dynamics(five_weeks, "quarter", today=TODAY, path=db_path)

    sent: list = []
    watchdog.set_delivery(lambda client_id, watch: sent.append(watch))
    access.grant_access(five_weeks, watchdog.MODULE, 30, "test", path=db_path)
    await watchdog.alerts_task(
        SimpleNamespace(client_id=five_weeks, payload={"report_id": 5}), path=db_path
    )

    assert no_wb == []
    assert len(sent) == 1
    assert [alert.metric for alert in sent[0].alerts] == ["commission"]


@pytest.mark.asyncio
async def test_alerts_do_not_reach_a_client_without_the_module(db_path, five_weeks):
    sent: list = []
    watchdog.set_delivery(lambda client_id, watch: sent.append(watch))

    await watchdog.alerts_task(
        SimpleNamespace(client_id=five_weeks, payload={"report_id": 5}), path=db_path
    )

    assert sent == []


def test_weekly_job_waits_for_a_new_finance_report(db_path, five_weeks):
    """Рассылка привязана к новому отчёту, а не к дню недели."""
    scheduler.reset()
    queue.reset()
    watchdog.register_jobs(path=db_path)

    assert watchdog.TASK_KIND in scheduler.weekly_names()
    assert watchdog.latest_report(five_weeks, path=db_path) == 5

    queued = scheduler.check_weekly(db_path)
    kinds = db.repo(five_weeks, db_path).rows("tasks", kind=watchdog.TASK_KIND)
    assert len(queued) == 1
    assert len(kinds) == 1
    # Тот же отчёт второй раз ничего не добавляет.
    scheduler.check_weekly(db_path)
    assert len(db.repo(five_weeks, db_path).rows("tasks", kind=watchdog.TASK_KIND)) == 1


# --- тексты и команда --------------------------------------------------------


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


def test_alert_text_speaks_rubles_and_not_points(db_path, five_weeks):
    """E8 дословно: что изменилось, с чего на что, на сколько и сколько рублей."""
    text = handler.alerts_text(watchdog.check(five_weeks, today=TODAY, path=db_path))

    assert "омиссия" in text
    assert "с 15% до 18%" in text
    assert "3 п.п." in text
    assert "3 000 ₽" in text
    assert "100 000 ₽" in text


def test_short_history_says_how_many_weeks_are_still_needed(db_path, client):
    for number, day in enumerate(["2026-08-24", "2026-08-31", "2026-09-07"], start=1):
        put_week(client, db_path, number, day)

    text = handler.alerts_text(watchdog.check(client, today=TODAY, path=db_path))

    assert "мало данных" in text.lower()
    assert "2 недели" in text
    assert "п.п." not in text


def test_quiet_week_is_said_plainly(db_path, client):
    for number, day in enumerate(
        ["2026-08-10", "2026-08-17", "2026-08-24", "2026-08-31", "2026-09-07"], start=1
    ):
        put_week(client, db_path, number, day)

    text = handler.alerts_text(watchdog.check(client, today=TODAY, path=db_path))

    assert "п.п." not in text
    assert "мало данных" not in text.lower()


@pytest.mark.asyncio
async def test_dynamics_command_answers_with_a_table_by_weeks(db_path, five_weeks):
    message = FakeMessage()

    await handler.dynamics_command(
        FakeUpdate(7070, message),
        SimpleNamespace(args=["квартал"]),
        path=db_path,
        today=TODAY,
    )

    assert "10.08" in message.last
    assert "18%" in message.last
    assert "Выручка" in message.last


@pytest.mark.asyncio
async def test_dynamics_command_without_data_does_not_show_an_empty_table(db_path, client):
    message = FakeMessage()

    await handler.dynamics_command(
        FakeUpdate(7070, message), SimpleNamespace(args=[]), path=db_path, today=TODAY
    )

    assert "Выручка" not in message.last
    assert message.last


class FakeApp:
    def __init__(self):
        self.handlers: list = []
        self.bot = SimpleNamespace(send_message=self._send)
        self.sent: list = []

    def add_handler(self, handler_obj, group=0):
        self.handlers.append(handler_obj)

    async def _send(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


@pytest.mark.asyncio
async def test_register_puts_the_command_and_the_weekly_delivery_in_place(db_path, five_weeks):
    scheduler.reset()
    queue.reset()
    app = FakeApp()

    handler.register(app, path=db_path)
    access.grant_access(five_weeks, watchdog.MODULE, 30, "test-register", path=db_path)
    await watchdog.alerts_task(
        SimpleNamespace(client_id=five_weeks, payload={"report_id": 5}), path=db_path
    )

    assert len(app.handlers) == 1
    assert watchdog.TASK_KIND in scheduler.weekly_names()
    assert app.sent and "3 000 ₽" in app.sent[0][1]


def spp_watch() -> watchdog.Watch:
    """Осмотр с одним алертом: СПП упал с 15% до 9% при выручке 840 000 ₽."""
    baseline = tuple(week(n, spp="15") for n in range(1, 5))
    current = week(5, spp="9")
    return watchdog.Watch(
        client_id=1,
        week=current,
        baseline=baseline,
        alerts=watchdog.compare(current, baseline),
        have=5,
        needed=5,
    )


def test_spp_money_is_not_called_a_withholding(db_path):
    """Сумму по СПП нельзя сложить с расходами: это изменение скидки."""
    text = handler.alerts_text(spp_watch())

    assert "скидк" in text
    assert "50 400 ₽" in text
    # Шаблон удержания достаётся только четырём остальным показателям.
    assert "за неделю это" not in text


def test_withheld_share_is_named_and_the_difference_with_finance_explained(db_path, five_weeks):
    """Две цифры об одном и том же не остаются без объяснения."""
    text = handler.alerts_text(watchdog.check(five_weeks, today=TODAY, path=db_path))

    assert "удержан" in text
    assert "/finance" in text


def test_dynamics_table_says_the_same_about_its_percents(db_path, five_weeks):
    table = watchdog.dynamics(five_weeks, "quarter", today=TODAY, path=db_path)

    text = handler.table_text(table)

    assert "удержал" in text
    assert "/finance" in text
