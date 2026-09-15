"""Реестр хендлеров: модули находят себя сами.

Новый хендлер это новый файл в этой папке с функцией register(app).
Ничего перечислять руками и ничего править в bot/app.py не нужно.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from types import ModuleType

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
        except Exception:
            logger.exception("хендлер %s не импортировался, пропускаю", full_name)
            continue
        register = getattr(module, "register", None)
        if not callable(register):
            logger.debug("в модуле %s нет функции register, пропускаю", full_name)
            continue
        try:
            register(app)
        except Exception:
            logger.exception("хендлер %s не зарегистрировался", full_name)
            continue
        registered.append(info.name)
    return registered
