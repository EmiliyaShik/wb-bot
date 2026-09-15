"""Обработчик артикула: старое поведение бота, перенесённое как есть.

Работает для всех, включая тех, кто не подключал кабинет. Логика не менялась:
из текста достаётся артикул, по нему собирается карточка с витрины.
"""

import logging
import re

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, MessageHandler, filters

from bot import texts
from legacy.card import (
    Product,
    ProductNotFoundError,
    WBApiError,
    WBBlockedError,
    fetch_product,
)

logger = logging.getLogger(__name__)

# Артикул WB это набор из 4-12 цифр. Достаём его в том числе из ссылки на товар.
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


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Любое текстовое сообщение: артикул отдаём карточкой, остальное объясняем."""
    text = update.message.text or ""
    article = parse_article(text)

    if article is None:
        await update.message.reply_text(
            texts.INTRO,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    status = await update.message.reply_text("🔎 Ищу товар...")

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


def register(app) -> None:
    """Ставится последним: это запасной обработчик для любого текста."""
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
