"""Три согласия до работы с ботом: оферта, персональные данные, новости.

Экран показывается сам, когда человек обращается к боту без согласий, и
исчезает сам, когда оба обязательных даны. Своей команды у него нет
намеренно: команду надо знать, а согласие должно встретить человека первым.

Что остаётся открытым до согласий, решено владельцем: **карточка товара по
артикулу или ссылке**. Это бесплатный крючок, которым бота пробуют до всяких
отношений, и ставить перед ним юридическую стену значит терять людей на
пороге. Открыт и `/stop`: сказать «не пишите мне» человек должен мочь
всегда, независимо ни от чего.

Третья кнопка отделена от двух первых не ради красоты. Реклама по сетям
связи разрешена только с отдельно выраженного согласия (статья 18 закона
«О рекламе»), и одна кнопка «со всем согласен» обесценила бы его целиком:
человек не мог бы отказаться от рассылки, не отказавшись от услуги.
Поэтому кнопок три, отказ от третьей ничему не мешает, и бот говорит это
вслух.

Сторож стоит своей группой и останавливает цепочку: иначе сообщение дошло
бы до команды, а та успела бы начать работу до согласия. Он же показывает
экран, поэтому `/start` трогать не пришлось, и `bot/app.py` остался как был.
"""

from __future__ import annotations

import functools
import logging
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    TypeHandler,
)

from bot.texts import Safe, fill
from core import consent, db, leads

logger = logging.getLogger(__name__)

PREFIX = "con:"
ACCEPT_OFFER = f"{PREFIX}offer"
ACCEPT_PD = f"{PREFIX}pd"
NEWS_YES = f"{PREFIX}news_yes"
NEWS_NO = f"{PREFIX}news_no"

# Своя группа, раньше любой команды. Занятые группы перечислены в CLAUDE.md:
# -100 частота, -99 отметка обращения. Сторож согласий идёт следом за ними:
# частоту считать надо и до согласия, отметку обращения тоже, а вот работу
# начинать нельзя.
GUARD_GROUP = -98

# Команды, которые работают без согласий. `/stop` тут потому, что отказ от
# рассылки не может зависеть ни от чего.
FREE_COMMANDS = ("/stop",)

ASK = (
    "👋 <b>Прежде чем начать</b>\n\n"
    "Бот читает данные вашего кабинета Wildberries и хранит их у себя, "
    "поэтому начинается всё с трёх вопросов. Два обязательных, третий по "
    "желанию.\n\n"
    "{offer_line}\n"
    "{pd_line}\n"
    "{news_line}\n\n"
    "Карточка товара по артикулу или ссылке работает и без этого: пришлите "
    "артикул, если хотите просто посмотреть."
)

READY = (
    "✅ <b>Готово, можно работать.</b>\n\n"
    "{offer_line}\n"
    "{pd_line}\n"
    "{news_line}\n\n"
    "С чего начать, покажет <code>/start</code>. Все команды списком - "
    "<code>/help</code>."
)

# Строки состояния. У каждой два вида: что ещё не дано и что уже дано с датой.
OFFER_WAIT = "📄 <b>Оферта.</b> Договор, по которому бот работает. {link}"
OFFER_DONE = "✅ Оферта принята {when}."

PD_WAIT = (
    "🔒 <b>Обработка персональных данных.</b> Разрешение хранить то, что вы "
    "оставите боту. {link}"
)
PD_DONE = "✅ Согласие на обработку данных дано {when}."

# Самая длинная строка экрана, и это не случайность. Рекламное согласие
# считается данным только тогда, когда человек понимал, на что соглашается:
# что именно придёт, сколько раз, как часто и как отказаться. Поэтому числа
# названы прямо здесь, а не спрятаны в документе, который не читают. Берутся
# они из конфига, из той же секции, которой держится сама рассылка: напишешь
# их руками, и однажды обещание разойдётся с поведением.
NEWS_WAIT = (
    "✉️ <b>Новости и специальные предложения.</b> По желанию: отказ ничему "
    "не мешает, бот работает одинаково.\n"
    "Пишу только о сервисе и не чаще одного раза в {days}. Если доступ вы так "
    "и не оформите, напоминаний будет не больше {letters}, дальше они "
    "прекращаются сами. Отказаться можно когда угодно командой "
    "<code>/stop</code>. Правила рассылки записаны в политике "
    "конфиденциальности. {link}"
)
NEWS_YES_DONE = "✅ Новости и предложения: присылать. Передумаете - <code>/stop</code>."
NEWS_NO_DONE = "➖ Новости и предложения: не присылать."

# Ссылка на документ. Пустая переменная это не повод молчать или выдумать
# адрес: человек должен видеть, что документ есть, но пока не опубликован.
LINK = '<a href="{link}">Прочитать</a>.'
LINK_PENDING = "Текст готовится, владелец бота об этом знает."

BLOCKED = (
    "Эта команда работает после согласий. Они выше, это два нажатия."
)

NEWS_LATER = (
    "Про новости решите когда угодно, боту это не мешает. Сейчас можно "
    "работать: <code>/start</code>."
)


def _link(kind: str) -> Safe:
    address = consent.document_of(kind)
    return fill(LINK, link=address) if address else Safe(LINK_PENDING)


def _days_word(days: int) -> str:
    """«30 дней», «3 дня», «1 день». Читатель это селлер, а не программист."""
    days = abs(int(days))
    if 11 <= days % 100 <= 14:
        return f"{days} дней"
    last = days % 10
    if last == 1:
        return f"{days} день"
    if last in (2, 3, 4):
        return f"{days} дня"
    return f"{days} дней"


def _letters_word(count: int) -> str:
    """«не больше 3 писем», «не больше 1 письма»: форма после слова «больше».

    Склонение здесь одно и под одну фразу: отдельный падеж на все случаи
    жизни завёл бы в хендлере маленький словарь русского языка.
    """
    count = abs(int(count))
    if count % 10 == 1 and count % 100 != 11:
        return f"{count} письма"
    return f"{count} писем"


def _news_line() -> Safe:
    """Что обещано рассылкой. Числа из конфига, оттуда же их берёт сама рассылка."""
    return fill(
        NEWS_WAIT,
        letters=_letters_word(leads.max_reminders()),
        days=_days_word(leads.gap_days()),
        link=_link(consent.PERSONAL_DATA),
    )


def _when(record: Any) -> str:
    at = getattr(record, "at", None)
    return at.strftime("%d.%m.%Y") if at is not None else "ранее"


def screen(client_id: int, *, path: str | Path | None = None) -> tuple[Safe, Any]:
    """Текст экрана и клавиатура под него. Зависят только от состояния."""
    offer = consent.given(client_id, consent.OFFER, path=path)
    pd = consent.given(client_id, consent.PERSONAL_DATA, path=path)
    news = consent.given(client_id, consent.NEWS, path=path)
    asked_news = consent.answered_news(client_id, path=path)

    if offer is None:
        offer_line = fill(OFFER_WAIT, link=_link(consent.OFFER))
    else:
        offer_line = fill(OFFER_DONE, when=_when(offer))
    if pd is None:
        pd_line = fill(PD_WAIT, link=_link(consent.PERSONAL_DATA))
    else:
        pd_line = fill(PD_DONE, when=_when(pd))
    if news is not None:
        news_line = Safe(NEWS_YES_DONE)
    elif asked_news:
        news_line = Safe(NEWS_NO_DONE)
    else:
        news_line = _news_line()

    template = READY if consent.ready(client_id, path=path) else ASK
    text = fill(
        template, offer_line=offer_line, pd_line=pd_line, news_line=news_line
    )
    return text, keyboard(offer is None, pd is None, not asked_news)


def keyboard(need_offer: bool, need_pd: bool, need_news: bool) -> Any:
    """Кнопки только на то, что ещё не решено. Нажатую кнопку убираем."""
    rows = []
    if need_offer:
        rows.append([InlineKeyboardButton("Принимаю оферту", callback_data=ACCEPT_OFFER)])
    if need_pd:
        rows.append(
            [InlineKeyboardButton("Согласен на обработку данных", callback_data=ACCEPT_PD)]
        )
    if need_news:
        rows.append(
            [
                InlineKeyboardButton("Присылать новости", callback_data=NEWS_YES),
                InlineKeyboardButton("Не присылать", callback_data=NEWS_NO),
            ]
        )
    return InlineKeyboardMarkup(rows) if rows else None


def _client_id(update: Update, path) -> int | None:
    user = getattr(update, "effective_user", None)
    if user is None:
        return None
    return db.admin_repo(path).ensure_client(user.id)


def _needs_consent(update: Update) -> bool:
    """Требует ли это обращение согласий.

    Свободны: обычный текст (артикул или ссылка на товар), `/stop` и кнопки
    самого экрана согласий. Всё остальное, включая присланные файлы и кнопки
    других экранов, ждёт согласий.
    """
    query = getattr(update, "callback_query", None)
    if query is not None:
        return not str(getattr(query, "data", "") or "").startswith(PREFIX)
    message = getattr(update, "effective_message", None)
    if message is None:
        return False
    if getattr(message, "document", None) is not None:
        return True
    text = str(getattr(message, "text", "") or "").strip()
    if not text.startswith("/"):
        # Артикул, ссылка на товар или просто слова: карточка открыта всем.
        return False
    return text.split()[0].split("@")[0].lower() not in FREE_COMMANDS


async def guard(update: Update, context: Any, *, path=None) -> None:
    """Пускает дальше только того, кто дал оба обязательных согласия.

    Сбой самого сторожа не должен запирать бота наглухо: если состояние не
    прочиталось, человек идёт дальше, а владелец видит запись в журнале.
    Запертый бот хуже, чем пропущенный экран: согласие всё равно спросится
    при следующем обращении.
    """
    if not _needs_consent(update):
        return
    try:
        client_id = _client_id(update, path)
        if client_id is None or consent.ready(client_id, path=path):
            return
        text, markup = screen(client_id, path=path)
        message = getattr(update, "effective_message", None)
        if message is not None:
            await message.reply_text(
                text, parse_mode=ParseMode.HTML, reply_markup=markup,
                disable_web_page_preview=True,
            )
    except ApplicationHandlerStop:
        raise
    except Exception:  # noqa: BLE001 - запертый бот хуже пропущенного экрана
        logger.exception("сторож согласий не отработал")
        return
    raise ApplicationHandlerStop


async def accept_callback(
    update: Update, context: Any, *, kind: str, path=None
) -> None:
    """Кнопка согласия. Перерисовывает экран в том же сообщении."""
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    client_id = _client_id(update, path)
    if client_id is None:
        return
    consent.accept(client_id, kind, path=path)
    await _redraw(query, client_id, path)


async def news_callback(update: Update, context: Any, *, permit: bool, path=None) -> None:
    """Кнопки про новости. Отказ это тоже ответ, и он записывается с датой."""
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    client_id = _client_id(update, path)
    if client_id is None:
        return
    if permit:
        consent.accept(client_id, consent.NEWS, path=path)
    else:
        consent.refuse_news(client_id, path=path)
    await _redraw(query, client_id, path)


async def _redraw(query: Any, client_id: int, path) -> None:
    text, markup = screen(client_id, path=path)
    try:
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=markup,
            disable_web_page_preview=True,
        )
    except Exception:  # noqa: BLE001 - не перерисовалось, значит текст тот же
        logger.debug("экран согласий не перерисовался", exc_info=True)


def register(app, *, path: str | Path | None = None) -> None:
    """Сам себя регистрирует: bot/app.py никто не трогает."""
    app.add_handler(
        TypeHandler(Update, functools.partial(guard, path=path)), group=GUARD_GROUP
    )
    app.add_handler(
        CallbackQueryHandler(
            functools.partial(accept_callback, kind=consent.OFFER, path=path),
            pattern=f"^{ACCEPT_OFFER}$",
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            functools.partial(accept_callback, kind=consent.PERSONAL_DATA, path=path),
            pattern=f"^{ACCEPT_PD}$",
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            functools.partial(news_callback, permit=True, path=path),
            pattern=f"^{NEWS_YES}$",
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            functools.partial(news_callback, permit=False, path=path),
            pattern=f"^{NEWS_NO}$",
        )
    )
