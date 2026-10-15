"""Обрыв связи с Telegram это пауза, а не конец работы.

Сети здесь нет, как и во всём прогоне. Обрыв подставляется транспортом: ниже
стоит `Breaking`, у которого каждый запрос рвётся, и `Flaky`, который рвётся
заданное число раз и потом начинает отвечать. Присмотр (`bot.netwatch`) при
этом настоящий, подменять в нём нечего.

Главный тест здесь не про сам присмотр, а про бота целиком: поднимается
настоящее приложение python-telegram-bot с настоящим циклом опроса, транспорт
опроса рвётся, и проверяется, что бот после нескольких неудач жив, продолжает
пробовать и отвечает на сообщение, когда связь вернулась. Ровно это и
перестало работать у владельца.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from telegram.error import NetworkError, TimedOut
from telegram.ext import ApplicationBuilder, MessageHandler, filters
from telegram.request import BaseRequest, HTTPXRequest

from bot import netwatch
from core import audit, config, db, queue

MOMENT = datetime(2026, 10, 16, 0, 56, 22, tzinfo=timezone.utc)

# Длинные тире в проекте запрещены, и сторож ищет их во всех файлах, этот
# не исключение. Поэтому сами знаки записаны кодами, а не как есть.
LONG_DASHES = (chr(0x2014), chr(0x2013), chr(0x2015))

# Ответы Telegram, которых хватает, чтобы приложение поднялось и ответило.
ME = b'{"ok":true,"result":{"id":1,"is_bot":true,"first_name":"bot","username":"bot"}}'
OK = b'{"ok":true,"result":true}'
SENT = b'{"ok":true,"result":{"message_id":2,"date":0,"chat":{"id":5,"type":"private"}}}'
NO_UPDATES = b'{"ok":true,"result":[]}'
ONE_UPDATE = (
    b'{"ok":true,"result":[{"update_id":1,"message":{"message_id":1,"date":0,'
    b'"chat":{"id":5,"type":"private"},"from":{"id":5,"is_bot":false,'
    b'"first_name":"seller"},"text":"\\u043f\\u0440\\u0438\\u0432\\u0435\\u0442"}}]}'
)


class Breaking(HTTPXRequest):
    """Нижний транспорт, который всегда рвёт соединение.

    Наследник HTTPXRequest, а не BaseRequest, затем, чтобы встать в цепочку
    под `netwatch.WatchedRequest`: присмотр зовёт нижний транспорт через
    `super()`, и подменять надо именно его, а не присмотр.
    """

    error = TimedOut("Pool timeout")

    def __init__(self):
        super().__init__()
        self.attempts = 0

    async def do_request(self, *args, **kwargs):
        self.attempts += 1
        raise type(self).error


class Flaky(Breaking):
    """Рвётся заданное число раз, потом отвечает «обновлений нет»."""

    breaks = 3

    async def do_request(self, *args, **kwargs):
        self.attempts += 1
        if self.attempts <= type(self).breaks:
            raise TimedOut("Pool timeout")
        return 200, NO_UPDATES


class WatchedBreaking(netwatch.WatchedRequest, Breaking):
    """Присмотр настоящий, транспорт под ним рвётся."""


class WatchedFlaky(netwatch.WatchedRequest, Flaky):
    """Связь рвётся, потом возвращается сама."""


class Working(BaseRequest):
    """Рабочий транспорт для всего, кроме опроса: getMe, отправка сообщений."""

    def __init__(self):
        self.sent: list[bytes] = []

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        name = url.rsplit("/", 1)[-1]
        if name == "getMe":
            return 200, ME
        if name == "sendMessage":
            self.sent.append(request_data.parameters.get("text", "") if request_data else "")
            return 200, SENT
        return 200, OK


@pytest.fixture(autouse=True)
def fresh(monkeypatch, tmp_path):
    """Своя база на каждый тест и присмотр, не помнящий прошлый.

    Третьего шва тут не заводится: присмотр за связью пишет в журнал по пути
    по умолчанию, а по умолчанию во время прогона он и так временный (DATA_DIR
    переставлен в tests/conftest.py). Своя папка на каждый тест нужна затем,
    что часть проверок считает записи журнала поимённо.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "watch"))
    db.close_all()
    db.migrate()
    netwatch.reset()
    yield
    netwatch.reset()
    db.close_all()


@pytest.fixture
def quick(monkeypatch):
    """Те же настройки, но в секундах, а не в минутах: прогон не должен ждать."""
    monkeypatch.setattr(
        netwatch,
        "_conf",
        lambda: {
            "retry_base_sec": 0.01,
            "retry_factor": 2,
            "retry_max_sec": 0.04,
            "notice_after_sec": 0.05,
            "notice_gap_sec": 3600,
        },
    )


# --- числа настроек живут в конфиге ---


def test_pause_numbers_come_from_the_config():
    """В config.toml секция [telegram]: 2 секунды, вдвое, не больше 60."""
    section = config.settings()["telegram"]

    assert netwatch.pause_sec(1) == float(section["retry_base_sec"])
    assert netwatch.pause_sec(2) == float(section["retry_base_sec"]) * float(
        section["retry_factor"]
    )
    assert netwatch.notice_after_sec() == float(section["notice_after_sec"])
    assert netwatch.notice_gap_sec() == float(section["notice_gap_sec"])


def test_pause_grows_and_stops_at_the_ceiling():
    """Пауза растёт, а не долбит сеть, но и не уходит в бесконечность."""
    paused = [netwatch.pause_sec(n) for n in range(1, 12)]

    assert paused == sorted(paused), "пауза обязана только расти"
    assert paused[0] > 0
    assert max(paused) == netwatch.pause_sec(100) == float(
        config.settings()["telegram"]["retry_max_sec"]
    )


def test_no_pause_while_the_link_is_fine():
    assert netwatch.pause_sec(0) == 0.0
    assert netwatch.failures() == 0


# --- бот переживает обрыв ---


@pytest.mark.asyncio
async def test_broken_link_does_not_stop_the_poll(quick):
    """Несколько неудач подряд, и опрос по-прежнему идёт."""
    transport = WatchedBreaking()
    app = (
        ApplicationBuilder()
        .token("1:aaa")
        .request(Working())
        .get_updates_request(transport)
        .build()
    )
    netwatch.set_polling(lambda: app.updater is not None and app.updater.running)

    await app.initialize()
    await app.start()
    await app.updater.start_polling(bootstrap_retries=-1)
    try:
        await asyncio.sleep(0.4)
        attempts = transport.attempts
        assert attempts >= 3, "после первой неудачи опрос обязан пробовать дальше"
        assert app.updater.running, "опрос не бросили"

        await asyncio.sleep(0.2)
        assert transport.attempts > attempts, "опрос продолжается и дальше"
    finally:
        app.updater._Updater__polling_cleanup_cb = None
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


@pytest.mark.asyncio
async def test_bot_answers_again_when_the_link_comes_back(quick, monkeypatch):
    """Связь вернулась сама, без выключения и запуска: бот отвечает на команду."""
    monkeypatch.setattr(Flaky, "breaks", 3)
    transport = WatchedFlaky()
    main = Working()
    app = (
        ApplicationBuilder()
        .token("1:aaa")
        .request(main)
        .get_updates_request(transport)
        .build()
    )
    netwatch.set_polling(lambda: app.updater is not None and app.updater.running)

    answered: list[str] = []

    async def reply(update, context):
        answered.append(update.effective_message.text)
        await update.effective_message.reply_text("на связи")

    app.add_handler(MessageHandler(filters.ALL, reply))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(bootstrap_retries=-1)
    try:
        # Ждём, пока обрыв кончится, и подкладываем сообщение селлера.
        for _ in range(100):
            if transport.attempts > Flaky.breaks:
                break
            await asyncio.sleep(0.02)

        # Одно сообщение селлера и дальше тишина: подставной Telegram не умеет
        # отмечать прочитанное по offset, и повтор означал бы вечный разбор
        # одного и того же сообщения.
        delivered: list[bool] = []

        async def one_update(*args, **kwargs):
            transport.attempts += 1
            if delivered:
                return 200, NO_UPDATES
            delivered.append(True)
            return 200, ONE_UPDATE

        monkeypatch.setattr(Flaky, "do_request", one_update)
        for _ in range(100):
            if answered:
                break
            await asyncio.sleep(0.02)

        assert answered == ["привет"], "после обрыва бот снова разбирает сообщения"
        assert "на связи" in main.sent
    finally:
        app.updater._Updater__polling_cleanup_cb = None
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


@pytest.mark.asyncio
async def test_broken_link_does_not_spin_without_a_pause(quick):
    """Обрыв без паузы это тридцать тысяч попыток в секунду. Пауза обязана быть."""
    transport = WatchedBreaking()

    started = asyncio.get_running_loop().time()
    for _ in range(4):
        with pytest.raises(TimedOut):
            await transport.do_request("https://x/getUpdates", "POST")
    spent = asyncio.get_running_loop().time() - started

    assert transport.attempts == 4
    expected = sum(netwatch.pause_sec(n) for n in range(1, 4))
    assert spent >= expected * 0.5, "между попытками обязана быть пауза"


@pytest.mark.asyncio
async def test_shutdown_does_not_wait_for_the_pause(quick, monkeypatch):
    """На выключении паузу не держат: бот не обязан гаснуть лишнюю минуту."""
    monkeypatch.setattr(
        netwatch, "_conf", lambda: {"retry_base_sec": 30, "retry_factor": 2}
    )
    transport = WatchedBreaking()
    netwatch.set_polling(lambda: False)

    started = asyncio.get_running_loop().time()
    with pytest.raises(TimedOut):
        await transport.do_request("https://x/getUpdates", "POST")

    assert asyncio.get_running_loop().time() - started < 1.0


# --- владелец узнаёт о затяжном обрыве, но не о каждом ---


@pytest.mark.asyncio
async def test_long_outage_reaches_the_owner_in_words(quick):
    said: list[str] = []
    netwatch.set_notifier(lambda text: said.append(text))

    # Первая неудача это ещё не новость.
    await netwatch.failed(NetworkError("httpx.ConnectError: "))
    assert said == []

    # Обрыв затянулся: порог взят из настроек, ждать его целиком незачем.
    netwatch._since = netwatch._now() - timedelta(seconds=10)
    await netwatch.failed(NetworkError("httpx.ConnectError: "))

    assert len(said) == 1
    assert "Telegram" in said[0]
    assert "перезапускать меня не нужно" in said[0]
    assert not any(dash in said[0] for dash in LONG_DASHES)


@pytest.mark.asyncio
async def test_owner_hears_that_the_link_came_back(quick):
    said: list[str] = []
    netwatch.set_notifier(lambda text: said.append(text))

    await netwatch.failed(TimedOut("Pool timeout"))
    netwatch._since = netwatch._now() - timedelta(seconds=10)
    await netwatch.failed(TimedOut("Pool timeout"))
    await netwatch.succeeded()

    assert len(said) == 2
    assert "вернулась" in said[1]
    assert netwatch.failures() == 0


@pytest.mark.asyncio
async def test_short_outages_do_not_become_a_stream(quick):
    """Десять коротких обрывов подряд не дают владельцу ни одного сообщения."""
    said: list[str] = []
    netwatch.set_notifier(lambda text: said.append(text))

    for _ in range(10):
        await netwatch.failed(NetworkError("httpx.ConnectError: "))
        await netwatch.succeeded()

    assert said == [], "обрыв на секунду это не новость"


@pytest.mark.asyncio
async def test_frequent_long_outages_are_said_once(quick, monkeypatch):
    """Мигающая связь: о втором затяжном обрыве подряд владельцу не пишут."""
    said: list[str] = []
    netwatch.set_notifier(lambda text: said.append(text))

    for _ in range(5):
        await netwatch.failed(TimedOut("Pool timeout"))
        netwatch._since = netwatch._now() - timedelta(seconds=10)
        await netwatch.failed(TimedOut("Pool timeout"))
        await netwatch.succeeded()

    # Один разговор: «связи нет» и «связь вернулась». Остальные четыре обрыва
    # остались в журнале, потому что пауза между сообщениями ещё не вышла.
    assert len(said) == 2, said
    hushed = [
        row["message"]
        for row in audit.recent(limit=50)
        if "промолчал" in row["message"]
    ]
    assert hushed, "промолчать можно, но в журнале след обязан остаться"


@pytest.mark.asyncio
async def test_journal_keeps_the_outage_without_flooding(quick):
    """Начало, затягивание и конец: три записи, а не запись на каждую попытку."""
    netwatch.set_notifier(None)

    await netwatch.failed(TimedOut("Pool timeout"))
    netwatch._since = netwatch._now() - timedelta(seconds=10)
    for _ in range(20):
        await netwatch.failed(TimedOut("Pool timeout"))
    await netwatch.succeeded()

    rows = [row for row in audit.recent(limit=100) if row["kind"] == netwatch.JOURNAL_KIND]
    assert len(rows) == 3, [row["message"] for row in rows]
    levels = {row["level"] for row in rows}
    assert "warning" in levels and "error" in levels and "info" in levels


@pytest.mark.asyncio
async def test_owner_learns_about_the_outage_even_if_the_word_did_not_reach_him(quick):
    """Telegram недостижим целиком: слово не ушло, но владелец узнает потом.

    Сказать в Telegram, когда Telegram не отвечает, нечем, и делать вид, что
    сказали, нельзя: владелец остался бы без новости вообще.
    """
    said: list[str] = []

    async def send(text: str) -> None:
        if not said:
            raise NetworkError("httpx.ConnectError: ")
        said.append(text)

    async def send_then_work(text: str) -> None:
        said.append(text)

    netwatch.set_notifier(send)
    await netwatch.failed(TimedOut("Pool timeout"))
    netwatch._since = netwatch._now() - timedelta(seconds=10)
    await netwatch.failed(TimedOut("Pool timeout"))
    assert said == [], "во время обрыва слово не дошло"

    netwatch.set_notifier(send_then_work)
    await netwatch.succeeded()

    assert len(said) == 1
    assert "вернулась" in said[0]


# --- сбор данных не зависит от Telegram ---


@pytest.mark.asyncio
async def test_collecting_goes_on_while_telegram_is_down(db_path, quick):
    """Telegram недоступен, Wildberries доступен: сбор обязан идти.

    Воронку по дням Wildberries отдаёт только за неделю назад, и пропущенные
    сутки не вернуть никаким перезапуском. Поэтому недоступность Telegram
    гасит доставку, но не работу.
    """
    queue.reset()
    client = db.admin_repo(db_path).ensure_client(100500)
    collected: list[int] = []

    def collect(task):
        collected.append(task.id)

    queue.register("rnp_collect_client", collect, quiet=True)

    # Доставка в Telegram не работает вовсе.
    async def dead(client_id, text):
        raise NetworkError("httpx.ConnectError: ")

    queue.set_notifier(dead)
    queue.enqueue(client, "rnp_collect_client", {"day": "2026-10-16"}, path=db_path)

    # И транспорт опроса тоже не работает.
    transport = WatchedBreaking()
    with pytest.raises(TimedOut):
        await transport.do_request("https://x/getUpdates", "POST")

    assert await queue.run_once(path=db_path) is True
    assert collected, "сбор данных не зависит от доступности Telegram"
    assert db.admin_repo(db_path).task(collected[0])["state"] == queue.DONE
    queue.reset()


@pytest.mark.asyncio
async def test_worker_keeps_working_through_a_long_outage(db_path, quick):
    """Воркер очереди живёт в том же процессе, но обрыв его не держит."""
    queue.reset()
    client = db.admin_repo(db_path).ensure_client(100501)
    done: list[int] = []
    queue.register("ads_collect_client", lambda task: done.append(task.id), quiet=True)
    queue.set_notifier(None)
    queue.enqueue(client, "ads_collect_client", {}, path=db_path)

    stop = asyncio.Event()
    worker = asyncio.create_task(queue.run_worker(path=db_path, poll_sec=0.02, stop=stop))
    transport = WatchedBreaking()

    async def keep_breaking():
        for _ in range(5):
            try:
                await transport.do_request("https://x/getUpdates", "POST")
            except TimedOut:
                pass

    await asyncio.gather(keep_breaking())
    for _ in range(100):
        if done:
            break
        await asyncio.sleep(0.02)
    stop.set()
    await worker

    assert done, "очередь обязана разбираться и при оборванной связи"
    assert transport.attempts == 5
    queue.reset()


def test_outage_length_is_measured_from_the_first_failure():
    assert netwatch.outage_sec() == 0.0
    netwatch._since = MOMENT
    assert netwatch.outage_sec(MOMENT + timedelta(minutes=11)) == pytest.approx(660.0)


def test_texts_for_the_owner_speak_russian_without_codes():
    for text in (netwatch.OWNER_OUTAGE, netwatch.OWNER_BACK):
        assert not any(dash in text for dash in LONG_DASHES)
        assert "timeout" not in text.lower()
        assert "error" not in text.lower()
