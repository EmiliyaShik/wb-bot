"""Общие приспособления тестов. Шов один: путь к базе."""

import pytest

from core import db


@pytest.fixture
def db_path(tmp_path):
    """Временная база с применёнными миграциями."""
    path = tmp_path / "test.db"
    db.migrate(path)
    yield path
    db.close_all()
