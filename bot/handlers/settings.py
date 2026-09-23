"""Настройки рассылок и сообщения жизненного цикла.

Здесь две вещи. Первая: команда `/settings` с тумблерами ежедневной и
еженедельной рассылки и временем утреннего отчёта. Вторая: тексты всего,
что бот говорит сам, без просьбы клиента - предложение продлить, льготный
период, выключение модуля, предупреждение об удалении данных.

Решения принимает core.lifecycle, тут только слова и кнопки. Кнопка
«Выставить счёт» ведёт в диалог покупки таска 07, своего диалога здесь нет.

Целевого ДРР в настройках нет намеренно: по брифу он относится к модулю
ads, отложенному до этапа 3.

Разметку в сообщениях ставит только бот. Название модуля приходит из
конфига, а все эти сообщения уходят с `ParseMode.HTML`, поэтому подстановка
идёт через общий `bot.texts.fill`: граница стоит на ней, а не у каждого
поля.
"""

from __future__ import annotations

import functools
import logging
from datetime import time
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from agents import lifecycle
from bot.handlers import tariffs
from bot.texts import Safe, fill
from core import config, db, scheduler

logger = logging.getLogger(__name__)

PREFIX = "set:"
TIME_PREFIX = f"{PREFIX}at:"

# Часы на выбор. Утро и только утро: отчёт нужен до того, как начнётся день.
TIMES = ("07:00", "08:00", "09:00", "10:00", "11:00", "12:00")

HEAD = "⚙️ <b>Настройки рассылок</b>"

EXPLAIN = (
    "Выключенная рассылка значит только одно: бот не пишет вам сам. "
    "Данные он собирает по-прежнему, каждый день, и отчёт всегда можно "
    "запросить командой."
)

ON = "включена"
OFF = "выключена"

BODY = (
    "Ежедневный отчёт по плану-факту: <b>{daily}</b>\n"
    "Время: <b>{at}</b> ({zone})\n"
    "Недельный разбор финансов: <b>{weekly}</b>\n"
    "Недельный разбор уходит не по расписанию, а когда Wildberries "
    "выложит новый финансовый отчёт."
)


def _mark(flag: bool) -> str:
    return "✅" if flag else "⬜"


def settings_text(prefs: lifecycle.Prefs) -> Safe:
    """Что сейчас включено. Время показывается в часовом поясе расписания."""
    body = fill(
        BODY,
        daily=ON if prefs.daily else OFF,
        at=prefs.daily_at_text,
        zone=scheduler.timezone_name(),
        weekly=ON if prefs.weekly else OFF,
    )
    return Safe(HEAD + "\n\n" + body + "\n\n" + EXPLAIN)


def keyboard(prefs: lifecycle.Prefs) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                f"{_mark(prefs.daily)} Ежедневный отчёт",
                callback_data=f"{PREFIX}daily",
            )
        ],
        [
            InlineKeyboardButton(
                f"{_mark(prefs.weekly)} Недельный разбор",
                callback_data=f"{PREFIX}weekly",
            )
        ],
    ]
    row: list[InlineKeyboardButton] = []
    for value in TIMES:
        mark = "🔹" if value == prefs.daily_at_text else ""
        row.append(
            InlineKeyboardButton(f"{mark}{value}".strip(), callback_data=f"{TIME_PREFIX}{value}")
        )
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


# --- тексты жизненного цикла --------------------------------------------------


def _module_title(module: str) -> str:
    info = config.modules().get(module)
    return (info.title if info and info.title else module) or module


def _days_word(days: int) -> str:
    """«5 дней», «3 дня», «1 день». Русский счёт, без него текст выглядит машинным."""
    days = abs(int(days))
    if 11 <= days % 100 <= 14:
        return f"{days} дней"
    last = days % 10
    if last == 1:
        return f"{days} день"
    if last in (2, 3, 4):
        return f"{days} дня"
    return f"{days} дней"


def _weeks_word(weeks: int) -> str:
    """«4 недели», «1 неделя», «5 недель». Счёт тот же, что и у дней."""
    weeks = abs(int(weeks))
    if 11 <= weeks % 100 <= 14:
        return f"{weeks} недель"
    last = weeks % 10
    if last == 1:
        return f"{weeks} неделя"
    if last in (2, 3, 4):
        return f"{weeks} недели"
    return f"{weeks} недель"


BACKFILL = (
    "📥 Модуль «{title}» работает. Бот подтягивает вашу "
    "историю за {weeks}: без неё не с чем сравнивать, "
    "и он не увидит, что расход вырос.\n\n"
    "Wildberries отдаёт такие отчёты медленно, поэтому это займёт время, "
    "иногда несколько часов. Ничего делать не нужно: первые отчёты и "
    "предупреждения о выросших расходах придут сами, как только история "
    "соберётся."
)

RENEWAL = (
    "⏳ Доступ к модулю «{title}» заканчивается через {days}.\n\n"
    "Чтобы ничего не прерывалось, продлите его заранее: нажмите кнопку ниже, "
    "и бот выставит счёт. Оплата по счёту для ИП и ООО, обычно доступ "
    "включается в день оплаты."
)

GRACE = (
    "⚠️ Срок доступа к модулю «{title}» вышел.\n\n"
    "Льготные дни уже идут: всё работает ещё {days}, "
    "отчёты приходят как обычно. Если продлить за это время, перерыва не будет "
    "вовсе."
)

SHUTDOWN = (
    "🔒 Модуль «{title}» выключен: льготные дни кончились.\n\n"
    "Данные никуда не делись. Они хранятся {kept} после выключения всех модулей, "
    "и как только вы продлите доступ, отчёты продолжатся с той же историей."
)

RETENTION = (
    "🗂 Через {days} бот удалит ваши данные: историю продаж, "
    "себестоимость, планы и отчёты.\n\n"
    "Так устроено хранение: данные живут {kept} после того, как выключился "
    "последний модуль.\n\n"
    "Чтобы всё сохранилось, включите любой модуль до этой даты. Ничего "
    "переносить и восстанавливать не придётся, история останется на месте."
)

DELETED = (
    "🗑 Срок хранения вышел, и данные удалены: история продаж, себестоимость, "
    "планы и отчёты.\n\n"
    "Восстановить их нельзя, но начать заново можно в любой момент: "
    "подключите кабинет командой <code>/connect</code>, и бот снова начнёт "
    "копить историю."
)


def backfill_text(event: lifecycle.Event) -> Safe:
    return fill(
        BACKFILL,
        title=_module_title(event.module),
        weeks=_weeks_word(event.days_left),
    )


def renewal_text(event: lifecycle.Event) -> Safe:
    return fill(
        RENEWAL,
        title=_module_title(event.module),
        days=_days_word(event.days_left),
    )


def grace_text(event: lifecycle.Event) -> Safe:
    return fill(
        GRACE,
        title=_module_title(event.module),
        days=_days_word(event.days_left),
    )


def shutdown_text(event: lifecycle.Event) -> Safe:
    return fill(
        SHUTDOWN,
        title=_module_title(event.module),
        kept=_days_word(lifecycle.retention_days()),
    )


def retention_text(event: lifecycle.Event) -> Safe:
    return fill(
        RETENTION,
        days=_days_word(event.days_left),
        kept=_days_word(lifecycle.retention_days()),
    )


def deleted_text(event: lifecycle.Event) -> Safe:
    return Safe(DELETED)


def notice(event: lifecycle.Event) -> tuple[Safe, InlineKeyboardMarkup | None]:
    """Текст и кнопки одного события. Кнопки ведут в уже готовые диалоги."""
    if event.kind == lifecycle.RENEWAL:
        return renewal_text(event), _invoice_keyboard(event.module)
    if event.kind == lifecycle.GRACE:
        return grace_text(event), _invoice_keyboard(event.module)
    if event.kind == lifecycle.SHUTDOWN:
        return shutdown_text(event), _invoice_keyboard(event.module)
    if event.kind == lifecycle.BACKFILL:
        return backfill_text(event), None
    if event.kind == lifecycle.RETENTION:
        return retention_text(event), _invoice_keyboard(tariffs.LIST_TOKEN)
    return deleted_text(event), None


def _invoice_keyboard(module: str) -> InlineKeyboardMarkup:
    """Кнопка в диалог покупки таска 07: имя модуля идёт в его callback."""
    label = "Выставить счёт" if module != tariffs.LIST_TOKEN else "Продлить доступ"
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=f"{tariffs.BUY_PREFIX}{module}")]]
    )


# --- команда ------------------------------------------------------------------


def _client_id(update: Update, path: str | Path | None) -> int | None:
    return tariffs.client_id_of(update, path)


async def settings_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, path: str | Path | None = None
) -> None:
    """Показывает тумблеры и время. Доступа для этого не нужно."""
    message = update.effective_message
    client_id = _client_id(update, path)
    if message is None or client_id is None:
        return
    prefs = lifecycle.prefs(client_id, path=path)
    await message.reply_text(
        settings_text(prefs), parse_mode=ParseMode.HTML, reply_markup=keyboard(prefs)
    )


async def toggle_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, path: str | Path | None = None
) -> None:
    """Кнопки настроек: тумблеры и выбор времени."""
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    client_id = _client_id(update, path)
    if client_id is None:
        return

    data = (query.data or "").removeprefix(PREFIX)
    prefs = lifecycle.prefs(client_id, path=path)
    if data == "daily":
        prefs = lifecycle.set_daily(client_id, not prefs.daily, path=path)
    elif data == "weekly":
        prefs = lifecycle.set_weekly(client_id, not prefs.weekly, path=path)
    elif data.startswith("at:"):
        try:
            prefs = lifecycle.set_daily_time(client_id, data[3:], path=path)
        except ValueError:
            logger.warning("не разобрать время из кнопки: %s", data)
            return

    try:
        await query.edit_message_text(
            settings_text(prefs), parse_mode=ParseMode.HTML, reply_markup=keyboard(prefs)
        )
    except Exception:  # noqa: BLE001 - Telegram ругается на неизменённый текст
        logger.debug("сообщение настроек не обновилось", exc_info=True)


# --- доставка предупреждений --------------------------------------------------


def make_notifier(app: Any, *, path: str | Path | None = None):
    """Чем жизненный цикл пишет клиенту. Решения не здесь, только слова."""

    async def send(client_id: int, event: lifecycle.Event) -> None:
        row = db.admin_repo(path).client(client_id)
        if row is None:
            logger.warning("некому написать: клиента %s нет в базе", client_id)
            return
        text, markup = notice(event)
        try:
            await app.bot.send_message(
                chat_id=int(row["telegram_id"]),
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
        except Exception:  # noqa: BLE001 - молчание Telegram не наша авария
            logger.exception("не удалось написать клиенту %s", client_id)

    return send


def register(app, *, path: str | Path | None = None) -> None:
    """Хендлер ставит себя сам: bot/app.py никто не трогает."""
    lifecycle.set_notifier(make_notifier(app, path=path))
    # Работы жизненного цикла поднимаются здесь, после агентов: утренняя
    # рассылка РНП перерегистрируется поверх агентской, чтобы знать про
    # тумблеры и время клиента.
    lifecycle.register_jobs(path=path)

    app.add_handler(
        CommandHandler("settings", functools.partial(settings_command, path=path))
    )
    app.add_handler(
        CallbackQueryHandler(
            functools.partial(toggle_callback, path=path), pattern=f"^{PREFIX}"
        )
    )
