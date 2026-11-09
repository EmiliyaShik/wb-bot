"""Напоминания тем, кто ничего не оформил, и команда `/stop`.

Кому бот пишет сам, решает не удобство, а закон. Клиенту он пишет по
договору: отчёт заказан, про окончание оплаченного периода сказано в оферте.
Человеку, который ничего не купил, писать можно только с его согласия: это
реклама, а реклама по сетям связи разрешена с предварительного согласия
(статья 18 закона «О рекламе»). Поэтому согласие спрашивается кнопкой, и
отказ работает сразу и навсегда.

Правила и состояние живут в `core.leads`, здесь слова, кнопки и две работы
расписания. Удаления своего здесь нет: оно одно на весь проект и лежит в
`agents.lifecycle.erase`, вместе с обезличенной записью в журнал.

Отдельный хендлер в группе `-99` отмечает день, когда человек обращался к
боту. Отметка нужна не для статистики, а для срока хранения: политика
обещает удалить данные того, кто ничего не оформил, через 180 дней **с
последнего обращения**, и без этой отметки срок считался бы от записи в
базе, то есть удалял бы тех, кто ботом пользуется. Группа своя и после
ограничителя частоты: поток сообщений он останавливает раньше, и писать в
базу на каждое из них незачем.

Разметку в сообщениях ставит только бот: шаблоны с числами заполняет общий
`bot.texts.fill`, остальное это собственные константы файла.
"""

from __future__ import annotations

import functools
import logging
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import Forbidden
from telegram.ext import CallbackQueryHandler, CommandHandler, TypeHandler

from agents import lifecycle
from bot.texts import Safe, fill
from core import db, leads, scheduler

logger = logging.getLogger(__name__)

PREFIX = "rem:"
PERMIT = f"{PREFIX}yes"
REFUSE = f"{PREFIX}no"

# Группа отметки последнего обращения. Занятые соседями: -100 ограничитель
# частоты, -10 диалог счёта, -9 ввод дат периода, 40 документы, 50 «Оформить».
SEEN_GROUP = -99

# Работы расписания, они же виды задач в очереди.
REMIND_JOB = "leads_remind"
CLEANUP_JOB = "leads_cleanup"

ASK = (
    "🔔 <b>Напоминать о сервисе?</b>\n"
    "Пока вы ничего не оформили, сам я вам не пишу: без согласия это была бы "
    "реклама. Если напоминание нужно, нажмите «Напоминать»: пришлю не больше "
    "трёх и не чаще раза в месяц. Отказаться можно в любой момент командой "
    "<code>/stop</code>."
)

PERMIT_SAVED = (
    "✅ Записал: напоминать можно. Не больше трёх напоминаний и не чаще раза "
    "в месяц. Передумаете - команда <code>/stop</code>, и я замолчу."
)

REFUSE_SAVED = (
    "Записал: не напоминать. Сам я вам больше не напишу.\n"
    "Отчёты по команде и ответы на ваши сообщения это не рассылка: они "
    "работают как раньше."
)

# Тому, у кого доступ уже был, этот отказ почти ничего не меняет, и честнее
# сказать это прямо, чем дать человеку думать, что он выключил отчёты.
STOP_CLIENT = (
    "Записал: напоминания о сервисе вам не нужны.\n\n"
    "Ваших отчётов это не касается: их бот присылает по договору, и "
    "выключаются они в другом месте. Ежедневный и недельный разбор - "
    "тумблеры в <code>/settings</code>. Удалить токен и все данные кабинета "
    "насовсем - <code>/disconnect</code>."
)

REMINDER = (
    "🔔 Вы заходили в бота и доступ не оформили.\n\n"
    "Без оплаты работают две вещи: карточка товара по артикулу или ссылке и "
    "бесплатный разбор недели <code>/diagnostic</code>, один раз на кабинет. "
    "Что входит в модули и сколько стоит - <code>/tariffs</code>.\n\n"
    "Это напоминание {number} из {total}. Чтобы не получать их вовсе - "
    "команда <code>/stop</code>."
)


def ask_keyboard() -> InlineKeyboardMarkup:
    """Две кнопки: согласие и отказ. Третьей («потом») нет намеренно.

    Молчание и есть «потом»: кто не нажал ничего, тому бот не пишет.
    """
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Напоминать", callback_data=PERMIT)],
            [InlineKeyboardButton("Не напоминать", callback_data=REFUSE)],
        ]
    )


def worth_asking(client_id: int, *, path: str | Path | None = None) -> bool:
    """Стоит ли показывать вопрос: доступа не было и мы ещё не спрашивали."""
    return leads.unpaid(client_id, path=path) and not leads.asked(
        client_id, path=path
    )


def _client_id(update: Update, path) -> int | None:
    user = getattr(update, "effective_user", None)
    if user is None:
        return None
    return db.admin_repo(path).ensure_client(user.id)


async def _answer(message: Any, text: Safe) -> None:
    if message is None:
        return
    await message.reply_text(text, parse_mode=ParseMode.HTML)


# --- команда /stop ------------------------------------------------------------


async def stop_command(update: Update, context: Any, *, path=None) -> None:
    """`/stop`: больше не напоминать. Работает у любого, и у клиента тоже."""
    message = getattr(update, "effective_message", None)
    client_id = _client_id(update, path)
    if message is None or client_id is None:
        return
    leads.set_choice(client_id, False, path=path)
    if leads.unpaid(client_id, path=path):
        await _answer(message, Safe(REFUSE_SAVED))
    else:
        await _answer(message, Safe(STOP_CLIENT))


# --- кнопки -------------------------------------------------------------------


async def choice_callback(
    update: Update, context: Any, *, permit: bool, path=None
) -> None:
    """Кнопки «Напоминать» и «Не напоминать». Решает кнопка, а не её текст."""
    query = getattr(update, "callback_query", None)
    if query is None:
        return
    await query.answer()
    client_id = _client_id(update, path)
    if client_id is None:
        return
    leads.set_choice(client_id, permit, path=path)
    await _answer(query.message, Safe(PERMIT_SAVED if permit else REFUSE_SAVED))


# --- отметка последнего обращения ---------------------------------------------


async def seen_guard(update: Update, context: Any, *, path=None) -> None:
    """Отмечает день обращения тем, кто в базе уже есть. Сбой бота не роняет.

    Строки здесь не создаются намеренно, хотя `ensure_client` под рукой.
    Отметка нужна для срока хранения, а срок хранения нужен тому, чьи данные
    у нас лежат; заводить запись о каждом, кто написал боту слово, значило бы
    собирать данные ради того, чтобы потом их удалить. Кому бот отвечает
    по-настоящему, запись и так заводит его команда.

    Срок хранения это обещание в политике, а не функция бота: лучше потерять
    одну отметку, чем ответ на команду.
    """
    try:
        user = getattr(update, "effective_user", None)
        if user is None:
            return
        row = db.admin_repo(path).client_by_telegram(user.id)
        if row is not None:
            leads.seen(int(row["id"]), path=path)
    except Exception:  # noqa: BLE001 - отметка не важнее разговора
        logger.exception("не удалось отметить обращение")


# --- работы расписания --------------------------------------------------------


def make_remind_job(app: Any, path: str | Path | None = None):
    """Утренняя рассылка напоминаний тем, кто сам разрешил."""

    async def remind(task: Any = None) -> int:
        day = leads.payload_date(task)
        total = leads.max_reminders()
        sent = 0
        for lead in leads.due(today=day, path=path):
            text = fill(REMINDER, number=lead.sent + 1, total=total)
            # Отметка ставится до отправки, а не после. Если запись отметки
            # однажды не пройдёт, а отправка пройдёт, человек попадёт в
            # очередь снова завтра, и послезавтра, и каждое утро: пауза
            # держится именно этой отметкой. Потерять одно напоминание из
            # трёх дешевле, чем писать человеку каждый день.
            leads.note_sent(lead.client_id, today=day, path=path)
            try:
                await app.bot.send_message(
                    chat_id=lead.telegram_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                )
            except Forbidden:
                # Бота заблокировали. Это тот же отказ, только сказанный
                # средствами Telegram, и записывается он так же: иначе бот
                # будет месяцами стучать в закрытую дверь.
                leads.set_choice(lead.client_id, False, today=day, path=path)
                logger.info(
                    "клиент %s заблокировал бота, напоминания выключены",
                    lead.client_id,
                )
            except Exception:  # noqa: BLE001 - один адресат не ломает рассылку
                logger.exception("не удалось напомнить клиенту %s", lead.client_id)
            else:
                sent += 1
        return sent

    return remind


def make_cleanup_job(path: str | Path | None = None):
    """Удаление данных тех, кто ничего не оформил и давно молчит.

    Предупреждения перед удалением тут нет намеренно. Клиенту о удалении
    говорится заранее ([access] retention_notice_days), а здесь половина
    людей как раз просила им не писать, и письмо «удаляю ваши данные» было бы
    ровно тем сообщением, которого они просили не присылать.
    """

    def cleanup(task: Any = None) -> int:
        day = leads.payload_date(task)
        removed = 0
        for lead in leads.stale(today=day, path=path):
            try:
                lifecycle.erase(lead.client_id, path=path)
                removed += 1
            except Exception:  # noqa: BLE001 - один клиент не ломает чистку
                logger.exception(
                    "не удалось удалить данные клиента %s", lead.client_id
                )
        if removed:
            logger.info("удалены данные %s человек без кабинета и без оплат", removed)
        return removed

    return cleanup


def register(app, *, path: str | Path | None = None) -> None:
    """Сам себя регистрирует: bot/app.py никто не трогает."""
    # TypeHandler, а не MessageHandler: обращением считается и команда, и
    # нажатие кнопки, и присланный файл.
    app.add_handler(
        TypeHandler(Update, functools.partial(seen_guard, path=path)),
        group=SEEN_GROUP,
    )
    app.add_handler(CommandHandler("stop", functools.partial(stop_command, path=path)))
    app.add_handler(
        CallbackQueryHandler(
            functools.partial(choice_callback, permit=True, path=path),
            pattern=f"^{PERMIT}$",
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            functools.partial(choice_callback, permit=False, path=path),
            pattern=f"^{REFUSE}$",
        )
    )
    scheduler.register_daily(REMIND_JOB, make_remind_job(app, path))
    scheduler.register_daily(CLEANUP_JOB, make_cleanup_job(path))
