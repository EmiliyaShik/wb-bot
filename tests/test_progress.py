"""Рассказ о ходе долгой выгрузки: одно живое сообщение вместо молчания."""

from __future__ import annotations

import logging
from datetime import date
from types import SimpleNamespace

import pytest

from agents import finance
from bot import progress
from core import db, wbapi

from tests.test_finance import FakeTime, ROWS, cabinet, wb_http  # noqa: F401


# --- проценты ---------------------------------------------------------------


def test_percent_counts_from_the_date_of_the_last_row():
    first, last = date(2026, 1, 1), date(2026, 12, 31)
    assert finance.progress_percent(first, last, date(2026, 1, 1)) == 0
    assert finance.progress_percent(first, last, date(2026, 7, 1)) == 50
    assert finance.progress_percent(first, last, date(2026, 12, 31)) == 100


def test_percent_never_leaves_the_period():
    first, last = date(2026, 3, 1), date(2026, 3, 31)
    # Дата раньше начала периода и позже конца: оба края прижимаются.
    assert finance.progress_percent(first, last, date(2025, 1, 1)) == 0
    assert finance.progress_percent(first, last, date(2027, 1, 1)) == 100
    # Строк ещё нет вовсе.
    assert finance.progress_percent(first, last, None) == 0


def test_one_day_period_is_whole_at_once():
    day = date(2026, 5, 5)
    assert finance.progress_percent(day, day, day) == 100


# --- выгрузка рассказывает о себе -------------------------------------------


@pytest.mark.asyncio
async def test_collect_tells_where_it_got_to(cabinet, db_path):  # noqa: F811
    client_id, path = cabinet, db_path
    clock = FakeTime()
    said: list[tuple[str, bool]] = []

    async def say(cid, text, final=False):
        assert cid == client_id
        said.append((text, final))

    finance.set_progress(say)
    try:
        await finance.collect(
            client_id,
            date(2026, 9, 1),
            date(2026, 9, 30),
            progress=True,
            http=wb_http(),
            path=path,
            clock=clock.clock,
            sleep=clock.sleep,
        )
    finally:
        finance.set_progress(None)

    assert said, "о долгой работе не сказано ничего"
    assert "Выгружаю" in said[0][0]
    assert any("Дошёл до 2026-09-" in text for text, _ in said)
    assert said[-1][1] is True and "закончена" in said[-1][0]


@pytest.mark.asyncio
async def test_nightly_collect_stays_silent(cabinet, db_path):  # noqa: F811
    """Сбор по расписанию никого не будит: адресата у него нет."""
    client_id, path = cabinet, db_path
    clock = FakeTime()
    said: list[str] = []

    async def say(cid, text, final=False):
        said.append(text)

    finance.set_progress(say)
    try:
        await finance.collect(
            client_id,
            date(2026, 9, 1),
            date(2026, 9, 30),
            http=wb_http(),
            path=path,
            clock=clock.clock,
            sleep=clock.sleep,
        )
    finally:
        finance.set_progress(None)

    assert said == []


@pytest.mark.asyncio
async def test_broken_storyteller_does_not_break_the_download(
    cabinet, db_path, caplog
):  # noqa: F811
    """Рассказ не важнее работы: сбой рассказчика выгрузку не роняет."""
    client_id, path = cabinet, db_path
    clock = FakeTime()

    async def say(cid, text, final=False):
        raise RuntimeError("телеграм молчит")

    finance.set_progress(say)
    try:
        with caplog.at_level(logging.ERROR):
            result = await finance.collect(
                client_id,
                date(2026, 9, 1),
                date(2026, 9, 30),
                progress=True,
                http=wb_http(),
                path=path,
                clock=clock.clock,
                sleep=clock.sleep,
            )
    finally:
        finance.set_progress(None)

    assert result.rows == len(ROWS)


# --- одно сообщение, а не поток ---------------------------------------------


class FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []
        self.edited: list[tuple[int, int, str]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((int(chat_id), text))
        return SimpleNamespace(message_id=777)

    async def edit_message_text(self, chat_id, message_id, text, **kwargs):
        self.edited.append((int(chat_id), int(message_id), text))


@pytest.fixture
def reporter(db_path):
    bot = FakeBot()
    client_id = db.admin_repo(db_path).ensure_client(4242)
    clock = SimpleNamespace(now=1000.0)
    report = progress.make_reporter(
        SimpleNamespace(bot=bot), db_path, min_gap_sec=20.0, clock=lambda: clock.now
    )
    return bot, client_id, clock, report


@pytest.mark.asyncio
async def test_first_news_is_sent_and_the_rest_edit_it(reporter):
    bot, client_id, clock, report = reporter

    await report(client_id, "начал")
    clock.now += 60
    await report(client_id, "середина")

    assert [text for _, text in bot.sent] == ["начал"]
    assert [text for _, _, text in bot.edited] == ["середина"]
    assert bot.edited[0][1] == 777


@pytest.mark.asyncio
async def test_frequent_news_are_skipped(reporter):
    bot, client_id, clock, report = reporter

    await report(client_id, "начал")
    clock.now += 1
    await report(client_id, "через секунду")

    assert bot.edited == [], "правка чаще паузы ушла в Telegram"


@pytest.mark.asyncio
async def test_last_news_ignores_the_pause(reporter):
    bot, client_id, clock, report = reporter

    await report(client_id, "начал")
    clock.now += 1
    await report(client_id, "готово", True)

    assert [text for _, _, text in bot.edited] == ["готово"]


@pytest.mark.asyncio
async def test_next_run_starts_a_new_message(reporter):
    bot, client_id, clock, report = reporter

    await report(client_id, "начал")
    await report(client_id, "готово", True)
    clock.now += 100
    await report(client_id, "начал снова")

    assert [text for _, text in bot.sent] == ["начал", "начал снова"]


@pytest.mark.asyncio
async def test_unknown_client_is_not_written_to(reporter):
    bot, client_id, clock, report = reporter

    await report(999999, "кому-то")

    assert bot.sent == [] and bot.edited == []


@pytest.mark.asyncio
async def test_silent_telegram_is_not_an_avalanche(reporter, caplog):
    """Молчащий Telegram это запись в журнал, а не сбой работы."""
    bot, client_id, clock, report = reporter

    async def boom(*args, **kwargs):
        raise RuntimeError("telegram timeout")

    bot.send_message = boom
    with caplog.at_level(logging.ERROR):
        await report(client_id, "начал")
