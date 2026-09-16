"""Точка входа WBРентген.

Запуск: python bot.py

Здесь только запуск: конфиг, миграции и сборка приложения живут в core и bot.
Старое поведение на месте: артикул или ссылка по-прежнему возвращают карточку.
"""

import logging
import os

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)


def main() -> None:
    from bot.app import run

    run(os.getenv("TELEGRAM_BOT_TOKEN"))


if __name__ == "__main__":
    main()
