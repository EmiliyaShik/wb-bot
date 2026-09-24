"""Общие приспособления тестов. Шов один: путь к базе.

Прогон обязан быть слепым к боевой базе владельца, и одного шва `path=` для
этого мало. Часть кода пути к базе не знает вовсе: реестр хендлеров
(`bot.handlers.register_all`), предупреждение о недоступности DaData,
запуск бота, обработчик ошибок. Такие места берут путь по умолчанию, а по
умолчанию он боевой, и прогон тестов молча дописывал журнал владельцу.

Затыкать их по одному бесполезно: следующее такое место появится, и про него
никто не вспомнит. Поэтому на время прогона переставляется сам «путь по
умолчанию»: DATA_DIR смотрит во временную папку, и боевого пути в тестах
просто не существует. Третьего шва при этом не заводится: DATA_DIR это
обычная переменная окружения бота, та же самая, которой пользуется хостинг.

Сверху стоит сторож. Пока идёт прогон, sqlite3.connect отказывается открывать
файл вне временной папки. Он ловит не только запись, но и само создание
боевой базы на чистой машине, и код, который придумал путь мимо
`config.data_dir()`. Проверку того, что сторож на месте и кусается, ведёт
tests/test_prod_db_untouched.py.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from core import config, db

# Настоящий sqlite3.connect. Сохраняется до подмены, чтобы сторож звал его,
# а не сам себя.
_REAL_CONNECT = sqlite3.connect

# Имена, за которыми файла нет: база в памяти и безымянная временная база.
# Запрещать их не за что.
_NOT_A_FILE = frozenset({":memory:", ""})

TOUCHED_PRODUCTION = (
    "тест попытался открыть базу вне временной папки прогона: {path}. "
    "Так тест дописывает боевую базу владельца или создаёт её заново. "
    "Передайте path= во временную базу или пользуйтесь путём по умолчанию: "
    "во время прогона он и так временный (см. tests/conftest.py)."
)

LEAKED = (
    "прогон пытался открыть базу вне временной папки, попыток {count}: {paths}. "
    "Отказ сторожа мог не дойти до теста: core.audit гасит любую ошибку записи, "
    "чтобы журнал не ронял бота. Поэтому попытки собираются за весь прогон и "
    "предъявляются здесь, когда бы они ни случились."
)


def _normal(value: object) -> str:
    """Путь в сравнимом виде: абсолютный, в регистре файловой системы."""
    return os.path.normcase(os.path.abspath(os.fsdecode(value)))


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root + os.sep)


def production_db_files(previous_data_dir: str | None = None) -> tuple[Path, ...]:
    """Файлы базы, которые тест не имеет права трогать.

    Локальный `./data`, хостинговый `/app/data` и папка, которую владелец
    задал переменной DATA_DIR до того, как прогон её переставил.
    """
    found = [
        config.LOCAL_DATA_DIR / config.DB_FILENAME,
        config.HOST_DATA_DIR / config.DB_FILENAME,
    ]
    if previous_data_dir:
        found.append(Path(previous_data_dir) / config.DB_FILENAME)
    seen: dict[str, Path] = {}
    for item in found:
        seen.setdefault(_normal(item), item)
    return tuple(seen.values())


@dataclass(frozen=True)
class Seal:
    """Что сторож знает о прогоне: временная папка и боевые файлы до старта."""

    data_dir: Path
    sandbox_root: Path
    production: tuple[Path, ...]
    existed: dict[str, bool]
    # Попытки открыть базу мимо временной папки за весь прогон. Список, а не
    # одно «да/нет»: разбирать придётся по путям.
    refusals: list[str] = field(default_factory=list)
    # Ноль или больше нарочных проверок сторожа, которые в список не идут.
    probes: list[bool] = field(default_factory=list)

    @contextmanager
    def probing(self):
        """Нарочная проверка сторожа: отказ ожидаем и утечкой не считается."""
        self.probes.append(True)
        try:
            yield
        finally:
            self.probes.pop()

    def inside(self, path: str | Path) -> bool:
        """Лежит ли путь внутри временной папки прогона."""
        return _inside(_normal(path), _normal(self.sandbox_root))

    def existed_before(self, path: str | Path) -> bool:
        """Был ли этот боевой файл на месте до первого теста."""
        return self.existed.get(_normal(path), False)

    def refusal(self, path: str | Path) -> str:
        """Текст отказа сторожа: тесту незачем собирать его самому."""
        return TOUCHED_PRODUCTION.format(path=path)


@pytest.fixture(scope="session", autouse=True)
def sealed_data_dir(tmp_path_factory):
    """Папка данных на время прогона временная, боевая недостижима.

    Фикстура автоматическая и на весь прогон: защита, которую надо не забыть
    попросить, защитой не является. Отдельный тест по-прежнему волен
    переставить DATA_DIR своим monkeypatch, лишь бы во временную папку:
    сторож ниже следит именно за этим.
    """
    sandbox_root = Path(tmp_path_factory.getbasetemp())
    data_dir = tmp_path_factory.mktemp("data")
    previous = os.environ.get("DATA_DIR")
    production = production_db_files(previous)
    # Существование запоминается до первого теста: если боевого файла не было,
    # он не имеет права появиться. Размер и время правки не годятся, на машине
    # владельца боевой бот пишет в свою базу параллельно с прогоном.
    existed = {_normal(item): item.exists() for item in production}

    allowed = _normal(sandbox_root)
    seal = Seal(
        data_dir=data_dir,
        sandbox_root=sandbox_root,
        production=production,
        existed=existed,
    )

    def guarded_connect(database, *args, **kwargs):
        try:
            name = os.fsdecode(database)
        except TypeError:  # не путь вовсе, пусть разбирается сам sqlite3
            return _REAL_CONNECT(database, *args, **kwargs)
        if name not in _NOT_A_FILE and not _inside(_normal(name), allowed):
            # Отказ записывается до броска: core.audit гасит ошибку записи,
            # и без этого списка утечка осталась бы совсем незаметной.
            if not seal.probes:
                seal.refusals.append(name)
            raise RuntimeError(TOUCHED_PRODUCTION.format(path=name))
        return _REAL_CONNECT(database, *args, **kwargs)

    os.environ["DATA_DIR"] = str(data_dir)
    sqlite3.connect = guarded_connect
    try:
        yield seal
    finally:
        db.close_all()
        sqlite3.connect = _REAL_CONNECT
        if previous is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = previous
        # Проверка стоит в разборке фикстуры, а не в тесте: тест ловит только
        # то, что случилось до него, а очередное такое место появится где
        # угодно в прогоне. Здесь прогон уже закончился, и поздних попыток
        # быть не может.
        if seal.refusals:
            raise AssertionError(
                LEAKED.format(
                    count=len(seal.refusals), paths=", ".join(sorted(set(seal.refusals)))
                )
            )


@pytest.fixture
def db_path(tmp_path):
    """Временная база с применёнными миграциями."""
    path = tmp_path / "test.db"
    db.migrate(path)
    yield path
    db.close_all()
