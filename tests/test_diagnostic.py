"""Бесплатная диагностика: три утечки в рублях, один раз на кабинет WB.

Швы те же два, что и у всех: путь к базе (временный файл) и транспорт WB
(httpx.MockTransport с записанными ответами). Сети нет ни байта.

Ожидаемые рубли посчитаны руками и записаны рядом: считать их тем же
способом, что и код, значит не проверить ничего.
"""

from __future__ import annotations

import base64
import json
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from agents import diagnostic, finance
from bot.handlers import diagnostic as handler
from core import clients, crypto, db, queue, wbapi

TODAY = date(2026, 9, 15)


def put_week(
    client_id,
    db_path,
    number: int,
    first_day: str,
    *,
    revenue="1000000",
    commission="150000",
    acquiring="14000",
    logistics="80000",
    storage="10000",
    spp=15.0,
):
    """Неделя в `fin_weeks` и строка в `fin_rows` ради процента СПП."""
    start = date.fromisoformat(first_day)
    finance.save_week(
        client_id,
        finance.Week(
            report_id=number,
            date_from=start.isoformat(),
            date_to=(start + timedelta(days=6)).isoformat(),
            amounts=finance.Amounts(
                revenue=Decimal(revenue),
                commission=Decimal(commission),
                acquiring=Decimal(acquiring),
                logistics=Decimal(logistics),
                storage=Decimal(storage),
            ),
        ),
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


WEEK_DAYS = ["2026-08-10", "2026-08-17", "2026-08-24", "2026-08-31", "2026-09-07"]


# Маска из документации: Контент 1, Аналитика 2, Статистика 5, Продвижение 6,
# Финансы 13, то есть 2 + 4 + 32 + 64 + 8192 = 8294.
MASK_FIVE = 8294
EXP = 1789000000

DETAILED = "/api/finance/v1/sales-reports/detailed"
LIST = "/api/finance/v1/sales-reports/list"


def make_token() -> str:
    """JWT с нужным payload. Подпись не проверяется, она тут не нужна."""

    def part(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    body = {"id": "ab" * 8, "sid": "sid-1", "exp": EXP, "s": MASK_FIVE, "acc": 3}
    return f"{part({'alg': 'HS256', 'typ': 'JWT'})}.{part(body)}.c2lnbmF0dXJl"


TOKEN = make_token()


class FakeTime:
    """Часы и пауза под контролем теста: лимит WB вживую не ждём."""

    def __init__(self) -> None:
        self.now = 1000.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += float(seconds)


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


@pytest.fixture
def cabinet(db_path, monkeypatch):
    """Клиент с подключённым кабинетом: seller_id и категория «Финансы»."""
    monkeypatch.setenv("ENCRYPTION_KEY", crypto.generate_key())
    wbapi.reset_limits()
    queue.reset()
    diagnostic.set_delivery(None)
    client_id = db.admin_repo(db_path).ensure_client(9090)
    db.admin_repo(db_path).set_client_fields(client_id, seller_id="sid-1")
    db.repo(client_id, db_path).insert(
        "wb_tokens",
        ciphertext=crypto.encrypt(TOKEN),
        exp=str(EXP),
        scopes="finance,analytics",
    )
    return client_id


@pytest.fixture
def five_weeks(db_path, cabinet):
    """Четыре спокойные недели и пятая, в которой поехало четыре статьи.

    Выручка везде 1 000 000 ₽, база одна и та же: комиссия 15%, эквайринг
    1,4%, логистика 8%, хранение 1%.

    В последней неделе, всё посчитано руками:
      логистика  8% -> 12%,  +4 п.п.  -> 40 000 ₽
      комиссия  15% -> 18%,  +3 п.п.  -> 30 000 ₽
      хранение   1% -> 3,5%, +2,5 п.п -> 25 000 ₽
      эквайринг 1,4% -> 2,5%, +1,1 п.п -> 11 000 ₽
    Самых дорогих три, эквайринг в тройку не попадает.
    """
    for number, day in enumerate(WEEK_DAYS, start=1):
        if number == 5:
            put_week(
                cabinet,
                db_path,
                number,
                day,
                commission="180000",
                acquiring="25000",
                logistics="120000",
                storage="35000",
            )
        else:
            put_week(cabinet, db_path, number, day)
    return cabinet


def test_three_most_expensive_leaks_are_named_in_rubles(db_path, five_weeks):
    """C3: три самые дорогие утечки недели, в рублях, по расчёту сторожа."""
    result = diagnostic.run("sid-1", five_weeks, today=TODAY, path=db_path)

    assert result.ok
    assert result.deviation is True
    assert [leak.metric for leak in result.leaks] == [
        "logistics_share",
        "commission",
        "storage_share",
    ]
    assert [leak.rubles for leak in result.leaks] == [
        Decimal("40000"),
        Decimal("30000"),
        Decimal("25000"),
    ]


def test_too_little_history_shows_biggest_costs_and_says_it_is_not_a_deviation(
    db_path, cabinet
):
    """C3: сравнивать не с чем - показываем крупнейшие статьи, без выдумок.

    Две недели вместо пяти. Расходы последней: комиссия 150 000 ₽,
    логистика 80 000 ₽, эквайринг 14 000 ₽, хранение 10 000 ₽.
    В тройку идут три первых, и ни одна из них не отклонение.
    """
    for number, day in enumerate(WEEK_DAYS[3:], start=1):
        put_week(cabinet, db_path, number, day)

    result = diagnostic.run("sid-1", cabinet, today=TODAY, path=db_path)

    assert result.ok
    assert result.deviation is False
    assert not result.enough
    assert result.have == 2
    assert [leak.title for leak in result.leaks] == [
        "комиссия площадки",
        "логистика",
        "эквайринг",
    ]
    assert [leak.rubles for leak in result.leaks] == [
        Decimal("150000"),
        Decimal("80000"),
        Decimal("14000"),
    ]
    assert all(leak.deviation is False for leak in result.leaks)


def test_same_cabinet_from_another_telegram_account_gets_nothing(db_path, five_weeks):
    """R56, R162: диагностика одна на кабинет, а не на Telegram-аккаунт."""
    first = diagnostic.run("sid-1", five_weeks, today=TODAY, path=db_path)
    assert first.ok

    second_account = db.admin_repo(db_path).ensure_client(9191)
    db.admin_repo(db_path).set_client_fields(second_account, seller_id="sid-1")
    db.repo(second_account, db_path).insert(
        "wb_tokens", ciphertext=b"x", exp="2027-01-01 00:00:00", scopes="finance"
    )

    assert diagnostic.availability(second_account, path=db_path) == diagnostic.TAKEN
    refused = diagnostic.run("sid-1", second_account, today=TODAY, path=db_path)
    assert refused.reason == diagnostic.TAKEN
    assert not refused.ok
    assert refused.leaks == ()


def test_second_call_by_the_same_client_shows_the_old_result(db_path, five_weeks):
    """Повторный вызов показывает прошлый разбор, а не считает заново."""
    first = diagnostic.run("sid-1", five_weeks, today=TODAY, path=db_path)

    # Появилась ещё одна неделя, и в ней всё иначе. Прошлый разбор от этого
    # меняться не должен.
    put_week(five_weeks, db_path, 6, "2026-09-14", commission="500000")
    again = diagnostic.run("sid-1", five_weeks, today=date(2026, 9, 22), path=db_path)

    assert again.repeat is True
    assert again.ok
    assert again.date_to == first.date_to
    assert [leak.rubles for leak in again.leaks] == [leak.rubles for leak in first.leaks]
    assert diagnostic.availability(five_weeks, path=db_path) == diagnostic.REPEAT


def test_without_a_cabinet_there_is_nothing_to_diagnose(db_path):
    """Без подключённого кабинета нет sid, а значит и диагностики."""
    lonely = db.admin_repo(db_path).ensure_client(9292)

    assert diagnostic.availability(lonely, path=db_path) == diagnostic.NOT_CONNECTED


def test_token_without_finance_category_cannot_be_diagnosed(db_path):
    """Нет категории «Финансы» - считать нечего, и это надо сказать честно."""
    client_id = db.admin_repo(db_path).ensure_client(9393)
    db.admin_repo(db_path).set_client_fields(client_id, seller_id="sid-2")
    db.repo(client_id, db_path).insert(
        "wb_tokens", ciphertext=b"x", exp="2027-01-01 00:00:00", scopes="analytics,content"
    )

    assert diagnostic.availability(client_id, path=db_path) == diagnostic.NO_CATEGORY


def test_every_visible_module_gets_a_line_from_the_config(db_path, five_weeks):
    """C3a: по строке на каждый видимый модуль, тексты из конфига."""
    from core import config

    result = diagnostic.run("sid-1", five_weeks, today=TODAY, path=db_path)

    shown = {line.module for line in result.lines}
    assert shown == {
        name for name, info in config.modules().items() if info.visible and info.diagnostic_line
    }
    # Владелец открыл всё, включая пакет: строку получает каждый модуль.
    assert shown == set(config.visible_modules())
    assert {"ads", "funnel", "all"} <= shown
    for line in result.lines:
        assert line.line == config.modules()[line.module].diagnostic_line


# --- шов транспорта WB -------------------------------------------------------


@pytest.mark.asyncio
async def test_task_goes_to_wb_for_the_week_and_then_delivers_the_result(
    db_path, five_weeks
):
    """Данные собирает задача очереди, а не хендлер: в WB ходят отсюда."""
    seen: list[str] = []

    def transport(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json=[])

    sent: list[diagnostic.Diagnostic] = []
    diagnostic.set_delivery(lambda client_id, result: sent.append(result))
    time = FakeTime()
    task = SimpleNamespace(client_id=five_weeks, kind=diagnostic.TASK_KIND, payload={})

    await diagnostic.diagnostic_task(
        task,
        path=db_path,
        http=httpx.AsyncClient(transport=httpx.MockTransport(transport)),
        clock=time.clock,
        sleep=time.sleep,
        today=TODAY,
    )

    assert DETAILED in seen
    assert len(sent) == 1
    assert sent[0].ok
    assert len(sent[0].leaks) == 3


@pytest.mark.asyncio
async def test_command_puts_a_task_in_the_queue_and_never_calls_wb(db_path, five_weeks):
    """Из хендлера в Wildberries не ходят: у транспорта нет ни одного вызова."""

    def transport(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError(f"хендлер пошёл в WB: {request.url}")

    message = FakeMessage()
    await handler.diagnostic_command(
        FakeUpdate(9090, message),
        SimpleNamespace(args=[], bot_data={}),
        path=db_path,
        http=httpx.AsyncClient(transport=httpx.MockTransport(transport)),
    )

    tasks = db.admin_repo(db_path).tasks_by_kind(diagnostic.TASK_KIND)
    assert len(tasks) == 1
    assert int(tasks[0]["client_id"]) == five_weeks


# --- тексты ------------------------------------------------------------------


def test_report_names_rubles_and_what_each_paid_module_would_find(db_path, five_weeks):
    """C3 и C3a в одном сообщении: рубли утечек и строки модулей из конфига."""
    from core import config

    text = handler.report_text(
        diagnostic.run("sid-1", five_weeks, today=TODAY, path=db_path)
    )

    # rubles() разделяет разряды неразрывным пробелом: в тексте бота он и нужен.
    assert "40 000 ₽" in text
    assert "30 000 ₽" in text
    assert "25 000 ₽" in text
    for name, info in config.visible_modules().items():
        if info.diagnostic_line:
            assert info.diagnostic_line in text


def test_report_says_plainly_that_biggest_costs_are_not_a_deviation(db_path, cabinet):
    """Мало истории: честная оговорка вместо выдуманного отклонения."""
    put_week(cabinet, db_path, 1, WEEK_DAYS[4])

    text = handler.report_text(
        diagnostic.run("sid-1", cabinet, today=TODAY, path=db_path)
    )

    assert "не отклонение" in text
    assert "150 000 ₽" in text


def test_refusals_say_what_to_do_next(db_path):
    """Отказ без объяснения это не отказ, а тупик."""
    lonely = db.admin_repo(db_path).ensure_client(9494)
    without_finance = db.admin_repo(db_path).ensure_client(9595)
    db.admin_repo(db_path).set_client_fields(without_finance, seller_id="sid-9")
    db.repo(without_finance, db_path).insert(
        "wb_tokens", ciphertext=b"x", exp="2027-01-01 00:00:00", scopes="analytics"
    )

    assert "/connect" in handler.refusal_text(
        diagnostic.availability(lonely, path=db_path)
    )
    no_category = handler.refusal_text(
        diagnostic.availability(without_finance, path=db_path)
    )
    assert "Финансы" in no_category and "/connect" in no_category
    taken = handler.refusal_text(diagnostic.TAKEN)
    assert "кабинет" in taken.lower() and "Telegram" in taken


# --- ничего лишнего в базе и в ответе ----------------------------------------


def test_nothing_of_the_client_money_survives_disconnect(db_path, five_weeks):
    """R32 и R71: после удаления клиента его цифр в базе не остаётся.

    Таблица `diagnostics` ключуется по кабинету и клиентской не является,
    поэтому она переживает удаление. Значит в ней не должно быть ни денег,
    ни процентов, ни ссылки на клиента. Удаление по сроку хранения идёт той
    же дверью (`lifecycle.erase` зовёт `clients.disconnect`).
    """
    diagnostic.run("sid-1", five_weeks, today=TODAY, path=db_path)

    clients.disconnect(five_weeks, path=db_path)

    row = db.admin_repo(db_path).diagnostic("sid-1")
    assert row is not None  # кабинет всё ещё «разобран», и это не лазейка
    summary = str(row["summary"] or "")
    assert summary == diagnostic.DONE_MARK
    assert not any(char.isdigit() for char in summary)
    assert db.repo(five_weeks, db_path).count("fin_weeks") == 0

    # Тот же кабинет на новом аккаунте второго разбора не получает.
    again = db.admin_repo(db_path).ensure_client(9696)
    db.admin_repo(db_path).set_client_fields(again, seller_id="sid-1")
    db.repo(again, db_path).insert(
        "wb_tokens", ciphertext=b"x", exp="2027-01-01 00:00:00", scopes="finance"
    )
    assert diagnostic.availability(again, path=db_path) == diagnostic.TAKEN


def test_repeat_without_data_says_so_instead_of_showing_old_numbers(db_path, five_weeks):
    """Цифр больше нет - повтор говорит правду, а не показывает копию."""
    diagnostic.run("sid-1", five_weeks, today=TODAY, path=db_path)
    db.repo(five_weeks, db_path).delete("fin_weeks")
    db.repo(five_weeks, db_path).delete("fin_rows")

    result = diagnostic.run("sid-1", five_weeks, today=TODAY, path=db_path)

    assert result.reason == diagnostic.GONE
    assert result.leaks == ()
    assert "удалены" in handler.report_text(result)


def test_the_shown_week_is_named_in_the_result(db_path, five_weeks, cabinet):
    """Какая неделя показана, видно в самом разборе, а не выводится из флагов."""
    result = diagnostic.run("sid-1", five_weeks, today=TODAY, path=db_path)

    # Отклонения считаются по последней полной неделе, это отчёт номер 5.
    assert result.report_id == 5
    assert result.date_to == "2026-09-13"


@pytest.mark.asyncio
async def test_free_report_does_not_hand_out_the_paid_one(db_path, cabinet):
    """Бесплатное остаётся бесплатным: три утечки, без артикулов и без файла.

    Поехали все пять показателей сразу, включая СПП (15% -> 9%). Утечек пять,
    показать можно только три: остальное это уже платный модуль.
    """
    for number, day in enumerate(WEEK_DAYS, start=1):
        if number == 5:
            put_week(
                cabinet,
                db_path,
                number,
                day,
                commission="180000",
                acquiring="25000",
                logistics="120000",
                storage="35000",
                spp=9.0,
            )
        else:
            put_week(cabinet, db_path, number, day)

    result = diagnostic.run("sid-1", cabinet, today=TODAY, path=db_path)
    text = handler.report_text(result)

    assert len(result.leaks) == 3
    # Четвёртой строки нет, хотя посчитано пять: остальное это платный модуль.
    assert "4." not in text
    # Таблицы по артикулам тоже нет. В платных отчётах она идёт моноширинным
    # блоком, здесь его нет ни одного.
    assert "<pre>" not in text
    assert "vendor" not in text.lower()

    class Bot:
        def __init__(self) -> None:
            self.messages: list[str] = []

        async def send_message(self, chat_id, text, **kwargs):
            self.messages.append(text)

        async def send_document(self, *args, **kwargs):  # pragma: no cover
            raise AssertionError("бесплатный разбор не отдаёт файлов")

    app = SimpleNamespace(bot=Bot())
    await handler.make_delivery(app, db_path)(cabinet, result)

    assert len(app.bot.messages) == 1


# --- чужой текст в бесплатном разборе ---
#
# Строка «что найдут платные модули» и название модуля лежат в конфиге, а
# разбор уходит с ParseMode.HTML. Это первое, что человек видит от сервиса:
# ошибка Telegram здесь означает, что он не увидит вообще ничего.

TRAP = '<a href="http://zlo.example">нажми</a>'


def test_a_module_line_from_config_does_not_become_markup(db_path, five_weeks, monkeypatch):
    import copy

    from core import config

    patched = copy.deepcopy(config.settings())
    patched["modules"]["finance"]["title"] = TRAP
    patched["modules"]["finance"]["diagnostic_line"] = TRAP
    monkeypatch.setattr(config, "settings", lambda: patched)

    text = handler.report_text(
        diagnostic.run("sid-1", five_weeks, today=TODAY, path=db_path)
    )

    assert "<a href" not in text
    assert "&lt;a href=&quot;" in text
    assert "<b>" in text  # разметка самого бота при этом на месте
