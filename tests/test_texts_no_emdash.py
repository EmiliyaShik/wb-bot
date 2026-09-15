"""Длинные тире запрещены в текстах бота: код, конфиг, схема, шаблон окружения.

Требование ТЗ дословно: «Тексты бота на русском языке, без длинных тире».
Поэтому проверяются .py, .toml, .sql, .env.example и .gitignore - там живут
строки, которые видит селлер, и настройки, из которых они собираются.
Документация (.md) в область не входит: README и CLAUDE это не тексты бота.

Проверяется сырое содержимое файла, а не только строковые литералы: тире в
комментарии к миграции такое же длинное тире.

Пропусков тут нет и быть не может. Найдено тире - падаем, даже если файл занят
другой программой: иначе проверка молчала бы ровно тогда, когда нарушение есть.
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Сами символы записаны через chr, иначе этот файл провалил бы собственную проверку.
LONG_DASHES = {
    chr(0x2014): "длинное тире",
    chr(0x2013): "среднее тире",
    chr(0x2015): "горизонтальная черта",
}

SKIP_DIRS = {".autopilot", ".git", "__pycache__", ".venv", "venv", "node_modules", "data"}
TEXT_SUFFIXES = {".py", ".toml", ".sql"}
TEXT_NAMES = {".env.example", ".gitignore"}


def text_files() -> list[Path]:
    found = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        if path.suffix in TEXT_SUFFIXES or path.name in TEXT_NAMES:
            found.append(path)
    return found


def test_there_is_something_to_check():
    names = {path.name for path in text_files()}
    for expected in ("config.toml", "001_initial.sql", "texts.py", ".env.example", "bot.py"):
        assert expected in names, f"проверка не дошла до {expected}"


@pytest.mark.parametrize(
    "path", text_files(), ids=lambda p: str(p.relative_to(ROOT)).replace("\\", "/")
)
def test_no_long_dash(path: Path):
    try:
        content = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        pytest.fail(f"{path.name} не удалось прочитать, проверить тире нечем: {exc}")

    offenders = []
    for number, line in enumerate(content.splitlines(), start=1):
        for dash, name in LONG_DASHES.items():
            if dash in line:
                offenders.append(f"строка {number}: {name}")

    assert not offenders, (
        f"в {path.name} длинное тире, замените на дефис или запятую: "
        + "; ".join(offenders)
    )
