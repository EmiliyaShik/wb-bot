"""Реестр хендлеров: модули находят себя сами.

Новый хендлер это новый файл в этой папке с функцией register(app).
Ничего перечислять руками и ничего править в bot/app.py не нужно.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from types import ModuleType

from core import audit

logger = logging.getLogger(__name__)


def register_all(app, package: ModuleType | None = None) -> list[str]:
    """Импортирует модули пакета и вызывает у каждого register(app).

    Возвращает имена модулей, которые зарегистрировались. Модуль, который
    не импортировался, не роняет остальные: ошибка уходит в лог.
    """
    package = package or __import__(__name__, fromlist=["*"])
    registered: list[str] = []
    for info in sorted(pkgutil.iter_modules(package.__path__), key=lambda m: m.name):
        if info.name.startswith("_"):
            continue
        full_name = f"{package.__name__}.{info.name}"
        try:
            module = importlib.import_module(full_name)
        except Exception as exc:  # noqa: BLE001 - один сбойный файл не роняет бота
            logger.exception("хендлер %s не импортировался, пропускаю", full_name)
            # Молча пропустить нельзя: без admin.py бот поднимется без
            # ограничителя частоты и без админ-команд, и узнать об этом
            # будет неоткуда, кроме вывода в консоль.
            audit.log(
                "handlers",
                None,
                f"Хендлер {full_name} не импортировался и выключен: "
                f"{type(exc).__name__}: {exc}",
                level="error",
            )
            continue
        register = getattr(module, "register", None)
        if not callable(register):
            logger.debug("в модуле %s нет функции register, пропускаю", full_name)
            continue
        try:
            register(app)
        except Exception as exc:  # noqa: BLE001
            logger.exception("хендлер %s не зарегистрировался", full_name)
            audit.log(
                "handlers",
                None,
                f"Хендлер {full_name} не зарегистрировался и выключен: "
                f"{type(exc).__name__}: {exc}",
                level="error",
            )
            continue
        registered.append(info.name)
    return registered
