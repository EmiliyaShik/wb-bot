"""Сторож: прогон тестов не трогает боевую базу владельца.

Дыра была настоящая. Код, который пути к базе не знает (реестр хендлеров,
предупреждение о недоступности DaData), брал путь по умолчанию, а по умолчанию
он боевой: в журнале владельца оседали записи про `fake_handlers_broken.alpha`
и `DaData недоступна (OSError)`, которых у него не происходило.

Затычка по местам не годится, мест будет больше. Закрыт весь класс: на время
прогона DATA_DIR смотрит во временную папку, а sqlite3.connect отказывается
открывать что-либо вне неё (tests/conftest.py). Здесь проверяется, что защита
на месте, что она кусается и что известные места больше не мимо.

Отказы сторожа копятся за весь прогон и предъявляются в разборке фикстуры:
тест видит только то, что случилось до него, а очередное такое место появится
где угодно. Здесь же список проверяется рано, чтобы виновника было видно рядом.

Чего этот сторож не ловит, честно:

- запись не через sqlite3: копирование файла базы, внешняя программа, правка
  журнала руками. Сторож стоит на соединении, а не на файле;
- `db.connect(боевой путь)` успевает создать папку данных до того, как сторож
  откажет в соединении. Пустая папка это не база, и файл в ней не появится,
  но папка останется;
- содержимое боевой базы не сверяется. Сравнивать размер и время правки
  нельзя: на машине владельца боевой бот пишет в свою базу параллельно с
  прогоном, и сверка врала бы через раз. Проверяется то, что не врёт: файла
  не было, файл не появился.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from types import ModuleType

import pytest

import bot.handlers as handlers
from core import audit, config, db


class _FakeApp:
    def add_handler(self, handler, group=0):
        pass


# --- защита на месте ---


def test_default_db_path_is_temporary(sealed_data_dir):
    """Путь по умолчанию во время прогона временный, а не боевой."""
    here = config.db_path()
    assert sealed_data_dir.inside(here), here
    for boevoy in sealed_data_dir.production:
        assert not sealed_data_dir.inside(boevoy), boevoy
        assert Path(here) != Path(boevoy)


def test_production_files_are_known(sealed_data_dir):
    """Сторожу есть что стеречь: локальная база владельца в списке."""
    assert sealed_data_dir.production, "список боевых файлов пуст"
    assert config.LOCAL_DATA_DIR / config.DB_FILENAME in sealed_data_dir.production
    assert {item.name for item in sealed_data_dir.production} == {config.DB_FILENAME}


def test_production_database_was_not_created(sealed_data_dir):
    """Файла боевой базы не было, он не появился.

    Это про чистую машину: раньше первый же прогон создавал
    `data/wbrentgen.db` и применял к нему схему.
    """
    for boevoy in sealed_data_dir.production:
        if sealed_data_dir.existed_before(boevoy):
            continue
        assert not boevoy.exists(), (
            f"прогон создал боевую базу {boevoy}: до старта её не было"
        )


# --- сторож кусается ---


def test_guard_refuses_production_database(sealed_data_dir):
    """Попытка открыть боевую базу падает, а не проходит молча."""
    boevoy = config.LOCAL_DATA_DIR / config.DB_FILENAME
    with sealed_data_dir.probing(), pytest.raises(RuntimeError) as beda:
        sqlite3.connect(str(boevoy))
    assert config.DB_FILENAME in str(beda.value)
    assert str(beda.value) == sealed_data_dir.refusal(str(boevoy))


def test_guard_refuses_any_path_outside_the_sandbox(sealed_data_dir):
    """Не только боевой путь: сторож стоит на всей папке прогона.

    Иначе его обошёл бы любой новый код, придумавший свой путь мимо
    config.data_dir().
    """
    with sealed_data_dir.probing(), pytest.raises(RuntimeError):
        sqlite3.connect(str(Path.home() / "выдуманная-сторожем.db"))


def test_deliberate_probes_are_not_counted_as_leaks(sealed_data_dir):
    """Список утечек чист: проверки сторожа в него не попадают.

    Сам список предъявляется в разборке фикстуры, когда прогон уже кончился.
    Здесь он проверяется рано, чтобы виновный тест было видно по соседству.
    """
    assert sealed_data_dir.refusals == []


def test_guard_lets_the_sandbox_through(tmp_path):
    """Сторож не падает на всём подряд: временная база открывается как обычно."""
    sqlite3.connect(str(tmp_path / "своя.db")).close()
    assert (tmp_path / "своя.db").exists()


def test_memory_database_is_allowed():
    """База в памяти это не файл, запрещать её не за что."""
    sqlite3.connect(":memory:").close()


# --- известные места пишут во временную базу ---


def test_journal_without_path_lands_in_the_temporary_database(sealed_data_dir):
    """audit.log без path пишет туда же, куда смотрит DATA_DIR прогона."""
    db.migrate()
    audit.log("сторож", None, "проверка пути по умолчанию")
    kinds = [row["kind"] for row in audit.recent(limit=20)]
    assert "сторож" in kinds, "запись без path не нашлась во временной базе"
    assert sealed_data_dir.inside(config.db_path())


def test_broken_handler_writes_into_the_temporary_database(tmp_path):
    """Реестр хендлеров: path у него есть не всегда, и это больше не беда.

    Именно такие записи («модуль ... не импортировался») и осели в боевой базе
    владельца.
    """
    db.migrate()
    folder = tmp_path / "guard_handlers"
    folder.mkdir()
    (folder / "alpha.py").write_text("VALUE = 1" + chr(10), encoding="utf-8")
    # Пакет, которого нет в sys.path: import_module по нему обязан упасть, а
    # register_all обязан записать это в журнал. Ровно та ветка, что текла.
    package = ModuleType("guard_handlers_not_importable")
    package.__path__ = [str(folder)]
    assert "guard_handlers_not_importable" not in sys.modules

    handlers.register_all(_FakeApp(), package=package)

    messages = [row["message"] for row in audit.recent(limit=50, level="error")]
    assert any("guard_handlers_not_importable.alpha" in text for text in messages), messages


@pytest.mark.asyncio
async def test_dadata_warning_lands_in_the_temporary_database(monkeypatch):
    """Недоступность DaData: path у audit.log там есть, а зовут его без него."""
    from core.billing import counterparty

    monkeypatch.setenv("DADATA_API_KEY", "ключ-для-сторожа")
    db.migrate()

    class Broken:
        async def post(self, *args, **kwargs):
            raise OSError("связи нет")

    assert await counterparty.lookup("1234567894", http=Broken()) is None
    messages = [row["message"] for row in audit.recent(limit=50, level="warning")]
    assert any("DaData недоступна" in text for text in messages), messages
