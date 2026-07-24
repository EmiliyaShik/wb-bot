"""Telegram-бот для анализа карточек товаров Wildberries.

Принимает артикул товара и возвращает название, цену, рейтинг и число отзывов.
"""

import logging
import os
import re

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from wb_api import (
    ProductNotFoundError,
    Product,
    WBApiError,
    WBBlockedError,
    fetch_product,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Артикул WB — это набор из 4–12 цифр. Достаём его в том числе из ссылки на товар.
ARTICLE_RE = re.compile(r"(\d{4,12})")


def parse_article(text: str) -> int | None:
    """Извлекает артикул из текста (число или ссылка на товар)."""
    match = ARTICLE_RE.search(text or "")
    return int(match.group(1)) if match else None


def format_product(product: Product) -> str:
    """Форматирует карточку товара для отправки в Telegram."""
    lines = [f"<b>{product.name}</b>"]

    if product.brand:
        lines.append(f"🏷 Бренд: {product.brand}")
    if product.supplier:
        lines.append(f"🏪 Продавец: {product.supplier}")

    if product.price is not None:
        price_line = f"💰 Цена: <b>{product.price:,.0f} ₽</b>".replace(",", " ")
        if product.old_price:
            old = f"{product.old_price:,.0f}".replace(",", " ")
            price_line += f" <s>{old} ₽</s>"
        lines.append(price_line)
    else:
        lines.append("💰 Цена: нет данных (возможно, нет в наличии)")

    if product.rating:
        stars = "⭐" * round(product.rating)
        lines.append(f"{stars} Рейтинг: {product.rating}")
    else:
        lines.append("⭐ Рейтинг: пока нет оценок")

    lines.append(f"💬 Отзывов: {product.feedbacks}")
    lines.append(f"🔢 Артикул: <code>{product.article}</code>")

    if product.characteristics:
        lines.append("\n<b>📋 Характеристики</b>")
        for name, value in product.characteristics[:6]:
            lines.append(f"• {name}: {value}")

    if product.description:
        desc = product.description
        if len(desc) > 400:
            desc = desc[:400].rstrip() + "…"
        lines.append(f"\n<b>📝 Описание</b>\n{desc}")

    if product.reviews:
        lines.append("\n<b>🗣 Отзывы</b>")
        for review in product.reviews:
            stars = f"{review.rating}⭐ " if review.rating else ""
            text = review.text
            if len(text) > 200:
                text = text[:200].rstrip() + "…"
            lines.append(f"• {stars}{text}")

    lines.append(f'\n<a href="{product.url}">Открыть на Wildberries</a>')

    return "\n".join(lines)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Привет! Я анализирую карточки товаров Wildberries.\n\n"
        "Пришли мне <b>артикул</b> товара (или ссылку на него), "
        "и я верну название, цену, рейтинг и количество отзывов.\n\n"
        "Например: <code>179323396</code>",
        parse_mode=ParseMode.HTML,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "ℹ️ Просто отправь артикул товара с Wildberries — "
        "число из адресной строки товара или ссылку целиком.\n\n"
        "Команды:\n"
        "/start — начать\n"
        "/help — помощь",
        parse_mode=ParseMode.HTML,
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text or ""
    article = parse_article(text)

    if article is None:
        await update.message.reply_text(
            "Не вижу артикул 🤔 Пришли число (например, 179323396) "
            "или ссылку на товар Wildberries."
        )
        return

    status = await update.message.reply_text("🔎 Ищу товар…")

    try:
        product = await fetch_product(article)
    except ProductNotFoundError:
        await status.edit_text(
            f"❌ Товар с артикулом {article} не найден. Проверь номер и попробуй снова."
        )
        return
    except WBBlockedError as exc:
        logger.warning("WB заблокировал запрос для %s: %s", article, exc)
        await status.edit_text(f"🚫 {exc}")
        return
    except WBApiError:
        logger.exception("Ошибка API WB для артикула %s", article)
        await status.edit_text(
            "⚠️ Wildberries сейчас недоступен. Попробуй ещё раз чуть позже."
        )
        return

    await status.edit_text(
        format_product(product),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit(
            "Не задан TELEGRAM_BOT_TOKEN. Создай файл .env на основе .env.example."
        )

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Бот запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
