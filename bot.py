"""Точка входа «Рентген Продаж».

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
    # Сам запуск живёт в bot.app.main: хостинг может позвать и модуль, и этот
    # файл, и две копии запуска разошлись бы молча.
    from bot.app import main as start

    start()


if __name__ == "__main__":
    main()
