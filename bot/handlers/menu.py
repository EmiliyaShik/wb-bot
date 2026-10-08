"""Меню команд Telegram и кнопки главных действий под `/start` и `/help`.

Две разные вещи, но обе про одно: селлер не должен помнить пятнадцать команд
наизусть и набирать их руками.

Первое - список команд, который бот заявляет Telegram при запуске
(`set_my_commands`). Он заполняет кнопку меню слева от поля ввода. Описания
разбираются из `bot.texts.HELP`: список команд в боте один, и второй его
список рядом разошёлся бы с первым молча, при первой же правке текста.

Админские команды в общий список не попадают никогда. Для постороннего их как
бы нет: они не отвечают ничем, ни отказом, ни ошибкой, чтобы перебор команд не
выдал админку. Общее меню с ними отменило бы всю эту работу целиком, поэтому
владельцу расширенный список уходит отдельно, областью видимости на его личный
разговор с ботом.

Отдельно стоит экран «мои отчёты»: одна подписка открывает несколько разных
отчётов, и селлеру нужно видеть, за что он заплатил, с дорогой к каждому под
пальцем. В приветствие они не помещаются: отчётов четыре, и приветствие из
восьми кнопок и есть тот самый второй список команд. Экран собирается по
списку отчётов из конфига, второго их списка здесь нет.

Второе - кнопки. Кнопка не повторяет команду, а зовёт ровно ту же функцию
хендлера, что и команда: правка в одном месте меняет и то, и другое. Прав
кнопка не даёт никаких. `callback_data` приходит от клиента и подделывается
свободно, поэтому платная команда под кнопкой обёрнута тем же
`require_module`, что и при вводе руками, а состояние кабинета читается из
базы, а не берётся из нажатой кнопки.
"""

from __future__ import annotations

import functools
import logging
import re
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import Callable

from telegram import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import CallbackQueryHandler, ContextTypes

from agents import ads as ads_agent
from agents import finance as finance_agent
from agents import funnel as funnel_agent
from agents import rnp as rnp_agent
from agents import watchdog as watchdog_agent
from bot import texts
from bot.handlers import (
    ads,
    connect,
    costs,
    diagnostic,
    dynamics,
    finance,
    funnel,
    profit,
    rnp,
    settings,
    tariffs,
)
from bot.texts import Safe, fill
from core import audit, clients, config

logger = logging.getLogger(__name__)

# Свой префикс кнопок. Занятые соседями: adm:, inv:, invpay:, connect:, fin:,
# profit:, set:, buy:, trial:. Сторож на пересечение стоит в тестах.
PREFIX = "menu:"

# Потолок описания команды у Telegram. Это факт про Telegram, а не настройка
# владельца, поэтому число стоит здесь, а не в config.toml.
DESCRIPTION_LIMIT = 256

# Команд, которых нет в списке HELP: /start открывает разговор, /help этот
# список и показывает. Описания короткие, их читает селлер в меню.
START_DESCRIPTION = "с чего начать и что бот умеет"
HELP_DESCRIPTION = "все команды бота списком"

# Команды владельца. В HELP их нет и быть не должно: HELP видит любой.
ADMIN_COMMANDS: tuple[tuple[str, str], ...] = (
    ("stats", "сводка по клиентам, деньгам и марже"),
    ("acts", "реестр оплаченных счетов за месяц"),
    ("grant", "выдать доступ к модулю вручную"),
    ("revoke", "снять доступ и записать причину"),
    ("tasks", "очередь задач и последние ошибки"),
    ("diag", "проверка связи с серверами Wildberries"),
)

# Строка списка команд в HELP: «<code>/finance</code> - недельная раскладка...».
COMMAND_LINE = re.compile(r"^<code>/([a-z][a-z0-9_]*)</code>\s+-\s+(.+?)\s*$")

UNKNOWN = (
    "Эта кнопка из старого сообщения и больше не работает. "
    "Наберите <code>/help</code>, там все команды."
)


def from_help(text: str = texts.HELP) -> tuple[tuple[str, str], ...]:
    """Команды и человеческие описания к ним, разобранные из HELP.

    Разбор, а не второй список: формулировки для селлера уже написаны один
    раз, и меню обязано показывать ровно их.
    """
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in text.splitlines():
        match = COMMAND_LINE.match(line.strip())
        if match is None:
            continue
        name = match.group(1)
        if name in seen:
            continue
        seen.add(name)
        found.append((name, match.group(2)[:DESCRIPTION_LIMIT]))
    return tuple(found)


CLIENT_COMMANDS: tuple[tuple[str, str], ...] = (
    (("start", START_DESCRIPTION),) + from_help() + (("help", HELP_DESCRIPTION),)
)


# --- заявка списка команд ---


def _commands(pairs: tuple[tuple[str, str], ...]) -> list[BotCommand]:
    return [BotCommand(name, description[:DESCRIPTION_LIMIT]) for name, description in pairs]


async def _declare(bot, pairs: tuple[tuple[str, str], ...], scope) -> bool:
    """Одна заявка. Отказ Telegram остаётся записью в журнале, и только.

    Сетевой вызов на запуске не имеет права уронить бота: меню это удобство,
    а без него бот работает ровно как раньше, командами.
    """
    try:
        await bot.set_my_commands(_commands(pairs), scope=scope)
        return True
    except Exception as exc:  # noqa: BLE001 - молчание Telegram не наша авария
        logger.exception("не удалось заявить список команд")
        audit.log(
            "menu",
            None,
            "Telegram не принял список команд, меню осталось прежним: "
            f"{type(exc).__name__}: {exc}",
            level="warning",
        )
        return False


async def publish(bot) -> int:
    """Заявляет меню: клиентское всем, расширенное каждому владельцу отдельно.

    Возвращает число удавшихся заявок. Владельцу, который ни разу не написал
    боту, личной области видимости у Telegram ещё нет, и это обычный отказ, а
    не поломка: запись в журнале есть, запуск продолжается.
    """
    done = 0
    if await _declare(bot, CLIENT_COMMANDS, BotCommandScopeAllPrivateChats()):
        done += 1
    for admin_id in config.admin_ids():
        scope = BotCommandScopeChat(chat_id=admin_id)
        if await _declare(bot, CLIENT_COMMANDS + ADMIN_COMMANDS, scope):
            done += 1
    audit.log("menu", None, f"Меню команд заявлено, областей видимости: {done}.")
    return done


# --- кнопки главных действий ---


@dataclass(frozen=True)
class Action:
    """Кнопка: надпись и та самая команда, которую она нажимает.

    Функция берётся из модуля по имени в момент нажатия, а не запоминается
    при сборке: кнопка обязана попадать туда же, куда попадает команда, даже
    если хендлер подменили.
    """

    label: str
    # Модуль команды. Строкой он записан там, где прямой импорт замкнул бы
    # круг: /help живёт в bot/app.py, а тот импортирует это меню.
    module: ModuleType | str
    attr: str
    # Модуль подписки, если команда платная. Проверку делает require_module.
    needs: str | None = None


CONNECT = "connect"
COSTS = "costs"
DIAGNOSTIC = "diagnostic"
FINANCE = "finance"
DYNAMICS = "dynamics"
PROFIT = "profit"
RNP = "rnp"
ADS = "ads"
FUNNEL = "funnel"
REPORTS = "reports"
TARIFFS = "tariffs"
SETTINGS = "settings"
HELP = "help"

ACTIONS: dict[str, Action] = {
    CONNECT: Action("🔌 Подключить кабинет", connect, "connect_command"),
    # Себестоимость денег не стоит и подписки не требует: она нужна, чтобы
    # уже оплаченная прибыль по артикулам вообще посчиталась.
    COSTS: Action("📦 Внести себестоимость", costs, "costs_command"),
    DIAGNOSTIC: Action("🔎 Бесплатный разбор", diagnostic, "diagnostic_command"),
    # Токен отчёта совпадает с именем его команды: по этому имени экран
    # отчётов собирает кнопки прямо из списка отчётов в конфиге.
    FINANCE: Action(
        "💰 Деньги за неделю",
        finance,
        "finance_command",
        needs=finance_agent.MODULE,
    ),
    DYNAMICS: Action(
        "📉 Что выросло",
        dynamics,
        "dynamics_command",
        needs=watchdog_agent.MODULE,
    ),
    PROFIT: Action(
        "📦 Прибыль по артикулам",
        profit,
        "profit_command",
        needs=finance_agent.MODULE,
    ),
    RNP: Action("📈 План-факт", rnp, "rnp_command", needs=rnp_agent.MODULE),
    ADS: Action("📣 Реклама", ads, "ads_command", needs=ads_agent.MODULE),
    FUNNEL: Action(
        "🔻 Воронка", funnel, "funnel_command", needs=funnel_agent.MODULE
    ),
    # Экран отчётов это единственная кнопка, за которой нет команды: полного
    # списка отчётов в меню Telegram не собрать, там команды идут вперемешку
    # с подключением и оплатой. Своей команды он не заводит нарочно, иначе
    # список команд вырос бы ровно затем, чтобы сторож приветствия пропустил
    # лишнюю кнопку.
    REPORTS: Action("📊 Мои отчёты", "bot.handlers.menu", "reports_screen"),
    TARIFFS: Action("💼 Тарифы и цены", tariffs, "tariffs_command"),
    SETTINGS: Action("⚙️ Настройки", settings, "settings_command"),
    HELP: Action("ℹ️ Что я умею", "bot.app", "help_command"),
}

# Кнопки это «с чего начать», а не второй список команд: полный список живёт в
# меню Telegram слева от поля ввода, и повторять его здесь стеной кнопок незачем.
#
# Кабинет не подключён: отчёты показывать нечестно, они всё равно ответят
# «сначала /connect». Бесплатный разбор при этом оставлен намеренно: в
# приветствии он обещан как бесплатный крючок, а без кабинета он отвечает не
# молчанием и не ошибкой, а объяснением, что нужен /connect. Кнопка, которая
# честно объясняет следующий шаг, лучше отсутствующей кнопки.
#
# Кабинет подключён: то, что селлер делает часто. «Что я умею» отсюда убрано,
# человек уже в работе, а полный список у него в меню. Отчёты стоят одной
# кнопкой «Мои отчёты», а не четырьмя подряд: их четыре, и приветствие из
# восьми кнопок и есть тот самый второй список команд. На своём экране у
# каждого отчёта помещается строка про то, зачем он нужен, а в приветствии
# помещалась бы только надпись на кнопке.
#
# Кабинет подключён, а себестоимости нет: первой строкой встаёт «Внести
# себестоимость». Это и есть всё повторное напоминание, какое тут будет.
# Ежедневное сообщение про себестоимость читалось бы как навязчивость, и его
# бы выключили; кнопка попадается на глаза ровно тогда, когда человек сам
# открыл бота, и гаснет сама, как только появилась первая строка
# себестоимости. Выключать её поэтому не нужно и нечем.
FIRST_STEPS = ((CONNECT,), (DIAGNOSTIC, TARIFFS), (HELP,))
CONNECTED = ((REPORTS,), (DIAGNOSTIC, TARIFFS), (SETTINGS,))
NO_COSTS = ((COSTS,),) + CONNECTED


def module_of(action: Action) -> ModuleType:
    """Модуль команды. Строка означает ленивый импорт в момент нажатия."""
    if isinstance(action.module, str):
        return import_module(action.module)
    return action.module


def handler_of(action: Action, path: str | Path | None = None) -> Callable:
    """Та же функция, что и у команды, с той же проверкой доступа.

    Путь к базе передаётся всем без исключения: это один из двух швов проекта,
    и хендлер, который его не примет, в тестах молча пошёл бы в боевую базу.
    """
    command = functools.partial(getattr(module_of(action), action.attr), path=path)
    if action.needs:
        command = tariffs.require_module(action.needs, path=path)(command)
    return command


def _rows(layout: tuple[tuple[str, ...], ...]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    ACTIONS[token].label, callback_data=f"{PREFIX}{token}"
                )
                for token in row
            ]
            for row in layout
        ]
    )


def costs_keyboard() -> InlineKeyboardMarkup:
    """Одна кнопка «Внести себестоимость».

    Нужна подключению кабинета и настройкам: там про себестоимость говорится
    словами, и дорога должна быть под пальцем, а не в памяти. Кнопка та же
    самая, что в меню, и ведёт в ту же функцию, что команда `/costs`.
    """
    return _rows(((COSTS,),))


# --- экран «мои отчёты» ---

REPORTS_HEAD = "📊 <b>Ваши отчёты</b>"

# Модуль это не одна функция, и здесь это видно сразу: цена, число отчётов,
# и дальше у каждого отчёта своё имя и своя польза.
#
# «990 ₽ в месяц: три отчёта» читалось как чек на три штуки, а «590 ₽: один
# отчёт» и вовсе как плата за одну выгрузку. Поэтому слово «разных» стоит
# обязательно, а у модуля с единственным отчётом число не печатается вовсе:
# список из одной строки и так виден, а «один отчёт» рядом с ценой только
# пугает.
REPORTS_MODULE = "<b>{title}</b>, {price} в месяц. Входят {count}:"
REPORTS_MODULE_ONE = "<b>{title}</b>, {price} в месяц."
REPORTS_LINE = "<b>{title}</b>, команда <code>/{command}</code>\n{gives}"
# Отчёт, названный так же, как модуль: своё имя он уже получил строкой выше,
# и второй раз подряд оно читается как сбой, а не как заголовок.
REPORTS_ALONE = "Команда <code>/{command}</code>\n{gives}"

REPORTS_FOOT = (
    "Каждый отчёт можно запрашивать сколько угодно раз: подписка открывает "
    "отчёты, а не считает запросы.\n\n"
    "Кнопка открывает тот же отчёт, что и команда. Если модуль ещё не "
    "оплачен, бот покажет, что он даёт и сколько стоит."
)
REPORTS_EMPTY = (
    "Отчётов пока нет. Посмотрите, что открыто сейчас: "
    "команда <code>/tariffs</code>."
)

# Отчётов у модуля единицы, и «3 отчёта» рядом с ценой читается как чек, а не
# как обещание. Число словами избавляет и от склонения.
COUNT_WORDS = {
    2: "два разных отчёта",
    3: "три разных отчёта",
    4: "четыре разных отчёта",
}


def count_words(count: int) -> str:
    """Сколько разных отчётов открывает подписка. Для одного не зовётся."""
    return COUNT_WORDS.get(int(count), f"{count} разных отчётов")


def report_tokens() -> tuple[str, ...]:
    """Отчёты в порядке конфига, второго их списка в коде нет.

    Кнопка рисуется только той команде, которая в боте действительно есть:
    опечатка в конфиге не должна обещать селлеру отчёт, ведущий в никуда.
    """
    found: list[str] = []
    for info in config.visible_modules().values():
        for report in info.reports:
            if report.command in ACTIONS and report.command not in found:
                found.append(report.command)
    return tuple(found)


def reports_keyboard() -> InlineKeyboardMarkup:
    """Кнопка на отчёт, по две в ряду: на телефоне третья уже не читается."""
    tokens = report_tokens()
    return _rows(tuple(tokens[start : start + 2] for start in range(0, len(tokens), 2)))


def reports_text() -> Safe:
    """Что селлер покупает и какой отчёт это открывает. Всё из конфига.

    Состояние подписки здесь не печатается: оно живёт в `/tariffs`, и второе
    место, где оно написано, разошлось бы с первым молча.
    """
    blocks: list[str] = [REPORTS_HEAD]
    for name, info in config.visible_modules().items():
        if not info.reports:
            continue
        module_title = info.title or name
        price = tariffs.rubles(config.price_decimal(name, 1))
        if len(info.reports) == 1:
            lines = [fill(REPORTS_MODULE_ONE, title=module_title, price=price)]
        else:
            lines = [
                fill(
                    REPORTS_MODULE,
                    title=module_title,
                    price=price,
                    count=count_words(len(info.reports)),
                )
            ]
        for report in info.reports:
            title = report.title or report.command
            gives = tariffs.sentence(report.gives)
            if title == module_title:
                lines.append(fill(REPORTS_ALONE, command=report.command, gives=gives))
                continue
            lines.append(
                fill(REPORTS_LINE, title=title, command=report.command, gives=gives)
            )
        blocks.append(Safe("\n\n".join(lines)))
    if len(blocks) == 1:
        blocks.append(REPORTS_EMPTY)
    blocks.append(REPORTS_FOOT)
    return Safe("\n\n".join(blocks))


async def reports_screen(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    path: str | Path | None = None,
) -> None:
    """Экран отчётов. Прав не даёт: доступ проверяет сама команда отчёта."""
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(
        reports_text(),
        parse_mode=ParseMode.HTML,
        reply_markup=reports_keyboard(),
    )


def main_keyboard(
    update: Update,
    *,
    path: str | Path | None = None,
    without: tuple[str, ...] = (),
) -> InlineKeyboardMarkup:
    """Кнопки под приветствием. Состояние кабинета читается из базы.

    Набор зависит от того, подключён ли кабинет, но узнаёт это сервер: по
    нажатию проверки идут заново, и нарисованная кнопка ничего не открывает.

    `without` убирает кнопку из набора. Нужен там, где кнопка вела бы на то
    самое сообщение, в котором нарисована: в ответе /help «Что я умею»
    показала бы ровно этот же текст.
    """
    client_id = tariffs.client_id_of(update, path)
    working = client_id is not None and clients.connected(client_id, path=path)
    rows = FIRST_STEPS
    if working:
        rows = CONNECTED if costs.has_costs(client_id, path=path) else NO_COSTS
    if without:
        rows = tuple(
            tuple(name for name in row if name not in without) for row in rows
        )
        rows = tuple(row for row in rows if row)
    return _rows(rows)


async def pressed(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, path: str | Path | None = None
) -> None:
    """Нажата кнопка меню. Отвечаем сразу, иначе кнопка крутится у клиента."""
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    action = ACTIONS.get((query.data or "").removeprefix(PREFIX))
    if action is None:
        # Кнопку из старого сообщения и выдуманную клиентом здесь не различить,
        # и различать незачем: обе не делают ничего.
        message = update.effective_message
        if message is not None:
            await message.reply_text(UNKNOWN, parse_mode=ParseMode.HTML)
        return
    await handler_of(action, path)(update, context)


def register(app, *, path: str | Path | None = None) -> None:
    """Сам себя регистрирует: bot/app.py никто не трогает."""
    app.add_handler(
        CallbackQueryHandler(
            functools.partial(pressed, path=path), pattern=f"^{PREFIX}"
        )
    )
