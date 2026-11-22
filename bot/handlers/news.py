"""Рассылка новостей и специальных предложений. Команда владельца.

Отправить сообщение сразу всем это действие, которое нельзя отменить:
письмо уже у людей. Поэтому команда устроена в три шага, и средний из них
главный. Владелец пишет текст, бот показывает **ровно то сообщение**,
которое уйдёт, и называет число получателей, и только после нажатия кнопки
рассылка начинается. Кнопка «Отправить» это единственная дверь: ни одна
ветка этого файла не шлёт ничего клиентам до неё.

Кому уходит, решает не этот файл, а `core.leads.news_due`, и там же лежат
обещания из политики конфиденциальности: только с согласия, не чаще одного
раза в 30 дней, отказ командой `/stop` действует сразу. Пауза общая с
напоминаниями: человек не должен получить напоминание и новость в одну
неделю, причём оба раза «по правилам».

Сама отправка идёт задачей очереди, а не в хендлере. Тысяча сообщений это
минуты работы, а хендлер обязан ответить сразу; к тому же Telegram не
принимает больше трёх десятков сообщений в секунду, и паузу между ними
кто-то должен держать.

Заблокировавший бота считается отказавшимся: `Forbidden` это и есть ответ
«не пишите мне», и спрашивать его ещё раз незачем.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import Forbidden
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from bot.texts import MESSAGE_LIMIT, Safe, fill
from core import audit, config, leads, queue

logger = logging.getLogger(__name__)

PREFIX = "news:"
SEND = f"{PREFIX}send"
CANCEL = f"{PREFIX}cancel"

TASK_KIND = "news_broadcast"

# Свой текстовый шаг и своя группа. Занятые перечислены в CLAUDE.md: у счёта
# -10, у дат периода -9. Разводить их обязательно: в одной группе телеграм
# отдаёт сообщение только первому подходящему хендлеру, и второй не получил
# бы его никогда.
DIALOG_GROUP = -8

# Ключ черновика в памяти разговора. Переживать перезапуск ему незачем:
# ненаписанная новость не потеря, а написанная уже ушла.
DRAFT = "news_draft"

ASK_TEXT = (
    "📣 <b>Рассылка новостей</b>\n\n"
    "Пришлите следующим сообщением текст, который уйдёт людям. Можно "
    "обычным форматированием Telegram: жирный, курсив, ссылки.\n\n"
    "Отправится он не сразу: сначала покажу, как он будет выглядеть, и "
    "сколько человек его получат."
)

NOBODY = (
    "Отправлять некому: ни у кого нет согласия на новости, либо у всех ещё "
    "идёт пауза в {days}.\n\n"
    "Согласие спрашивается на экране согласий, пауза общая с напоминаниями: "
    "человек не должен получить два письма подряд."
)

PREVIEW = (
    "👆 Вот так увидят люди.\n\n"
    "Получателей: <b>{count}</b>. Это те, у кого есть согласие на новости и "
    "у кого прошла пауза в {days}.\n\n"
    "Отправка идёт в фоне, отменить её после начала нельзя."
)

EMPTY = "Пустой текст рассылать нечего. Пришлите сообщение или наберите команду заново."

# Длиннее предела Telegram сообщение не примет вовсе. Резать новость по
# живому нельзя: у отчёта остальное лежит в книге, а у новости остального
# нет, и человек получит оборванную фразу.
TOO_LONG = (
    "Текст длиннее, чем Telegram принимает в одном сообщении: {size} знаков "
    "против {limit}. Сократите на {extra} и пришлите заново."
)

WHO = (
    "📣 Сейчас новость получили бы <b>{count}</b> человек.\n"
    "Это те, у кого есть согласие на новости и у кого прошла пауза в {days}."
)

CANCELLED = "Рассылка отменена, никому ничего не ушло."

STARTED = "Отправляю. Напишу, когда закончу."

DONE = (
    "📣 Рассылка закончена.\n"
    "Доставлено: <b>{sent}</b> из {total}.\n"
    "Заблокировали бота: {blocked}, не доставлено по другой причине: {failed}."
)

# Сколько сообщений в секунду. Число Telegram, а не наша настройка: больше
# трёх десятков в секунду он не принимает и начинает отвечать отказом. Берём
# с запасом, рассылка не срочная.
PER_SECOND = 20


def _owner(update: Update) -> int | None:
    """Telegram ID владельца или None. Для всех остальных команды не существует."""
    user = getattr(update, "effective_user", None)
    telegram_id = getattr(user, "id", None)
    return int(telegram_id) if config.is_admin(telegram_id) else None


def _days() -> str:
    """Пауза словами. Число из конфига, оттуда же его берёт сама рассылка."""
    days = leads.gap_days()
    if 11 <= days % 100 <= 14:
        return f"{days} дней"
    last = days % 10
    if last == 1:
        return f"{days} день"
    if last in (2, 3, 4):
        return f"{days} дня"
    return f"{days} дней"


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Отправить", callback_data=SEND)],
            [InlineKeyboardButton("Отмена", callback_data=CANCEL)],
        ]
    )


async def _show_preview(message: Any, text: str, context: Any, path) -> None:
    """Показывает письмо как есть и спрашивает подтверждение."""
    if len(text) > MESSAGE_LIMIT:
        await message.reply_text(
            fill(
                TOO_LONG,
                size=len(text),
                limit=MESSAGE_LIMIT,
                extra=len(text) - MESSAGE_LIMIT,
            ),
            parse_mode=ParseMode.HTML,
        )
        return
    people = leads.news_due(path=path)
    if not people:
        await message.reply_text(
            fill(NOBODY, days=_days()), parse_mode=ParseMode.HTML
        )
        return
    context.user_data[DRAFT] = text
    # Сначала само письмо, ровно тем же способом, каким оно уйдёт людям:
    # пересказ своими словами показал бы не то, что получит человек.
    await message.reply_text(text, parse_mode=ParseMode.HTML)
    await message.reply_text(
        fill(PREVIEW, count=len(people), days=_days()),
        parse_mode=ParseMode.HTML,
        reply_markup=confirm_keyboard(),
    )


async def news_command(update: Update, context: Any, *, path=None) -> None:
    """`/news`: начать рассылку. С текстом в той же строке или без него."""
    if _owner(update) is None:
        return
    message = getattr(update, "effective_message", None)
    if message is None:
        return
    context.user_data[DRAFT] = None
    context.user_data["news_waiting"] = True
    await message.reply_text(Safe(ASK_TEXT), parse_mode=ParseMode.HTML)


async def who_command(update: Update, context: Any, *, path=None) -> None:
    """`/newswho`: сколько человек получат новость прямо сейчас.

    Нужна до того, как писать текст: рассылка на двух человек и рассылка на
    двести это разные решения, и узнавать это после написания письма поздно.
    """
    if _owner(update) is None:
        return
    message = getattr(update, "effective_message", None)
    if message is None:
        return
    await message.reply_text(
        fill(WHO, count=len(leads.news_due(path=path)), days=_days()),
        parse_mode=ParseMode.HTML,
    )


async def text_step(update: Update, context: Any, *, path=None) -> None:
    """Текст рассылки отдельным сообщением. Чужие сообщения не трогает."""
    if _owner(update) is None or not context.user_data.get("news_waiting"):
        return
    message = getattr(update, "effective_message", None)
    if message is None:
        return
    # text_html, а не text: так сохраняется форматирование, которое владелец
    # набрал в Telegram, и заодно экранируются угловые скобки. Своей разметки
    # мы сюда не добавляем ни одной.
    text = str(getattr(message, "text_html", None) or getattr(message, "text", "") or "").strip()
    context.user_data["news_waiting"] = False
    if not text:
        await message.reply_text(Safe(EMPTY), parse_mode=ParseMode.HTML)
        raise ApplicationHandlerStop
    await _show_preview(message, text, context, path)
    # Цепочка останавливается только в своём диалоге, иначе артикул товара
    # перестал бы доходить до карточки.
    raise ApplicationHandlerStop


async def send_callback(update: Update, context: Any, *, path=None) -> None:
    """Кнопка «Отправить». Единственная дверь к настоящей отправке."""
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    if _owner(update) is None:
        return
    text = context.user_data.get(DRAFT)
    if not text:
        await query.edit_message_text(Safe(EMPTY), parse_mode=ParseMode.HTML)
        return
    context.user_data[DRAFT] = None
    queue.enqueue(None, TASK_KIND, {"text": text}, notify=False, path=path)
    audit.log("news", None, "Владелец запустил рассылку новостей.", path=path)
    await query.edit_message_text(Safe(STARTED), parse_mode=ParseMode.HTML)


async def cancel_callback(update: Update, context: Any, *, path=None) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    if _owner(update) is None:
        return
    context.user_data[DRAFT] = None
    context.user_data["news_waiting"] = False
    await query.edit_message_text(Safe(CANCELLED), parse_mode=ParseMode.HTML)


def make_broadcast(app: Any, path: str | Path | None = None):
    """Сама рассылка. Идёт задачей очереди: это минуты работы, а не секунды."""

    async def broadcast(task: Any = None) -> int:
        payload = getattr(task, "payload", None) or {}
        text = str(payload.get("text") or "")
        if not text:
            return 0
        people = leads.news_due(path=path)
        sent = blocked = failed = 0
        pause = 1 / PER_SECOND if PER_SECOND > 0 else 0
        for person in people:
            # Отметка ставится до отправки, как и у напоминаний: если запись
            # однажды не пройдёт, а отправка пройдёт, человек получит
            # следующую новость раньше обещанной паузы.
            leads.note_mailing(person.client_id, path=path)
            try:
                await app.bot.send_message(
                    chat_id=person.telegram_id,
                    text=Safe(text),
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
                sent += 1
            except Forbidden:
                # Заблокировал бота: это тот же отказ, и спрашивать второй
                # раз незачем. Счётчик обнуляется той же дверью, что и /stop.
                leads.set_choice(person.client_id, False, path=path)
                blocked += 1
            except Exception:  # noqa: BLE001 - один адресат не ломает рассылку
                logger.exception("новость не ушла клиенту %s", person.client_id)
                failed += 1
            if pause:
                await asyncio.sleep(pause)
        audit.log(
            "news",
            None,
            f"Рассылка закончена: доставлено {sent} из {len(people)}, "
            f"заблокировали {blocked}, сбоев {failed}.",
            path=path,
        )
        await _tell_owner(app, sent, len(people), blocked, failed)
        return sent

    return broadcast


async def _tell_owner(app: Any, sent: int, total: int, blocked: int, failed: int) -> None:
    words = fill(DONE, sent=sent, total=total, blocked=blocked, failed=failed)
    for owner_id in config.admin_ids():
        try:
            await app.bot.send_message(
                chat_id=owner_id, text=words, parse_mode=ParseMode.HTML
            )
        except Exception:  # noqa: BLE001 - итог не важнее самой рассылки
            logger.exception("итог рассылки не ушёл владельцу %s", owner_id)


def register(app, *, path: str | Path | None = None) -> None:
    """Сам себя регистрирует: bot/app.py никто не трогает."""
    app.add_handler(CommandHandler("news", functools.partial(news_command, path=path)))
    app.add_handler(
        CommandHandler("newswho", functools.partial(who_command, path=path))
    )
    app.add_handler(
        CallbackQueryHandler(
            functools.partial(send_callback, path=path), pattern=f"^{SEND}$"
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            functools.partial(cancel_callback, path=path), pattern=f"^{CANCEL}$"
        )
    )
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND, functools.partial(text_step, path=path)
        ),
        group=DIALOG_GROUP,
    )
    queue.register(TASK_KIND, make_broadcast(app, path), quiet=True)
