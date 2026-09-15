"""Точка входа WBРентген.

Запуск: python bot.py

Здесь только запуск: конфиг, миграции и сборка приложения живут в core и bot.
Старое поведение на месте: артикул или ссылка по-прежнему возвращают карточку.
"""

import logging
import os

from dotenv import load_dotenv
from telegram import Update

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit(
            "Не задан TELEGRAM_BOT_TOKEN. Создай файл .env на основе .env.example."
        )

    from bot.app import build_app, startup

    report = startup()
    logger.info(
        "База: %s, схема %s, админов %s, приём токенов %s",
        report["db_path"],
        report["schema_version"],
        report["admins"],
        "включён" if report["tokens_enabled"] else "выключен",
    )

    app = build_app(token)

    logger.info("Бот запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
