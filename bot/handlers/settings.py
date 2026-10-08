"""Настройки рассылок и сообщения жизненного цикла.

Здесь две вещи. Первая: команда `/settings` с тумблерами ежедневной и
еженедельной рассылки и временем утреннего отчёта. Вторая: тексты всего,
что бот говорит сам, без просьбы клиента - предложение продлить, льготный
период, выключение модуля, предупреждение об удалении данных.

Решения принимает core.lifecycle, тут только слова и кнопки. Кнопка
«Выставить счёт» ведёт в диалог покупки таска 07, своего диалога здесь нет.

Целевой ДРР тоже здесь: это настройка селлера, а не число в коде. Хранит и
проверяет её `agents.ads`, экран только показывает и переключает. Кнопки
стоят и у того, кто модуль «Реклама» не покупал: настройка бесплатная, а
узнать про неё человеку иначе неоткуда.

Режим налогообложения и ставка устроены так же: хранит и проверяет их
`core.tax`, экран показывает и переключает. Ставка выбирается селлером,
потому что в регионах она льготная, а не потому, что бот не знает общей.
Белого списка ставок здесь нет намеренно: он один и лежит в `core.tax`,
иначе кнопка однажды предложила бы ставку, которую расчёт не принимает.

Разметку в сообщениях ставит только бот. Название модуля приходит из
конфига, а все эти сообщения уходят с `ParseMode.HTML`, поэтому подстановка
идёт через общий `bot.texts.fill`: граница стоит на ней, а не у каждого
поля.
"""

from __future__ import annotations

import functools
import logging
from datetime import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from agents import ads as ads_agent
from agents import lifecycle
from bot.handlers import costs as costs_handler
from bot.handlers import tariffs
from bot.texts import Safe, fill
from core import config, db, scheduler
from core import tax as tax_module

logger = logging.getLogger(__name__)

PREFIX = "set:"
TIME_PREFIX = f"{PREFIX}at:"
DRR_PREFIX = f"{PREFIX}drr:"
TAX_MODE_PREFIX = f"{PREFIX}tax:"
TAX_RATE_PREFIX = f"{PREFIX}rate:"
TAX_OFF_TOKEN = f"{PREFIX}taxoff"

# Часы на выбор. Утро и только утро: отчёт нужен до того, как начнётся день.
TIMES = ("07:00", "08:00", "09:00", "10:00", "11:00", "12:00")

# Целевой ДРР на выбор, в процентах. Кнопки, а не ввод числа: диалог с
# текстовым шагом в проекте уже занят счётом, а вторая такая же дорожка
# перехватывала бы у него сообщения. Шаг в пять процентов: разницу между
# 17 и 18 процентами цели никто не заметит, а лишние кнопки заметят все.
DRR_CHOICES = (5, 10, 15, 20, 25, 30)

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


DRR_BLOCK = (
    "🎯 <b>Целевой ДРР: {value}%</b>\n"
    "Это ваша граница: сколько вы готовы отдавать рекламе с каждых ста рублей "
    "выручки. По ней отчёт <code>/ads</code> отмечает кампании, которые "
    "тратят больше."
)


TAX_OFF_BLOCK = (
    "🧾 <b>Налог: режим не выбран</b>\n"
    "Пока режим не выбран, прибыль в <code>/profit</code> показана до налога. "
    "Это не ноль, а «не посчитано»: на УСН «доходы» налог это ещё несколько "
    "процентов от выручки, и они уходят. Ставку выбираете вы: в регионах она "
    "льготная."
)

TAX_BLOCK = (
    "🧾 <b>Налог: {mode}, ставка {rate}%</b>\n"
    "В отчёте <code>/profit</code> бот покажет оценку налога отдельной строкой "
    "и вычтет её из прибыли. Это оценка, а не налог к уплате: бухгалтера бот "
    "не заменяет."
)

# Почему это вообще стоит в настройках отдельной строкой: селлер видит
# поступление от Wildberries и считает процент с него, а налоговая считает с
# полной цены продажи. Сказать об этом надо там, где человек выбирает режим,
# а не только в отчёте.
TAX_INCOME_NOTE = (
    "\nБаза считается с того, что заплатил покупатель, а не с того, что "
    "перечислил Wildberries: удержанная комиссия и скидка площадки в неё "
    "входят. Разницу бот покажет в отчёте цифрой."
)

TAX_MINUS_NOTE = (
    "\nВ расходы бот берёт себестоимость проданного товара, удержания "
    "Wildberries и рекламу. Штрафы не берёт: такие расходы обычно не "
    "принимают. Взносы, зарплату и аренду бот не видит вовсе."
)

# Короткие подписи для кнопок. Полные названия режимов живут в core.tax и
# едут в текст, а на кнопке нужна строка, которая помещается на телефоне.
TAX_MODE_LABELS = {
    tax_module.USN_INCOME: "УСН доходы",
    tax_module.USN_INCOME_MINUS: "УСН доходы минус расходы",
}

TAX_OFF_LABEL = "Не считать налог"


def _mark(flag: bool) -> str:
    return "✅" if flag else "⬜"


def tax_text(rule: Any) -> Safe:
    """Строка про налог. Режим и ставка приходят из настроек, а не из кода."""
    if not rule.known:
        return Safe(TAX_OFF_BLOCK)
    text = fill(TAX_BLOCK, mode=rule.title, rate=rule.rate_text)
    return Safe(text + (TAX_MINUS_NOTE if rule.with_expenses else TAX_INCOME_NOTE))


def drr_text(target: Any) -> Safe:
    """Строка про целевой ДРР. Число приходит из настроек, а не из кода."""
    return fill(DRR_BLOCK, value=_drr_label(target))


def _drr_label(target: Any) -> str:
    """Цель числом, без хвостовых нулей: 15, а не 15.00."""
    text = f"{Decimal(str(target)):f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def settings_text(
    prefs: lifecycle.Prefs,
    state: costs_handler.Coverage | None = None,
    target: Any = None,
    rule: Any = None,
) -> Safe:
    """Что сейчас включено. Время показывается в часовом поясе расписания.

    Себестоимость это тоже состояние, и показывать его больше негде: прибыль
    по артикулам без неё не считается вовсе, а узнать об этом раньше пустого
    отчёта человеку было неоткуда. `state` необязателен, потому что бывают
    вызовы без базы под рукой, и `target` тоже.
    """
    body = fill(
        BODY,
        daily=ON if prefs.daily else OFF,
        at=prefs.daily_at_text,
        zone=scheduler.timezone_name(),
        weekly=ON if prefs.weekly else OFF,
    )
    blocks = [HEAD, body, EXPLAIN]
    if target is not None:
        blocks.append(drr_text(target))
    if rule is not None:
        blocks.append(tax_text(rule))
    if state is not None:
        blocks.append(costs_handler.state_text(state))
    return Safe("\n\n".join(blocks))


def keyboard(
    prefs: lifecycle.Prefs,
    state: costs_handler.Coverage | None = None,
    target: Any = None,
    rule: Any = None,
) -> InlineKeyboardMarkup:
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
    if target is not None:
        row = []
        current = _drr_label(target)
        for percent in DRR_CHOICES:
            mark = "🔹" if str(percent) == current else ""
            row.append(
                InlineKeyboardButton(
                    f"{mark}{percent}%".strip(), callback_data=f"{DRR_PREFIX}{percent}"
                )
            )
            if len(row) == 3:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
    if rule is not None:
        rows += _tax_rows(rule)
    if state is not None and not state.complete:
        # Кнопка ровно там, где сказано про нехватку: дорога к себестоимости
        # уже есть, показать её надо в том же сообщении, а не в памяти.
        rows += list(_costs_keyboard().inline_keyboard)
    return InlineKeyboardMarkup(rows)


def _tax_rows(rule: Any) -> list[list[InlineKeyboardButton]]:
    """Кнопки налога: режим, ставки выбранного режима и отказ считать.

    Ставки рисуются из того же белого списка, по которому их принимает
    `core.tax`: два списка разошлись бы молча, и кнопка предлагала бы ставку,
    которую расчёт не берёт.
    """
    rows = [
        [
            InlineKeyboardButton(
                f"{'🔹' if rule.mode == mode else ''}{TAX_MODE_LABELS[mode]}".strip(),
                callback_data=f"{TAX_MODE_PREFIX}{mode}",
            )
        ]
        for mode in tax_module.MODES
    ]
    if not rule.known:
        return rows
    row: list[InlineKeyboardButton] = []
    for percent in tax_module.RATE_CHOICES[rule.mode]:
        mark = "🔹" if str(percent) == rule.rate_text else ""
        # Слово на кнопке не для красоты: рядом стоит такой же ряд процентов
        # для целевого ДРР, и «15%» без подписи читается как он.
        row.append(
            InlineKeyboardButton(
                f"{mark}Налог {percent}%".strip(),
                callback_data=f"{TAX_RATE_PREFIX}{percent}",
            )
        )
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(TAX_OFF_LABEL, callback_data=TAX_OFF_TOKEN)])
    return rows


def _costs_keyboard() -> InlineKeyboardMarkup:
    """Кнопка «Внести себестоимость» из меню.

    Импорт ленивый: `bot/handlers/menu.py` импортирует этот файл, чтобы кнопка
    настроек попадала в ту же функцию, что и команда. Обратный импорт на
    верхнем уровне замкнул бы круг.
    """
    from bot.handlers import menu

    return menu.costs_keyboard()


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
    state = costs_handler.coverage(client_id, path=path)
    target = ads_agent.target_drr(client_id, path=path)
    rule = tax_module.rule_of(client_id, path=path)
    await message.reply_text(
        settings_text(prefs, state, target, rule),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard(prefs, state, target, rule),
    )


async def toggle_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, path: str | Path | None = None
) -> None:
    """Кнопки настроек: тумблеры, выбор времени и целевой ДРР."""
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
    elif data.startswith("drr:"):
        # `callback_data` приходит от клиента и подделывается свободно:
        # принимаем только те проценты, которые сами и нарисовали. Иначе цель
        # в ноль или в миллион приехала бы прямо в настройки кабинета.
        if data[4:] not in {str(value) for value in DRR_CHOICES}:
            logger.warning("не разобрать целевой ДРР из кнопки: %s", data)
            return
        ads_agent.set_target_drr(client_id, data[4:], path=path)
    elif data == "taxoff":
        tax_module.clear_rule(client_id, path=path)
    elif data.startswith("tax:") or data.startswith("rate:"):
        # Та же граница, что и у ДРР: `callback_data` подделывается свободно,
        # и проверять его нарисованными кнопками нельзя. Режим и ставку
        # принимает `core.tax`, там же лежит белый список, и чужой режим или
        # ставка в двести процентов до настроек кабинета не доходят.
        try:
            if data.startswith("tax:"):
                # Режим выбран впервые: ставка встаёт по умолчанию из конфига,
                # дальше селлер меняет её кнопками ставок.
                current = tax_module.rule_of(client_id, path=path)
                mode = data[4:]
                rate = current.rate if current.mode == mode else None
                tax_module.set_rule(client_id, mode, rate, path=path)
            else:
                mode = tax_module.rule_of(client_id, path=path).mode
                tax_module.set_rule(client_id, mode, data[5:], path=path)
        except ValueError:
            logger.warning("не разобрать налоговую настройку из кнопки: %s", data)
            return

    state = costs_handler.coverage(client_id, path=path)
    target = ads_agent.target_drr(client_id, path=path)
    rule = tax_module.rule_of(client_id, path=path)
    try:
        await query.edit_message_text(
            settings_text(prefs, state, target, rule),
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard(prefs, state, target, rule),
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
