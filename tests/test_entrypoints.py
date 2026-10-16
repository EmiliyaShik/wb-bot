"""Сторож: бота поднимает любой разумный способ запуска.

Хостинг собирает образ сам и команду запуска выбирает тоже сам. Bothost
собрал `CMD ["python", "-m", "bot.app"]`, хотя в `Procfile` написано
`python bot.py`. Модуль тогда просто импортировался и завершался с кодом
ноль: для хостинга «успешно отработала», карточка показывала «Работает», а
бот не поднимался вовсе и сообщения не читал никто. Нашлось это только
вопросом к самому Telegram, читает ли кто-нибудь обновления.

Поэтому запуск обязан быть один на все точки входа. Две копии разошлись бы
молча при первой же правке, и починили бы мы тогда только одну.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Как бота могут позвать: файлом, пакетом и модулем сборки. Последний это не
# фантазия, а то, что выбрал настоящий хостинг.
ENTRY_POINTS = ("bot.py", "bot/__main__.py", "bot/app.py")


def _tree(name: str) -> ast.Module:
    return ast.parse((ROOT / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", ENTRY_POINTS)
def test_every_entry_point_starts_the_bot(name: str) -> None:
    """У каждой точки входа есть запуск, а не только импорт."""
    guards = [
        node
        for node in _tree(name).body
        if isinstance(node, ast.If)
        and ast.dump(node.test).count("__main__")
    ]
    assert guards, f"{name} при запуске ничего не делает"


def test_the_start_itself_lives_in_one_place() -> None:
    """Токен читается и приложение запускается ровно в одном модуле.

    Если это появится во втором месте, точки входа начнут расходиться:
    одну поправят, вторую забудут, и забытая окажется той, которую зовёт
    хостинг.
    """
    callers = []
    for name in ENTRY_POINTS:
        text = (ROOT / name).read_text(encoding="utf-8")
        if "TELEGRAM_BOT_TOKEN" in text:
            callers.append(name)
    assert callers == ["bot/app.py"], (
        "запуск с чтением токена должен быть только в bot/app.py, "
        f"а он ещё и в {[n for n in callers if n != 'bot/app.py']}"
    )
