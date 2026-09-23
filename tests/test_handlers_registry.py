"""Хендлеры находят себя сами: новый файл в bot/handlers и всё."""

import sys

import pytest

import bot.handlers as handlers
from core import audit, db


@pytest.fixture
def sandbox(tmp_path):
    """Фабрика временных пакетов. Убирает за собой sys.path и sys.modules,
    чтобы порядок тестов ни на что не влиял."""
    path_before = list(sys.path)
    modules_before = set(sys.modules)

    def make(name: str, body: str, extra: dict[str, str] | None = None):
        package = tmp_path / name
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "alpha.py").write_text(body, encoding="utf-8")
        (package / "_private.py").write_text(
            "raise RuntimeError('не должен импортироваться')\n", encoding="utf-8"
        )
        for file_name, content in (extra or {}).items():
            (package / file_name).write_text(content, encoding="utf-8")
        sys.path.insert(0, str(tmp_path))
        __import__(name)
        return sys.modules[name]

    yield make

    sys.path[:] = path_before
    for name in set(sys.modules) - modules_before:
        del sys.modules[name]


class FakeApp:
    def __init__(self):
        self.added = []

    def add_handler(self, handler, group=0):
        self.added.append((handler, group))


def test_register_all_calls_register_of_every_module(sandbox):
    body = "REGISTERED = []\n\n\ndef register(app):\n    REGISTERED.append(app)\n"
    package = sandbox("fake_handlers_ok", body)
    app = FakeApp()
    names = handlers.register_all(app, package=package)
    assert names == ["alpha"]
    assert sys.modules["fake_handlers_ok.alpha"].REGISTERED == [app]


def test_module_without_register_is_skipped(sandbox):
    package = sandbox("fake_handlers_plain", "VALUE = 1\n")
    assert handlers.register_all(FakeApp(), package=package) == []


def test_broken_module_does_not_stop_the_others(sandbox):
    package = sandbox(
        "fake_handlers_broken",
        "raise ImportError('сломан')\n",
        {"beta.py": "def register(app):\n    app.add_handler('beta')\n"},
    )
    app = FakeApp()
    assert handlers.register_all(app, package=package) == ["beta"]
    assert app.added == [("beta", 0)]


def test_sandbox_leaves_nothing_behind():
    # предыдущие тесты подменяли sys.path и sys.modules, после них чисто
    assert "fake_handlers_ok" not in sys.modules
    assert not [entry for entry in sys.path if "pytest-of" in entry]


def test_own_package_is_importable():
    # в пакете хендлеров пока может не быть ни одного модуля, и это нормально
    assert isinstance(handlers.register_all(FakeApp()), list)


def test_broken_handler_is_visible_in_the_journal(monkeypatch, tmp_path, sandbox):
    """Без admin.py бот поднимется без ограничителя частоты и админ-команд,
    поэтому сбой импорта обязан попасть в журнал, а не только в stdout."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    db.migrate()
    body = "raise ImportError('нет модуля telegram')" + chr(10)
    package = sandbox("fake_handlers_journal", body)
    handlers.register_all(FakeApp(), package=package)

    errors = [row["message"] for row in audit.recent(level="error")]
    db.close_all()
    assert any("fake_handlers_journal.alpha" in message for message in errors), errors
    assert any("ImportError" in message for message in errors)
