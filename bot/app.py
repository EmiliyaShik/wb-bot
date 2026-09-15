"""Сборка приложения Telegram.

Этот файл после таска 01 не меняется: новый хендлер это новый файл в
bot/handlers с функцией register(app), реестр найдёт его сам.

Порядок важен. Внутри одной группы python-telegram-bot берёт первый
подходящий хендлер, поэтому:
  1) модули из bot/handlers - у них приоритет;
  2) базовые команды /start и /help - если модуль их не перехватил;
  3) обработчик артикула из legacy - запасной для любого текста.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from bot import texts
from bot.handlers import register_all
from core import audit, config, crypto, db

logger = logging.getLogger(__name__)


def startup() -> dict:
    """Готовит бота к работе: папка данных, миграции, честный отчёт о ключах.

    Ничего не роняет: без ключа шифрования и без списка админов бот работает,
    просто часть возможностей выключена, и об этом написано в журнале.
    """
    config.data_dir().mkdir(parents=True, exist_ok=True)
    version = db.migrate()

    admins = config.admin_ids()
    if not admins:
        audit.log(
            "system",
            None,
            "Переменная ADMIN_TELEGRAM_IDS пуста: админ-команд нет ни у кого. "
            "Укажите в ней свой Telegram ID, чтобы получить /stats и /tasks.",
            level="warning",
        )

    tokens_enabled = crypto.key_available()
    if not tokens_enabled:
        audit.log(
            "system",
            None,
            "Переменная ENCRYPTION_KEY не задана или испорчена: токены кабинетов "
            "принимать нельзя. " + crypto.KEY_HINT,
            level="warning",
        )

    audit.log(
        "system",
        None,
        f"Бот запущен. Схема базы {version}, админов {len(admins)}, "
        f"приём токенов {'включён' if tokens_enabled else 'выключен'}.",
    )
    return {
        "schema_version": version,
        "admins": len(admins),
        "tokens_enabled": tokens_enabled,
        "db_path": str(config.db_path()),
    }


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        texts.INTRO, parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        texts.HELP, parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


def register_base(app: Application) -> None:
    """Команды, которые есть всегда, даже когда ни один модуль не подключён."""
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))


def build_app(token: str) -> Application:
    """Собирает приложение со всеми хендлерами в правильном порядке."""
    app = Application.builder().token(token).build()

    names = register_all(app)
    logger.info("Хендлеры модулей: %s", ", ".join(names) if names else "пока нет")

    register_base(app)

    # Запасной обработчик текста ставится последним, иначе он перехватил бы
    # сообщения, адресованные модулям (ввод токена, себестоимость и прочее).
    from legacy import handlers as legacy_handlers

    legacy_handlers.register(app)

    return app
