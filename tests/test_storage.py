"""Агент 8, хранение и приёмка по товарам.

Швы те же два, что и у всех: путь к базе (временный файл) и транспорт WB
(`httpx.MockTransport` с записанными ответами, сети нет ни байта). Отчёт здесь
не обычный: Wildberries готовит его заданием, поэтому транспорт отвечает
последовательностью состояний, а не одним ответом. Ровно это и нужно
проверить: `processing` не ошибка, `purged` не пустой ответ, а бесконечное
ожидание кончается честным «не дождались».

Ожидаемые числа посчитаны руками и записаны рядом в комментариях: считать их
тем же способом, что и код, значит не проверить ничего.

Главная проверка этого файла не про сбор, а про деньги: одни и те же рубли
приходят в отчёт двумя путями, и в прибыли их должно остаться ровно столько,
сколько их в большем из двух источников. Ни двойного вычитания (на рекламе
этот сюжет уже стоил владельцу полумиллиона), ни подаренной прибыли.
"""

from __future__ import annotations

import base64
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from agents import finance, profit, storage
from bot.handlers import profit as handlers_profit
from core import costs as costs_module
from core import crypto, db, queue, wbapi, xlsx

TODAY = date(2026, 9, 7)
FIRST = date(2026, 9, 1)

EXP = int(datetime(2027, 1, 1, tzinfo=timezone.utc).timestamp())

# Маска токена с категориями «Аналитика» и «Финансы»: ровно те, на которых
# стоят эти отчёты и отчёт о реализации. Биты из core/wbapi/token.py.
MASK = (1 << 2) | (1 << 13)

CREATE = "/api/v1/paid_storage"
STATUS = "/api/v1/paid_storage/tasks/{task}/status"
DOWNLOAD = "/api/v1/paid_storage/tasks/{task}/download"
ACC_CREATE = "/api/v1/acceptance_report"

# Строка отчёта о платном хранении в том виде, в каком её отдаёт Wildberries.
# Поля и значения взяты с живого кабинета владельца: сумма хранения точнее
# копейки, и строк на одни сутки по одному товару бывает несколько (сутки
# плюс поставка плюс размер).
def storage_row(day: str, nm_id: int, price: str, **extra) -> dict:
    row = {
        "date": day,
        "originalDate": day,
        "nmId": nm_id,
        "vendorCode": f"Арт. {nm_id}",
        "subject": "Карнавальные очки",
        "warehousePrice": float(price),
        "warehouseCoef": 0,
        "volume": 2.945,
        "calcType": "короба: товары свыше базы",
        "barcodesCount": 2,
        "loyaltyDiscount": 0,
        "warehouse": "Склад WB РФ",
    }
    row.update(extra)
    return row


def acceptance_row(day: str, nm_id: int, total: str, count: int = 1) -> dict:
    # Артикул здесь `nmID`, с заглавной D: так его называет Wildberries в этом
    # отчёте, и тест ловит разбор, который помнит только одно написание.
    return {
        "shkCreateDate": day,
        "giCreateDate": day,
        "incomeId": 777,
        "nmID": nm_id,
        "subjectName": "Кружка",
        "count": count,
        "total": float(total),
    }


def make_token(mask: int = MASK) -> str:
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
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(float(seconds))
        self.now += float(seconds)


class Recorder:
    """Записанные ответы WB и журнал запросов.

    Состояния задания отдаются по списку: первый запрос получает первое
    значение, последний повторяется сколько угодно раз. Так и ведёт себя
    Wildberries: `new`, потом `processing`, потом `done`.
    """

    def __init__(self, rows=None, states=("done",), codes=None, download_code=200) -> None:
        self.rows = list(rows or [])
        self.states = list(states)
        self.codes = dict(codes or {})
        self.download_code = download_code
        self.calls: list[tuple[str, dict]] = []
        self.asked_states = 0

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def paths(self) -> list[str]:
        return [path for path, _ in self.calls]

    def params(self, path: str) -> list[dict]:
        return [query for spot, query in self.calls if spot == path]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        query = dict(request.url.params)
        self.calls.append((path, query))
        code = self.codes.get(path)
        if code:
            return httpx.Response(code, json={"title": "нет"})
        if path.endswith("/status"):
            index = min(self.asked_states, len(self.states) - 1)
            self.asked_states += 1
            return httpx.Response(
                200, json={"data": {"id": "task-1", "status": self.states[index]}}
            )
        if path.endswith("/download"):
            if self.download_code == 204:
                return httpx.Response(204)
            return httpx.Response(self.download_code, json=self.rows)
        # Создание задания: номер один на все окна, тесту этого достаточно.
        return httpx.Response(200, json={"data": {"taskId": "task-1"}})


async def fetch(recorder, *, kind=storage.STORAGE, first=FIRST, last=TODAY, polls=None):
    """Один вызов клиента WB с часами под контролем теста."""
    clock = FakeTime()
    client = wbapi.WBClient(
        1, make_token(), recorder.client(), clock=clock.clock, sleep=clock.sleep
    )
    method = getattr(client, storage.FETCH[kind])
    extra = {} if polls is None else {"polls": polls}
    return await method(first, last, **extra)


# --- отчёт с заданием: три шага и ожидание ----------------------------------


@pytest.mark.asyncio
async def test_report_waits_through_processing_and_then_downloads():
    """`processing` это не ошибка, а «ещё готовится».

    Ровно этого в проекте раньше не было вовсе: все методы WB отвечали сразу.
    Ожидание обязано пережить несколько «ещё не готово» и забрать отчёт, когда
    он станет готов.
    """
    recorder = Recorder(
        rows=[storage_row("2026-09-02", 111, "1.5")],
        states=("new", "processing", "processing", "done"),
    )

    rows = await fetch(recorder)

    assert len(rows) == 1
    assert recorder.asked_states == 4
    # Три шага, и каждый ровно один раз, кроме готовности.
    assert recorder.paths().count(CREATE) == 1
    assert sum(1 for path in recorder.paths() if path.endswith("/download")) == 1
    # Номер задания подставлен в путь, а не приклеен к параметрам.
    assert STATUS.format(task="task-1") in recorder.paths()


@pytest.mark.asyncio
async def test_purged_report_is_an_error_not_an_empty_answer():
    """Отчёт удалён у WB: это не «данных нет», и молчать об этом нельзя.

    Пустой список здесь означал бы «за эти сутки расхода не было», и отчёт
    получил бы колонку с нулями вместо честного «не собрали».
    """
    recorder = Recorder(states=("processing", "purged"))

    with pytest.raises(wbapi.WBApiError) as failed:
        await fetch(recorder)

    assert "purged" in str(failed.value)
    # За отчётом, которого нет, не ходят.
    assert not [path for path in recorder.paths() if path.endswith("/download")]


@pytest.mark.asyncio
async def test_canceled_report_is_an_error_too():
    """Задание отклонено: ждать нечего, заказывать надо заново."""
    recorder = Recorder(states=("canceled",))

    with pytest.raises(wbapi.WBApiError) as failed:
        await fetch(recorder)

    assert "canceled" in str(failed.value)


@pytest.mark.asyncio
async def test_waiting_has_an_end_and_it_is_called_unavailability():
    """Вечного ожидания не бывает: не дождались - это недоступность WB.

    Именно недоступность, а не ошибка данных: повтор такой задачи имеет смысл,
    и делает его очередь.
    """
    recorder = Recorder(states=("processing",))

    with pytest.raises(wbapi.WBUnavailable):
        await fetch(recorder, polls=3)

    assert recorder.asked_states == 3


@pytest.mark.asyncio
async def test_no_data_is_an_empty_answer_not_a_failure():
    """Код 204 у Wildberries означает «нет данных», а не сбой."""
    recorder = Recorder(rows=[], download_code=204)

    assert await fetch(recorder) == []


@pytest.mark.asyncio
async def test_long_period_is_cut_into_windows_wb_accepts():
    """Окно задания восемь суток, и режет период клиент, а не агент.

    Девятнадцать суток это три окна: 1-8, 9-16, 17-19. Проверяются сами
    границы, а не их количество: ошибка на сутки в стыке окон оставила бы в
    истории дыру, которую потом нечем заметить.
    """
    recorder = Recorder(rows=[])

    await fetch(recorder, first=date(2026, 9, 1), last=date(2026, 9, 19))

    windows = [
        (query["dateFrom"], query["dateTo"]) for query in recorder.params(CREATE)
    ]
    assert windows == [
        ("2026-09-01", "2026-09-08"),
        ("2026-09-09", "2026-09-16"),
        ("2026-09-17", "2026-09-19"),
    ]


@pytest.mark.asyncio
async def test_acceptance_asks_for_a_month_at_once():
    """У приёмки окно 31 день: просить её восьмёрками значило бы ждать вчетверо."""
    recorder = Recorder(rows=[])

    await fetch(recorder, kind=storage.ACCEPTANCE, first=date(2026, 9, 1), last=date(2026, 9, 30))

    assert [
        (query["dateFrom"], query["dateTo"]) for query in recorder.params(ACC_CREATE)
    ] == [("2026-09-01", "2026-09-30")]


@pytest.mark.asyncio
async def test_missing_task_number_is_an_error():
    """Нет номера задания - дальше идти некуда, и это говорится сразу."""

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {}})

    clock = FakeTime()
    client = wbapi.WBClient(
        1, make_token(), httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        clock=clock.clock, sleep=clock.sleep,
    )

    with pytest.raises(wbapi.WBApiError):
        await client.paid_storage(FIRST, TODAY)


def test_task_number_does_not_pollute_the_call_log():
    """Номер задания в журнал вызовов не попадает.

    Иначе каждый вызов попал бы в `api_calls` под своим именем, и сложить
    расход по методу стало бы нечем.
    """
    spot = wbapi.ENDPOINTS["paid_storage_status"].at(task_id="219eaecf")

    assert "219eaecf" in spot.url
    assert "219eaecf" not in spot.label
    assert spot.label == wbapi.ENDPOINTS["paid_storage_status"].label


def test_every_report_step_has_its_own_lane():
    """У трёх шагов дорожки разные: лимиты WB считает по методам.

    Общая корзина заставила бы опрос готовности ждать минуту, хотя ему
    разрешено раз в пять секунд, и готовый отчёт (он живёт два часа) успел бы
    устареть.
    """
    from core.wbapi import limits

    lanes = {
        name: wbapi.ENDPOINTS[name].lane
        for name in (
            "paid_storage_create",
            "paid_storage_status",
            "paid_storage_download",
            "acceptance_create",
            "acceptance_status",
            "acceptance_download",
        )
    }
    assert len(set(lanes.values())) == 6
    for lane in lanes.values():
        assert lane in limits.LANES, lane
    # Готовность спрашивается раз в пять секунд, задание и скачивание раз в
    # минуту: числа из документации Wildberries.
    assert limits.LANES[lanes["paid_storage_status"]].period == 5.0
    assert limits.LANES[lanes["paid_storage_create"]].period == 60.0
    assert limits.LANES[lanes["paid_storage_download"]].period == 60.0


# --- разбор и запись ---------------------------------------------------------


def test_rows_are_folded_in_rubles_and_rounded_once():
    """Копейки складываются в рублях, а округляются один раз.

    На живом кабинете сумма хранения строки это 0,89528 ₽, а строк за сутки по
    одному товару бывает десятки: сутки плюс поставка плюс размер. Округли
    каждую строку порознь, и на восьми сутках набежит рубля два, а на годе
    под сотню.

    Руками: четыре строки по 0,89528 это 3,58112, то есть 3,58 ₽ и 358
    копеек. По копейке на строку вышло бы 4 раза по 0,90, то есть 3,60.
    """
    rows = [storage_row("2026-09-02", 111, "0.89528") for _ in range(4)]

    folded = storage.storage_rows(rows)

    assert folded[("2026-09-02", 111)]["amount_kop"] == 358
    assert folded[("2026-09-02", 111)]["rows"] == 4


def test_acceptance_reads_its_own_field_names():
    """В приёмке артикул это nmID, а сумма total: имена другие, чем в хранении."""
    folded = storage.acceptance_rows([acceptance_row("2026-09-03", 222, "12.34", count=3)])

    assert folded[("2026-09-03", 222)]["amount_kop"] == 1234
    assert folded[("2026-09-03", 222)]["units"] == 3


def test_rows_without_an_article_stay_in_the_day_total(db_path):
    """Строка без артикула в разрез по товарам не идёт, а деньги не теряются.

    Поставить её на случайный товар нельзя, а выдумать артикул тем более. Но
    суточный итог считается по всем строкам, и разница между ним и суммой по
    товарам видна в отчёте.
    """
    client_id = db.admin_repo(db_path).ensure_client(1111)
    rows = [
        storage_row("2026-09-02", 111, "10"),
        storage_row("2026-09-02", 0, "5"),
    ]

    storage.save(client_id, storage.STORAGE, date(2026, 9, 2), date(2026, 9, 2), rows, path=db_path)
    spend = storage.spend_of(
        client_id, storage.STORAGE, date(2026, 9, 2), date(2026, 9, 2), path=db_path
    )

    assert spend.total == Decimal("10")
    assert spend.collected == Decimal("15")


def test_a_day_without_expense_is_not_a_day_without_data(db_path):
    """Собранные сутки отмечаются даже тогда, когда расхода в них не было.

    Иначе «хранения не было» и «эти сутки мы не собирали» в базе выглядят
    одинаково, а разница тут главная: на ней стоит честное сравнение с
    отчётом о реализации.
    """
    client_id = db.admin_repo(db_path).ensure_client(2222)

    storage.save(
        client_id, storage.STORAGE, date(2026, 9, 1), date(2026, 9, 3), [], path=db_path
    )

    assert storage.covered_days(
        client_id, storage.STORAGE, date(2026, 9, 1), date(2026, 9, 3), path=db_path
    ) == {"2026-09-01", "2026-09-02", "2026-09-03"}
    assert storage.missing_days(
        client_id, storage.STORAGE, date(2026, 9, 1), date(2026, 9, 5), path=db_path
    ) == [date(2026, 9, 4), date(2026, 9, 5)]
    spend = storage.spend_of(
        client_id, storage.STORAGE, date(2026, 9, 1), date(2026, 9, 3), path=db_path
    )
    assert spend.available is True
    assert spend.total == Decimal("0")
    assert spend.complete is True


def test_unseen_days_are_not_available_at_all(db_path):
    """Несобранный период это «нет данных», а не ноль."""
    client_id = db.admin_repo(db_path).ensure_client(3333)

    spend = storage.spend_of(client_id, storage.STORAGE, FIRST, TODAY, path=db_path)

    assert spend.available is False
    assert spend.reason == storage.NOT_COLLECTED
    assert spend.of(111) is None
    assert spend.days == 0 and spend.days_total == 7


def test_window_is_rewritten_not_appended(db_path):
    """Перерасчёт переписывает сутки, а не складывается с прежней суммой.

    У Wildberries бывают перерасчёты задним числом, и ответ за окно это вся
    правда об этом окне. Дополнение сложило бы старое с новым и удвоило
    расход на ровном месте.
    """
    client_id = db.admin_repo(db_path).ensure_client(4444)
    window = (date(2026, 9, 2), date(2026, 9, 2))

    storage.save(client_id, storage.STORAGE, *window, [storage_row("2026-09-02", 111, "10")], path=db_path)
    storage.save(client_id, storage.STORAGE, *window, [storage_row("2026-09-02", 111, "7")], path=db_path)

    spend = storage.spend_of(client_id, storage.STORAGE, *window, path=db_path)
    assert spend.total == Decimal("7")
    assert db.repo(client_id, db_path).count("expense_daily") == 1


def test_days_outside_the_window_are_not_written(db_path):
    """Сутки вне запрошенного окна не пишутся.

    Окно, которое их переписало бы, никто не запрашивал: такая строка осталась
    бы в базе навсегда и однажды показалась бы в отчёте вместе со свежей.
    """
    client_id = db.admin_repo(db_path).ensure_client(5555)

    storage.save(
        client_id,
        storage.STORAGE,
        date(2026, 9, 2),
        date(2026, 9, 2),
        [storage_row("2026-09-02", 111, "10"), storage_row("2026-08-01", 111, "99")],
        path=db_path,
    )

    rows = db.repo(client_id, db_path).rows("expense_daily")
    assert [row["date"] for row in rows] == ["2026-09-02"]


def test_sources_live_in_one_table_and_do_not_mix(db_path):
    """Хранение и приёмка лежат в одной таблице и друг другу не мешают."""
    client_id = db.admin_repo(db_path).ensure_client(6666)
    window = (date(2026, 9, 2), date(2026, 9, 2))

    storage.save(client_id, storage.STORAGE, *window, [storage_row("2026-09-02", 111, "10")], path=db_path)
    storage.save(
        client_id, storage.ACCEPTANCE, *window, [acceptance_row("2026-09-02", 111, "3")], path=db_path
    )

    assert storage.spend_of(client_id, storage.STORAGE, *window, path=db_path).total == Decimal("10")
    assert storage.spend_of(client_id, storage.ACCEPTANCE, *window, path=db_path).total == Decimal("3")


# --- сбор через очередь ------------------------------------------------------


@pytest.fixture
def cabinet(db_path, monkeypatch):
    """Подключённый кабинет: токен с категорией «Аналитика»."""
    monkeypatch.setenv("ENCRYPTION_KEY", crypto.generate_key())
    wbapi.reset_limits()
    queue.reset()
    client_id = db.admin_repo(db_path).ensure_client(7070)
    db.repo(client_id, db_path).insert(
        "wb_tokens", ciphertext=crypto.encrypt(make_token()), exp=str(EXP), scopes="analytics"
    )
    return client_id


async def collect_window(client_id, db_path, recorder, *, kind=storage.STORAGE):
    clock = FakeTime()
    return await storage.collect(
        client_id,
        kind,
        FIRST,
        FIRST + timedelta(days=2),
        path=db_path,
        http=recorder.client(),
        clock=clock.clock,
        sleep=clock.sleep,
    )


@pytest.mark.asyncio
async def test_collect_puts_the_window_into_the_base(cabinet, db_path):
    """Обычный сбор: окно приехало и легло в базу."""
    recorder = Recorder(
        rows=[storage_row("2026-09-01", 111, "10"), storage_row("2026-09-02", 222, "4")],
        states=("processing", "done"),
    )

    done = await collect_window(cabinet, db_path, recorder)

    assert done.ok is True
    assert done.articles == 2
    assert done.amount == Decimal("14")
    spend = storage.spend_of(
        cabinet, storage.STORAGE, FIRST, FIRST + timedelta(days=2), path=db_path
    )
    assert spend.spend == {111: Decimal("10"), 222: Decimal("4")}


@pytest.mark.asyncio
async def test_wb_silence_leaves_the_base_untouched(cabinet, db_path):
    """Wildberries не ответил: это «не знаем», и в базе ничего не меняется.

    Пустое окно, записанное после отказа, означало бы «за эти сутки расхода
    не было»: ровно та ложь, от которой таблица собранных суток и спасает.
    """
    recorder = Recorder(codes={CREATE: 500})

    done = await collect_window(cabinet, db_path, recorder)

    assert done.reason == storage.UNAVAILABLE
    assert db.repo(cabinet, db_path).count("expense_days") == 0


@pytest.mark.asyncio
async def test_without_analytics_category_it_is_a_state_not_a_failure(cabinet, db_path):
    """Нет категории «Аналитика» - колонки не будет, и это нормально."""
    recorder = Recorder(codes={CREATE: 403})

    done = await collect_window(cabinet, db_path, recorder)

    assert done.reason == storage.NO_CATEGORY
    assert db.repo(cabinet, db_path).count("expense_days") == 0


@pytest.mark.asyncio
async def test_nightly_work_takes_the_fresh_window_and_digs_into_history(
    cabinet, db_path, monkeypatch
):
    """Ночная работа: свежее окно, а потом немного истории вглубь.

    Свежее окно обязано быть первым: история терпит, а вчерашние деньги нужны
    отчёту сегодня. Глубина ограничена настройкой владельца, иначе один
    кабинет занял бы очередь на час.
    """
    # Самая ранняя неделя отчёта о реализации это дно: глубже хранение
    # сравнивать не с чем.
    finance.save_rows(cabinet, FIN_ROWS, path=db_path)
    for week in finance.aggregate(FIN_ROWS).values():
        finance.save_week(cabinet, week, path=db_path)
    monkeypatch.setattr(storage, "backfill_windows", lambda: 2)
    monkeypatch.setattr(storage, "collect_days", lambda: 8)
    seen: list[tuple[str, date, date]] = []

    async def fake_collect(client_id, kind, first, last, **kw):
        seen.append((kind, first, last))
        return storage.Collected(kind=kind)

    monkeypatch.setattr(storage, "collect", fake_collect)
    task = SimpleNamespace(client_id=cabinet, payload={"date": "2026-09-30"})

    await storage.collect_client(task, path=db_path)

    windows = [item for item in seen if item[0] == storage.STORAGE]
    # Свежее окно это последние восемь суток, 23-30 сентября.
    assert windows[0] == (storage.STORAGE, date(2026, 9, 23), date(2026, 9, 30))
    # Дальше два окна истории, от свежих к старым: 15-22 и 7-14 сентября.
    assert windows[1] == (storage.STORAGE, date(2026, 9, 15), date(2026, 9, 22))
    assert windows[2] == (storage.STORAGE, date(2026, 9, 7), date(2026, 9, 14))
    assert len(windows) == 3
    # Приёмка идёт тем же заходом, и окно у неё своё, 31 день.
    assert (storage.ACCEPTANCE, date(2026, 9, 23), date(2026, 9, 30)) in seen


@pytest.mark.asyncio
async def test_without_financial_weeks_history_is_not_dug_at_all(
    cabinet, db_path, monkeypatch
):
    """Денег за кабинет ещё не выгружали - истории хранения не нужно.

    Иначе свежеподключённый кабинет полтора месяца жёг бы свой лимит на сутки,
    которые сравнивать не с чем: прибыль считается по неделям отчёта о
    реализации, а их нет ни одной.
    """
    monkeypatch.setattr(storage, "backfill_windows", lambda: 2)
    seen: list[tuple[str, date, date]] = []

    async def fake_collect(client_id, kind, first, last, **kw):
        seen.append((kind, first, last))
        return storage.Collected(kind=kind)

    monkeypatch.setattr(storage, "collect", fake_collect)

    await storage.collect_client(
        SimpleNamespace(client_id=cabinet, payload={"date": "2026-09-30"}), path=db_path
    )

    # Только свежие окна двух источников, и ни одного окна истории.
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_named_window_is_collected_as_asked(cabinet, db_path, monkeypatch):
    """Названное окно собирается целиком и режется по пределу Wildberries."""
    seen: list[tuple[str, date, date]] = []

    async def fake_collect(client_id, kind, first, last, **kw):
        seen.append((kind, first, last))
        return storage.Collected(kind=kind)

    monkeypatch.setattr(storage, "collect", fake_collect)
    task = SimpleNamespace(
        client_id=cabinet,
        payload={"kind": storage.STORAGE, "from": "2026-09-01", "to": "2026-09-12"},
    )

    await storage.collect_client(task, path=db_path)

    assert seen == [
        (storage.STORAGE, date(2026, 9, 1), date(2026, 9, 8)),
        (storage.STORAGE, date(2026, 9, 9), date(2026, 9, 12)),
    ]


def test_cleanup_removes_history_deeper_than_the_term(db_path, monkeypatch):
    """Чистка срезает историю глубже срока: иначе таблицы растут бесконечно."""
    client_id = db.admin_repo(db_path).ensure_client(8080)
    monkeypatch.setattr(storage, "history_days", lambda: 10)
    storage.save(
        client_id, storage.STORAGE, date(2026, 9, 1), date(2026, 9, 1),
        [storage_row("2026-09-01", 111, "10")], path=db_path,
    )
    storage.save(
        client_id, storage.STORAGE, date(2026, 9, 25), date(2026, 9, 25),
        [storage_row("2026-09-25", 111, "10")], path=db_path,
    )

    removed = storage.cleanup(SimpleNamespace(payload={"date": "2026-09-30"}), path=db_path)

    assert removed == 2  # одна строка товара и одни отмеченные сутки
    assert [row["date"] for row in db.repo(client_id, db_path).rows("expense_daily")] == [
        "2026-09-25"
    ]


def test_disconnect_takes_the_history_with_it(db_path):
    """Удаление клиента убирает и эти таблицы: каскад, а не дисциплина."""
    client_id = db.admin_repo(db_path).ensure_client(9090)
    storage.save(
        client_id, storage.STORAGE, date(2026, 9, 1), date(2026, 9, 1),
        [storage_row("2026-09-01", 111, "10")], path=db_path,
    )
    assert db.repo(client_id, db_path).count("expense_daily") == 1

    db.admin_repo(db_path).delete_client(client_id)

    assert db.repo(client_id, db_path).count("expense_daily") == 0
    assert db.repo(client_id, db_path).count("expense_days") == 0


def test_tables_belong_to_the_client():
    """Таблицы названы клиентскими: иначе репозиторий клиента их не отдаст."""
    assert "expense_daily" in db.CLIENT_TABLES
    assert "expense_days" in db.CLIENT_TABLES


# --- деньги в отчёте о прибыли ----------------------------------------------

# Неделя отчёта о реализации: две продажи и две служебные строки без артикула.
# Хранение и приёмка приходят именно так, без nmId: это проверено на живом
# кабинете владельца, и ровно поэтому колонка по товарам иначе была бы нашей
# пропорцией от выручки.
FIN_ROWS = [
    {
        "reportId": 1,
        "rrdId": 1,
        "dateFrom": "2026-09-01",
        "dateTo": "2026-09-07",
        "nmId": 111,
        "vendorCode": "A-1",
        "subjectName": "Кружка",
        "title": "Кружка с котом",
        "docTypeName": "Продажа",
        "quantity": 2,
        "retailAmount": 3000,
        "retailPriceWithDisc": 1500,
        "forPay": 2400,
        "vw": 300,
    },
    {
        "reportId": 1,
        "rrdId": 2,
        "dateFrom": "2026-09-01",
        "dateTo": "2026-09-07",
        "nmId": 222,
        "vendorCode": "B-2",
        "subjectName": "Ложка",
        "title": "Ложка деревянная",
        "docTypeName": "Продажа",
        "quantity": 1,
        "retailAmount": 1000,
        "retailPriceWithDisc": 1000,
        "forPay": 800,
        "vw": 100,
    },
    {
        "reportId": 1,
        "rrdId": 3,
        "dateFrom": "2026-09-01",
        "dateTo": "2026-09-07",
        "docTypeName": "",
        "sellerOperName": "Хранение",
        "paidStorage": 200,
    },
    {
        "reportId": 1,
        "rrdId": 4,
        "dateFrom": "2026-09-01",
        "dateTo": "2026-09-07",
        "docTypeName": "",
        "sellerOperName": "Операции на приёмке",
        "paidAcceptance": 50,
    },
]

# Второй источник. Хранение по товарам меньше удержанного (140 + 30 = 170 при
# 200 в отчёте о реализации), приёмка больше (60 при 50): две разные развилки
# правила в одном отчёте.
STORAGE_ROWS = [
    storage_row("2026-09-02", 111, "120"),
    storage_row("2026-09-02", 222, "30"),
    storage_row("2026-09-05", 111, "20"),
]
ACCEPTANCE_ROWS = [acceptance_row("2026-09-03", 111, "60")]


def seed(db_path, *, telegram_id=4040, with_reports=True):
    """Кабинет с неделей агента 1, себестоимостью и вторым источником."""
    client_id = db.admin_repo(db_path).ensure_client(telegram_id)
    finance.save_rows(client_id, FIN_ROWS, path=db_path)
    for week in finance.aggregate(FIN_ROWS).values():
        finance.save_week(client_id, week, path=db_path)
    costs_module.save_costs(
        client_id, {111: Decimal("400"), 222: Decimal("500")}, path=db_path
    )
    if with_reports:
        # Окно сбора берётся то же, что у отчёта: иначе сутки периода были бы
        # собраны не все, и отчёт честно оговорил бы неполноту, а проверять
        # сейчас надо не её.
        first, last = finance.period_of("week", TODAY).bounds
        storage.save(client_id, storage.STORAGE, first, last, STORAGE_ROWS, path=db_path)
        storage.save(
            client_id, storage.ACCEPTANCE, first, last, ACCEPTANCE_ROWS, path=db_path
        )
    return client_id


def report_of(client_id, db_path):
    return profit.build(
        client_id, "week", today=TODAY, ads=profit.AdSpend({}), path=db_path
    )


def article(report, nm_id):
    for item in report.articles:
        if item.nm_id == nm_id:
            return item
    raise AssertionError(f"артикула {nm_id} нет в отчёте")


def source_of(report, kind):
    found = report.report_of(kind)
    assert found is not None
    return found


def test_storage_by_article_comes_from_its_own_report(db_path):
    """Колонка хранения по товарам это цифры отчёта WB, а не наша пропорция.

    Руками: 120 + 20 у артикула 111 это 140, у 222 это 30. Пропорция от
    выручки дала бы 150 и 50: именно от неё и уходим, потому что хранение
    зависит от объёма и времени на складе, а не от выручки.
    """
    client_id = seed(db_path)

    report = report_of(client_id, db_path)

    assert article(report, 111).storage_report == Decimal("140")
    assert article(report, 222).storage_report == Decimal("30")
    assert report.report_total(storage.STORAGE) == Decimal("170")
    assert source_of(report, storage.STORAGE).billed == Decimal("200")


def test_storage_is_not_subtracted_twice(db_path):
    """Одни рубли вычитаются один раз, и это та же проверка, что у рекламы.

    Хранение приходит в отчёт двумя путями: статьёй отчёта о реализации (200 ₽
    без артикула) и отчётом о платном хранении по товарам (140 + 30 = 170 ₽).
    В расходах должно остаться 200: большее из двух. Двойное вычитание дало бы
    370, выброшенная статья - 170.

    Приёмка здесь же показывает вторую развилку: по товарам 60 ₽ при 50 ₽
    удержанных, и в расходах остаётся 60.

    Числа руками. Обезличка: остаток хранения 200 - 170 = 30, приёмки
    50 - 50 = 0, итого 30. Доля 111: 30 * 3000 / 4000 = 22,50; доля 222:
    30 * 1000 / 4000 = 7,50. Расходы, вошедшие в прибыль: комиссия 300 + 100,
    хранение 140 + 30, приёмка 60 + 0, обезличка 22,50 + 7,50, то есть 660.
    """
    client_id = seed(db_path)

    report = report_of(client_id, db_path)

    storage_source = source_of(report, storage.STORAGE)
    acceptance = source_of(report, storage.ACCEPTANCE)
    assert storage_source.replaced == Decimal("170")
    assert storage_source.uncovered == Decimal("30")
    assert acceptance.replaced == Decimal("50")
    assert acceptance.uncovered == Decimal("0")
    assert report.unallocated == Decimal("30")

    spent = sum(
        (
            item.wb_costs
            + (item.ad_spend or Decimal("0"))
            + (item.storage_report or Decimal("0"))
            + (item.acceptance_report or Decimal("0"))
            + item.unallocated
            for item in report.priced
        ),
        Decimal("0"),
    )
    # 400 комиссии + 200 хранения (большее из двух) + 60 приёмки (тоже
    # большее). Ни 370 двойного хранения, ни 170 вместо настоящих 200.
    assert spent == Decimal("660")
    # Статья отчёта о реализации в расходах строки больше не стоит: её деньги
    # разобраны на заменённую часть и остаток.
    assert article(report, 111).storage_counted is False
    assert article(report, 111).wb_costs == Decimal("300")


def test_profit_counts_the_bigger_of_two_sources(db_path):
    """Прибыль по товарам: вычтено ровно по одному разу.

    Руками: 111 это 3000 - 800 себестоимости - 300 комиссии - 140 хранения -
    60 приёмки - 22,50 обезлички = 1677,50. 222 это 1000 - 500 - 100 - 30 -
    0 - 7,50 = 362,50.
    """
    client_id = seed(db_path)

    report = report_of(client_id, db_path)

    assert article(report, 111).profit == Decimal("1677.50")
    assert article(report, 222).profit == Decimal("362.50")


def test_without_the_second_report_everything_counts_as_before(db_path):
    """Нет второго отчёта - считается ровно как раньше, пропорцией.

    Руками: обезличка это вся статья хранения и приёмки, 250. Доля 111:
    250 * 3000 / 4000 = 187,50, доля 222: 62,50. Прибыль 111: 3000 - 800 -
    300 - 187,50 = 1712,50; 222: 1000 - 500 - 100 - 62,50 = 337,50.

    Разница с прошлым тестом ровно в приёмке: по отчёту о приёмке её 60, а
    удержано 50, и прибыль кабинета меньше на эти 10 ₽. Это не ошибка, а
    второй источник, который больше первого.
    """
    client_id = seed(db_path, telegram_id=4141, with_reports=False)

    report = report_of(client_id, db_path)

    assert report.report_sources == ()
    assert article(report, 111).storage_report is None
    assert article(report, 111).storage_counted is True
    assert report.unallocated == Decimal("250")
    assert article(report, 111).profit == Decimal("1712.50")
    assert article(report, 222).profit == Decimal("337.50")
    assert report.total_profit - Decimal("10") == report_of(
        seed(db_path, telegram_id=4242), db_path
    ).total_profit


def test_the_gap_between_two_numbers_is_shown_not_hidden(db_path):
    """Разница двух источников называется числом, и в сообщении, и в книге.

    Wildberries нигде не обещал, что суммы сойдутся, и причины расходиться
    назвал сам. Молчание о разнице стоило бы доверия обоим числам: селлер
    сложит колонку, сверит с /finance и не получит того же.
    """
    client_id = seed(db_path)

    report = report_of(client_id, db_path)
    source = source_of(report, storage.STORAGE)
    assert source.gap == Decimal("-30")
    assert source.matches is False

    text = handlers_profit.summary_text(report)
    assert "отчёт WB о платном хранении" in text
    assert "170" in text and "200" in text
    assert "Разница" in text

    problems = xlsx.read_book(profit.excel_bytes(report))[profit.PROBLEMS_SHEET]
    blocks = [row.get("Блок") for row in problems.rows]
    assert storage.BLOCK_TITLES[storage.STORAGE] in blocks
    note = next(
        row.get("Что это значит")
        for row in problems.rows
        if row.get("Блок") == storage.BLOCK_TITLES[storage.STORAGE]
    )
    assert "отчёт WB о платном хранении" in note
    assert "200.00" in note.replace(",", ".")


def test_the_book_shows_one_storage_column_and_names_its_source(db_path):
    """В книге одна колонка хранения, и она названа своим источником.

    Две колонки под одно хранение читались бы как два разных расхода, а
    безымянная колонка обещала бы то же число, что в /finance.
    """
    client_id = seed(db_path)

    report = report_of(client_id, db_path)
    headers = profit.article_headers(report)

    assert storage.COLUMN_TITLES[storage.STORAGE] in headers
    assert storage.COLUMN_TITLES[storage.ACCEPTANCE] in headers
    assert "Хранение, ₽" not in headers
    assert "Приёмка, ₽" not in headers

    sheet = xlsx.read_book(profit.excel_bytes(report))[profit.ARTICLES_SHEET]
    row = next(item for item in sheet.rows if str(item.get("Артикул WB")) == "111")
    assert Decimal(str(row.get(storage.COLUMN_TITLES[storage.STORAGE]))) == Decimal("140")
    totals = next(
        item for item in sheet.rows if str(item.get("Артикул WB")) == profit.TOTAL_LABEL
    )
    assert Decimal(str(totals.get(storage.COLUMN_TITLES[storage.STORAGE]))) == Decimal("170")


def test_a_source_with_no_money_at_all_keeps_quiet(db_path):
    """Ноль в обоих источниках это не повод для абзаца в сводке.

    У кабинета владельца приёмка нулевая и в отчёте о реализации, и в своём
    отчёте. Колонка в книге остаётся (настоящий ноль это ответ, а не пробел),
    а разговор о разнице двух нулей не нужен никому.
    """
    client_id = db.admin_repo(db_path).ensure_client(4848)
    rows = [row for row in FIN_ROWS if "paidAcceptance" not in row]
    finance.save_rows(client_id, rows, path=db_path)
    for week in finance.aggregate(rows).values():
        finance.save_week(client_id, week, path=db_path)
    costs_module.save_costs(client_id, {111: Decimal("400")}, path=db_path)
    first, last = finance.period_of("week", TODAY).bounds
    storage.save(client_id, storage.STORAGE, first, last, STORAGE_ROWS, path=db_path)
    storage.save(client_id, storage.ACCEPTANCE, first, last, [], path=db_path)

    report = report_of(client_id, db_path)
    text = handlers_profit.summary_text(report)

    assert source_of(report, storage.ACCEPTANCE).notable is False
    assert "Приёмка по товарам" not in text
    assert "Хранение по товарам" in text
    # Колонка на месте: приёмки правда не было, и это ответ.
    assert storage.COLUMN_TITLES[storage.ACCEPTANCE] in profit.article_headers(report)
    problems = xlsx.read_book(profit.excel_bytes(report))[profit.PROBLEMS_SHEET]
    assert storage.BLOCK_TITLES[storage.ACCEPTANCE] not in [
        row.get("Блок") for row in problems.rows
    ]


def test_incomplete_history_is_named_by_number(db_path):
    """Собрано не всё - сказано, сколько суток из скольких.

    Столбец за несобранные сутки занижен, и промолчать об этом значило бы
    выдать неполную цифру за полную.
    """
    client_id = db.admin_repo(db_path).ensure_client(4343)
    finance.save_rows(client_id, FIN_ROWS, path=db_path)
    for week in finance.aggregate(FIN_ROWS).values():
        finance.save_week(client_id, week, path=db_path)
    costs_module.save_costs(client_id, {111: Decimal("400")}, path=db_path)
    # Собраны только двое суток периода из семи.
    storage.save(
        client_id, storage.STORAGE, date(2026, 9, 2), date(2026, 9, 3), STORAGE_ROWS[:2],
        path=db_path,
    )

    report = report_of(client_id, db_path)
    source = source_of(report, storage.STORAGE)

    # Относительная кнопка «последние 7 дней» считается целыми неделями, и
    # период у неё восемь суток: с 31 августа по 7 сентября.
    assert source.days == 2 and source.days_total == 8
    assert source.complete is False
    assert report.partial_sources == (source,)
    assert "собрано 2 из 8" in handlers_profit.summary_text(report)


def test_tax_base_counts_the_expense_once(db_path, monkeypatch):
    """В базе налога те же деньги и столько же раз.

    Дважды принятый расход занижает базу и обещает селлеру налог меньше
    настоящего, а это хуже, чем завысить.

    Руками: доходы 4000, расходы это удержания Wildberries без штрафов
    (комиссия 400, хранение 200, приёмка 50 = 650) минус заменённые части
    (170 хранения и 50 приёмки) плюс колонки второго отчёта (170 и 60) плюс
    себестоимость 1300, то есть 650 - 220 + 230 + 1300 = 1960.
    """
    from core import tax as tax_module

    client_id = seed(db_path, telegram_id=4444)
    tax_module.set_rule(client_id, tax_module.USN_INCOME_MINUS, Decimal("15"), path=db_path)

    report = report_of(client_id, db_path)
    estimate = report.tax

    assert estimate.expenses == Decimal("1960")
    # Без второго отчёта в базу вошли бы те же 650 удержаний и 1300
    # себестоимости: 1950. Разница ровно в приёмке, которой по её отчёту
    # больше на 10 ₽.
    assert estimate.base == Decimal("2040")


def test_finance_report_does_not_change_by_a_kopeck(db_path):
    """Финансовая раскладка второго источника не видит вовсе.

    Отчёт о реализации это документ взаиморасчётов, и сверка с итогами
    Wildberries стоит на нём. Подмена числа в нём сломала бы сверку, которая
    сейчас работает, поэтому /finance остаётся с `paidStorage` отчёта.
    """
    with_reports = seed(db_path, telegram_id=4545)
    without = seed(db_path, telegram_id=4646, with_reports=False)

    first = finance.build(with_reports, "week", today=TODAY, path=db_path)
    second = finance.build(without, "week", today=TODAY, path=db_path)

    assert first.totals == second.totals
    # И это ровно то, что удержал Wildberries, а не то, что насчитал второй
    # отчёт: 200 хранения и 50 приёмки.
    assert first.totals.storage == Decimal("200")
    assert first.totals.acceptance == Decimal("50")
    assert [check.ours for week in first.weeks for check in week.checks] == [
        check.ours for week in second.weeks for check in week.checks
    ]


def test_profit_report_asks_to_collect_the_missing_days(db_path, monkeypatch):
    """Дыру в истории закрывает отдельная работа, а не сам отчёт.

    Собрать год задним числом это 46 заданий по дорожке «1 запрос в минуту»:
    селлер, попросивший в это время /finance, ждал бы час. Поэтому отчёт
    уходит с тем, что есть, а дыра начинает закрываться сама.
    """
    client_id = seed(db_path, telegram_id=4747, with_reports=False)
    queue.reset()
    monkeypatch.setattr(storage, "backfill_windows", lambda: 1)
    window = finance.period_of("week", TODAY)

    asked = profit._request_missing(client_id, window, path=db_path)

    assert len(asked) == 2  # хранение и приёмка
    tasks = db.admin_repo(db_path).tasks()
    kinds = {row["kind"] for row in tasks}
    assert kinds == {storage.COLLECT_ONE}
    payloads = sorted(json.loads(row["payload"])["kind"] for row in tasks)
    assert payloads == [storage.ACCEPTANCE, storage.STORAGE]
    # Второй такой же отчёт не ставит задачу заново: очередь не двоит.
    profit._request_missing(client_id, window, path=db_path)
    assert len(db.admin_repo(db_path).tasks()) == 2
