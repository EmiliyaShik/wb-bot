"""Сводка отчёта не перерастает предел Telegram.

Отчёт о прибыли за год у живого кабинета упёрся в этот предел и не дошёл
вовсе: Telegram отвечает «Message is too long», задача падает, и вместо
разбора клиент получает сообщение о сбое. Книга при этом собрана и цела,
то есть потеряно было только сообщение.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from bot import texts

ROOT = Path(__file__).resolve().parent.parent
HANDLERS = ROOT / "bot" / "handlers"

# Хендлеры, которые отдают клиенту разбор: их сводка растёт вместе с числом
# артикулов, кампаний и недель, и предела у неё своего нет.
REPORTS = ("ads", "diagnostic", "dynamics", "finance", "funnel", "profit", "rnp")


# --- сам ограничитель ---


def test_short_text_is_left_alone():
    text = "<b>Коротко</b> и ясно"
    assert texts.clip(text) == text


def test_long_text_is_cut_to_the_limit():
    long = "\n".join(f"<b>строка {i}</b> про деньги" for i in range(500))
    out = texts.clip(long)
    assert len(out) <= texts.MESSAGE_LIMIT
    assert len(out) < len(long)


def test_cut_says_where_the_rest_is():
    long = "\n".join(f"строка {i}" for i in range(2000))
    assert "в файле" in texts.clip(long)


def test_cut_happens_on_whole_lines():
    long = "\n".join(f"строка номер {i}" for i in range(2000))
    kept = texts.clip(long).split("\n")
    # Последняя уцелевшая строка это целая строка, а не её половина.
    assert kept[-4] in long.split("\n")


def test_open_tag_is_closed_after_the_cut():
    """Незакрытый тег Telegram не показывает вовсе: сообщение не уйдёт."""
    long = "<b>" + "\n".join(f"строка {i}" for i in range(2000)) + "</b>"
    out = texts.clip(long)
    assert out.count("<b>") == out.count("</b>")


def test_one_endless_line_is_cut_by_characters():
    """Выбора нет: целых строк в такой сводке просто не существует."""
    out = texts.clip("а" * 9000)
    assert len(out) <= texts.MESSAGE_LIMIT and "в файле" in out


def test_clip_returns_our_own_mark():
    assert isinstance(texts.clip("коротко"), texts.Safe)


# --- сторож: ни одна сводка не уходит мимо ограничителя ---


@pytest.mark.parametrize("module", REPORTS)
def test_report_summary_goes_through_the_clipper(module):
    """Сводка отчёта обязана ехать через `texts.clip`.

    Сторож смотрит на доставку готового отчёта (`make_sender`): это
    единственное место, где длина сообщения не ограничена ничем, кроме
    числа артикулов у клиента. Короткие ответы команд сюда не попадают.
    """
    source = (HANDLERS / f"{module}.py").read_text(encoding="utf-8")
    # Доставка называется по-разному: отчёт с файлом отдаёт `make_sender`,
    # рассылка без файла `make_delivery`. Имя не важно, важно одно место.
    where = next(
        (source.index(name) for name in ("def make_sender", "def make_delivery")
         if name in source),
        None,
    )
    assert where is not None, f"в {module} не нашлось доставки отчёта"
    sender = source[where:]
    sends = re.findall(r"text=([^,\n]+)", sender)
    assert sends, f"в {module} не нашлось отправки готового отчёта"
    assert all("clip" in one for one in sends), (
        f"сводка {module} уходит мимо ограничителя длины: {sends}"
    )


# --- обрезка не должна ломать разметку ---


def test_cut_does_not_leave_a_half_written_entity():
    """«&lt» без точки с запятой Telegram не разбирает вовсе.

    Нашлось обзором безопасности: сводка из одной длинной строки режется по
    знакам, и разрез попадал в середину экранированного символа. Ответ
    Telegram на такое тот же самый «Message is too long»... то есть отказ, от
    которого обрезка и защищает.
    """
    long = "Товар " + "&lt;кружка&gt; и &amp; " * 500
    out = texts.clip(long)
    body = out[: out.index("…")]
    assert not re.search(r"&[a-zA-Z#0-9]*$", body.rstrip())


def test_cut_does_not_leave_a_half_written_tag():
    long = "<b>" + "я" * 9000
    out = texts.clip(long)
    body = out[: out.index("…")]
    assert not re.search(r"<[^>]*$", body.rstrip())
    assert out.count("<b>") == out.count("</b>")


def test_whole_entities_survive_the_cut():
    """Отрезается только оборванное: целая сущность остаётся на месте."""
    out = texts.clip("&amp; " * 2000)
    assert "&amp;" in out
