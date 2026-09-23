"""Обработчик артикула: старое поведение бота, перенесённое как есть.

Работает для всех, включая тех, кто не подключал кабинет. Логика не менялась:
из текста достаётся артикул, по нему собирается карточка с витрины.

Название, бренд, описание и отзывы приходят с витрины Wildberries, и
распоряжается ими продавец карточки, а не мы. Своего экранирования здесь
больше нет: подстановка общая, `bot.texts.fill`, и граница стоит на ней, а
не у каждого поля. Копия инструмента однажды разошлась бы с оригиналом
молча, и дыра вернулась бы в самую посещаемую команду бота.
"""

import logging
import re

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, MessageHandler, filters

from bot import texts
from bot.texts import Safe, fill
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


def format_product(product: Product) -> Safe:
    """Форматирует карточку товара для отправки в Telegram.

    Разметку ставим только мы. Всё, что пришло от Wildberries, уезжает в
    сообщение полем подстановки и становится текстом: без этого продавец
    подставил бы в описание свою ссылку, и она ушла бы клиенту от имени бота,
    а одиночный «<» в названии не дал бы отправить карточку вообще.
    """
    lines = [fill("<b>{name}</b>", name=product.name)]

    if product.brand:
        lines.append(fill("🏷 Бренд: {brand}", brand=product.brand))
    if product.supplier:
        lines.append(fill("🏪 Продавец: {supplier}", supplier=product.supplier))

    if product.price is not None:
        price = f"{product.price:,.0f}".replace(",", " ")
        price_line = fill("💰 Цена: <b>{price} ₽</b>", price=price)
        if product.old_price:
            old = f"{product.old_price:,.0f}".replace(",", " ")
            price_line = Safe(price_line + fill(" <s>{old} ₽</s>", old=old))
        lines.append(price_line)
    else:
        lines.append("💰 Цена: нет данных (возможно, нет в наличии)")

    if product.rating:
        stars = "⭐" * round(product.rating)
        lines.append(
            fill("{stars} Рейтинг: {rating}", stars=stars, rating=product.rating)
        )
    else:
        lines.append("⭐ Рейтинг: пока нет оценок")

    lines.append(fill("💬 Отзывов: {count}", count=product.feedbacks))
    lines.append(fill("🔢 Артикул: <code>{article}</code>", article=product.article))

    if product.characteristics:
        lines.append("\n<b>📋 Характеристики</b>")
        for name, value in product.characteristics[:6]:
            lines.append(fill("• {name}: {value}", name=name, value=value))

    if product.description:
        desc = product.description
        if len(desc) > 400:
            desc = desc[:400].rstrip() + "…"
        lines.append(fill("\n<b>📝 Описание</b>\n{desc}", desc=desc))

    if product.reviews:
        lines.append("\n<b>🗣 Отзывы</b>")
        for review in product.reviews:
            stars = f"{review.rating}⭐ " if review.rating else ""
            text = review.text
            if len(text) > 200:
                text = text[:200].rstrip() + "…"
            lines.append(fill("• {stars}{text}", stars=stars, text=text))

    # Ссылку на витрину бот собирает сам из артикула, и она остаётся живой
    # разметкой: адрес всё равно идёт полем, а не мимо подстановки.
    lines.append(fill('\n<a href="{url}">Открыть на Wildberries</a>', url=product.url))

    return Safe("\n".join(lines))


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Любое текстовое сообщение: артикул отдаём карточкой, остальное объясняем."""
    message = update.effective_message
    if message is None:  # правка старого сообщения приходит без message
        return
    text = message.text or ""
    article = parse_article(text)

    if article is None:
        await message.reply_text(
            texts.INTRO,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    status = await message.reply_text("🔎 Ищу товар...")

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
