"""Сторож: токен бота не должен попадать в журнал через чужие библиотеки.

Свой журнал мы чистим сами (core.audit.redact), но httpx на уровне INFO
печатает полный адрес запроса, а токен бота это часть адреса:
https://api.telegram.org/bot<ТОКЕН>/getMe. На хостинге журнал хранится и
бывает виден, так что утечка настоящая, а не теоретическая.
"""

from __future__ import annotations

import logging

import pytest

from bot import app


@pytest.mark.parametrize("name", app.URL_WRITING_LOGGERS)
def test_url_writing_libraries_are_kept_above_info(name, monkeypatch):
    """Порог поднят: ошибки видны, адреса с токеном в журнал не попадают."""
    logging.getLogger(name).setLevel(logging.NOTSET)
    app.hush_url_logging()
    assert logging.getLogger(name).level >= logging.WARNING


def test_startup_hushes_them_itself(db_path, monkeypatch):
    """Порог поднимает сам запуск: отдельного вызова помнить не нужно."""
    monkeypatch.setenv("DATA_DIR", str(db_path.parent))
    for name in app.URL_WRITING_LOGGERS:
        logging.getLogger(name).setLevel(logging.NOTSET)

    app.startup()

    assert all(
        logging.getLogger(name).level >= logging.WARNING
        for name in app.URL_WRITING_LOGGERS
    )
