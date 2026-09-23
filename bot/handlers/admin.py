"""Команды владельца: бизнес целиком, акты, ручная выдача и упавшие задачи.

Здесь всё, что нужно владельцу бота и не нужно клиенту: `/stats`, `/acts`,
`/grant`, `/revoke`, `/tasks`. Плюс ограничение частоты команд, чтобы один
человек не мог задолбать бота.

Главное свойство этого файла - чужой не должен узнать, что эти команды
существуют. Несуществующая команда в этом боте не отвечает ничем: хендлера
нет, ответа нет. Значит и админ-команда у чужого не отвечает ничем: ни
«доступ запрещён», ни «не понял», ни ошибки. Иначе перебор команд выдал бы
список админки любому желающему, и это единственная защита, которую здесь
можно обойти вниманием.

Второе свойство - клиент здесь всегда внутренний id. Ни Telegram-аккаунта,
ни имени, ни токена, ни реквизитов в ответы админки и в журнал не попадает.
Текст упавшей задачи это тоже ответ админки: в `last_error` попадает что
угодно, вплоть до куска ответа WB с токеном, поэтому он показывается только
через `audit.redact`, как и всё остальное.
Исключение ровно одно: реестр для актов, где ИНН и название организации
нужны бухгалтерии по закону.

Третье свойство - в ответах владельцу разметку ставит только бот. Сообщения
уходят с `ParseMode.HTML`, а подставляется в них чужое: имя модуля из
аргумента команды, номер платежа, текст упавшей задачи, запись журнала.
Угловая скобка в таком значении либо становится нашей же разметкой (ссылка
в личке владельца, от его собственного бота), либо ломает сообщение целиком,
и владелец не получает сводку вообще. Поэтому граница здесь одна и стоит на
подстановке: `bot.texts.fill` экранирует всё, что в неё попало, а собранный
ботом текст помечается `Safe` вслух. Инструмент общий, а не свой: он нужен
каждому, кто шлёт с `ParseMode.HTML`, и вторая его копия однажды разошлась
бы с первой. Экранируется сообщение, а не данные: в базе и в книгах Excel
имя лежит так, как его написали.

Доступ включается только через `core.access.grant_access` и гасится только
через `core.access.revoke_access`: других дверей в системе нет, и ни `/grant`,
ни `/revoke` их не обходят. Реестр актов собирает `core.billing`: он
складывается из счетов, здесь остаётся команда и ежемесячная отправка.
"""

from __future__ import annotations

import functools
import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    TypeHandler,
)

from bot import texts
from bot.texts import Safe, fill
from bot.handlers.tariffs import local_date, rubles
from core import access, audit, billing, config, db, metering, queue, ratelimit, scheduler

logger = logging.getLogger(__name__)

PREFIX = "adm:"
RETRY = f"{PREFIX}retry:"

# Проверка частоты стоит раньше всех остальных хендлеров: она решает, дойдёт
# ли обновление до своего обработчика вообще. И стоит она на всех типах
# обновлений, а не на командах: дорогое в этом боте как раз не команда, а
# присланный файл и присланный токен, который проверяется живым запросом к
# Wildberries. Бот обрабатывает по одному обновлению за раз, поэтому поток
# таких сообщений от одного человека останавливает бота для всех остальных.
GUARD_GROUP = -100

# Ответ на слишком частое нажатие кнопки. Уходит всплывающей подсказкой:
# молчание Telegram показывает как вечно крутящуюся кнопку, и человек решит,
# что бот сломался.
BUTTON_WAIT = "Слишком часто. Подождите немного."

ACTS_JOB = "acts_monthly"

# Способы оплаты из ТЗ: счёт, карта, другое. Слева то, что владелец пишет
# руками, справа то, что ложится в журнал.
METHODS = {
    "счёт": "invoice",
    "счет": "invoice",
    "invoice": "invoice",
    "карта": "card",
    "картой": "card",
    "card": "card",
    "другое": "other",
    "other": "other",
}

# Сколько строк показывать в /tasks и сколько записей журнала под ними.
TASKS_LIMIT = 10
EVENTS_LIMIT = 5

STATS_HEAD = "📊 <b>WBРентген за {title}</b>"
# Слово «клиент» у владельца должно всюду значить одно число. Здесь и в /tasks
# это внутренний id, и подпись говорит об этом прямо: в уведомлении о счёте
# владелец видит Telegram-аккаунт, и два числа легко спутать.
CLIENT_NOTE = (
    "Клиент везде это внутренний id. /grant и /revoke понимают и его, "
    "и Telegram ID из уведомления о счёте."
)
NO_CLIENTS = "Клиентов пока нет."
NO_CALLS = "Вызовов к Wildberries за месяц не было."
STATS_MARGIN = "<b>Маржа по клиентам</b>"

ACTS_EMPTY = "За {title} оплаченных счетов нет, реестр собирать не из чего."
ACTS_CAPTION = "Оплаченные счета за {title}. Номер, дата, ИНН, название, модуль, период, сумма."
ACTS_BAD_MONTH = "Месяц пишется так: <code>/acts 2026-08</code>. Без него беру текущий."

GRANT_USAGE = (
    "Выдать доступ вручную:\n"
    "<code>/grant id модуль месяцев способ номер_платежа</code>\n\n"
    "Например: <code>/grant 12 finance 3 счёт WBR-2026-0007</code>\n"
    "Способ: счёт, карта или другое.\n"
    "Клиент это внутренний id из /stats или Telegram ID из уведомления о счёте: "
    "понимаю оба, переводить одно в другое не нужно."
)
GRANT_DONE = "Клиент {client_id}: модуль {module} включён до {until}. Платёж {ref}, способ {method}."
GRANT_DUPLICATE = (
    "Это дубль: платёж {ref} уже проводили, ничего не продлено. "
    "Модуль {module} работает до {until}."
)
UNKNOWN_MODULE = "Нет такого модуля: {module}. Есть: {known}."
UNKNOWN_CLIENT = (
    "Клиента с номером {client_id} в базе нет: ни внутреннего id, ни Telegram-аккаунта."
)
# Одно число может означать двух разных клиентов. Угадывать в команде, которая
# стоит денег, нельзя: бот называет обоих и просит повторить однозначно.
CLIENT_AMBIGUOUS = (
    "Число {value} означает сразу двоих: это внутренний id клиента {by_id} и "
    "Telegram-аккаунт клиента {by_telegram}. Угадывать не буду. Повторите с "
    "внутренним id, он показан в /stats."
)
FOUND_BY_TELEGRAM = "Узнал клиента по Telegram-аккаунту: внутренний id {client_id}."
BAD_NUMBER = "Срок пишется числом месяцев, например 3."

REVOKE_USAGE = (
    "Отменить доступ:\n"
    "<code>/revoke id модуль причина</code>\n\n"
    "Например: <code>/revoke 12 finance возврат по счёту WBR-2026-0007</code>\n"
    "Вместо модуля можно написать <code>все</code>: тогда гаснет всё, что работает.\n"
    "Клиент, как и у /grant, это внутренний id или Telegram ID: понимаю оба."
)

# Слово вместо модуля: погасить у клиента всё сразу, обходом по модулям.
ALL_MODULES = {"все", "всё", "all"}
REVOKE_DONE = (
    "Клиент {client_id}: модуль {module} отключён, сгорело оплаченных дней {days}. "
    "Причина записана в журнал. Новая выдача пойдёт по новому номеру платежа: "
    "отмена не открывает старый платёж заново."
)
REVOKE_NOTHING = "У клиента {client_id} модуль {module} и так не подключён."

TASKS_HEAD = "🧰 <b>Задачи и ошибки</b>"
TASKS_NONE = "Упавших задач нет, в работе тоже пусто."
TASKS_RUNNING = "<b>В работе</b>"
TASKS_FAILED = "<b>Упали</b>"
TASKS_EVENTS = "<b>Последние ошибки в журнале</b>"
RETRY_BUTTON = "Перезапустить #{task_id}"
RETRY_DONE = "Задача #{task_id} возвращена в очередь, счётчик попыток обнулён."
RETRY_GONE = "Такой задачи уже нет."


# --- кто спрашивает ---


def _owner(update) -> int | None:
    """Telegram ID владельца или None. Для всех остальных команда не существует."""
    user = getattr(update, "effective_user", None)
    telegram_id = getattr(user, "id", None)
    return int(telegram_id) if config.is_admin(telegram_id) else None


async def _reply(update, text: str, *, reply_markup: Any = None, **fields: Any) -> None:
    """Единственная отправка с разметкой в этом файле, она же и граница.

    Либо шаблон и поля к нему, и тогда поля экранируются здесь, либо готовый
    текст с пометкой `Safe`. Третьего пути к `ParseMode.HTML` из этого файла
    нет, и обойти его случайной f-строкой не выйдет: незаполненный шаблон без
    полей всё равно проходит через `fill`.
    """
    message = getattr(update, "effective_message", None)
    if message is None:
        return
    body = text if isinstance(text, Safe) else fill(text, **fields)
    extra = {"reply_markup": reply_markup} if reply_markup is not None else {}
    await message.reply_text(body, parse_mode=ParseMode.HTML, **extra)


# --- H5: ограничение частоты команд ---


async def rate_guard(update, context) -> None:
    """Пропускает обновление или вежливо просит подождать. Стоит раньше всех.

    Считается человек, а не клиент: защищаться нужно от собеседника, и ходить
    за этим в базу на каждое сообщение незачем.

    Три решения, которые тут видно.

    Первое: под счёт попадает любое обновление, а не команда. Текст, документ
    и нажатие кнопки стоят боту не меньше команды, а иногда куда больше.

    Второе: у кнопок своё, более терпимое окно, и отвечают им всплывающей
    подсказкой. Двойное нажатие это обычное дело, а не нападение, и молчание
    в ответ на кнопку выглядит как сломанный бот.

    Третье: словами бот отвечает один раз за окно. Иначе на поток сообщений
    он ответил бы потоком же и сам стал бы усилителем нагрузки, а человек и
    так уже прочитал, что надо подождать.
    """
    user = getattr(update, "effective_user", None)
    who = getattr(user, "id", None)
    if who is None:
        return
    # Владельца бот не ограничивает: защищаться нужно от чужого потока команд,
    # а владелец гоняет /tasks и /stats как раз тогда, когда что-то упало.
    if config.is_admin(who):
        return

    query = getattr(update, "callback_query", None)
    if query is not None:
        if ratelimit.allow(int(who), scope=ratelimit.BUTTONS, limit=ratelimit.button_per_minute()):
            return
        await query.answer(BUTTON_WAIT)
        raise ApplicationHandlerStop

    if ratelimit.allow(int(who)):
        return
    message = getattr(update, "effective_message", None)
    if message is not None and ratelimit.allow(int(who), scope=ratelimit.WARNED, limit=1):
        await message.reply_text(texts.RATE_LIMITED)
    # Дальше по группам обновление не пойдёт: ни к своей команде, ни к чужой.
    raise ApplicationHandlerStop


# --- H1: /stats ---


def stats_text(result: metering.Stats) -> Safe:
    """Бизнес одним экраном. Клиенты только внутренними id.

    Каждая строка собирается `fill`: имя модуля и имя метода WB приходят из
    базы, а не из этого файла, и угловая скобка в них не должна становиться
    разметкой. Готовая сводка помечается `Safe` уже целиком.
    """
    lines = [fill(STATS_HEAD, title=result.title), ""]

    lines.append(
        fill(
            "Клиентов всего: {total}, с рабочим доступом: {active}",
            total=result.total_clients,
            active=result.active_clients,
        )
    )
    if result.modules:
        for module, count in sorted(result.modules.items(), key=lambda item: (-item[1], item[0])):
            lines.append(fill("  {module}: {count}", module=module, count=count))
    elif not result.total_clients:
        lines.append(NO_CLIENTS)
    lines.append("")

    lines.append(fill("Выручка за месяц: {amount}", amount=rubles(result.revenue)))
    lines.append(
        fill(
            "Счета выставлены и не оплачены: {count} на {amount}",
            count=result.unpaid_count,
            amount=rubles(result.unpaid_amount),
        )
    )
    if result.ai_cost:
        lines.append(fill("Расход на нейросеть: {amount}", amount=rubles(result.ai_cost)))
    lines.append("")

    lines.append(
        fill(
            "<b>Расходы: вызовы WB</b> всего {calls}, с ошибкой {errors}",
            calls=result.calls_total,
            errors=result.errors_total,
        )
    )
    if result.calls:
        for use in result.calls[:TASKS_LIMIT]:
            if use.errors:
                lines.append(
                    fill(
                        "  {method}: {count}, ошибок {errors}",
                        method=use.method,
                        count=use.count,
                        errors=use.errors,
                    )
                )
            else:
                lines.append(fill("  {method}: {count}", method=use.method, count=use.count))
    else:
        lines.append(NO_CALLS)
    lines.append("")

    if result.clients:
        lines.append(STATS_MARGIN)
        for item in result.clients:
            lines.append(
                fill(
                    "  клиент #{client_id} ({modules}): выручка {revenue}, "
                    "расход {cost}, маржа {margin}, вызовов {calls}",
                    client_id=item.client_id,
                    modules=", ".join(item.modules) if item.modules else "без доступа",
                    revenue=rubles(item.revenue),
                    cost=rubles(item.cost),
                    margin=rubles(item.margin),
                    calls=item.calls,
                )
            )
    return Safe("\n".join(lines).strip())


async def stats_command(
    update, context, *, path: str | Path | None = None, today: date | None = None
) -> None:
    if _owner(update) is None:
        return
    result = metering.stats("month", today=today, path=path)
    await _reply(update, stats_text(result))


# --- D8: /acts и ежемесячный реестр ---


def _month_bounds_of(text: str, today: date | None) -> tuple[datetime, datetime] | None:
    """Границы месяца по «ГГГГ-ММ» или текущий месяц, если ничего не сказали.

    Считает их core.billing: календарь реестра и календарь сводки должны
    совпадать, иначе «за сентябрь» в файле и в /stats означало бы разное.
    """
    try:
        return billing.month_bounds(text or None, today=today)
    except billing.BadPeriod:
        return None


async def acts_command(
    update, context, *, path: str | Path | None = None, today: date | None = None
) -> None:
    if _owner(update) is None:
        return
    args = list(getattr(context, "args", None) or [])
    bounds = _month_bounds_of(args[0] if args else "", today)
    if bounds is None:
        await _reply(update, ACTS_BAD_MONTH)
        return
    start, end = bounds
    title = billing.month_title(start)
    invoices = billing.paid_invoices(start, end, path=path)
    if not invoices:
        await _reply(update, ACTS_EMPTY, title=title)
        return
    message = getattr(update, "effective_message", None)
    if message is None:
        return
    # Подпись к файлу уходит без разметки, поэтому и без экранирования: иначе
    # владелец читал бы в ней `&quot;` вместо кавычек.
    await message.reply_document(
        document=billing.acts_book(invoices, path=path),
        filename=billing.acts_file_name(start),
        caption=ACTS_CAPTION.format(title=title),
    )
    audit.log("admin.acts", None, f"реестр актов за {title}: счетов {len(invoices)}", path=path)


def make_acts_job(app: Any, path: str | Path | None = None):
    """Ежемесячная отправка реестра. Работа ежедневная, дело делает первого числа.

    Отдельного «ежемесячного» расписания в проекте нет, и заводить его ради
    одной работы незачем: ежедневная задача сама смотрит на число.
    """

    async def job(task) -> None:
        payload = task.payload if isinstance(task.payload, dict) else json.loads(task.payload or "{}")
        try:
            day = date.fromisoformat(str(payload.get("date", "")))
        except ValueError:
            return
        if day.day != 1:
            return
        start, end = billing.previous_month_bounds(day)
        title = billing.month_title(start)
        invoices = billing.paid_invoices(start, end, path=path)
        if not invoices:
            audit.log("admin.acts", None, f"за {title} оплаченных счетов нет", path=path)
            return
        data = billing.acts_book(invoices, path=path)
        name = billing.acts_file_name(start)
        for owner_id in config.admin_ids():
            await app.bot.send_document(
                chat_id=int(owner_id),
                document=data,
                filename=name,
                caption=ACTS_CAPTION.format(title=title),
            )
        audit.log("admin.acts", None, f"реестр актов за {title} отправлен: счетов {len(invoices)}", path=path)

    return job


# --- D10: /grant и /revoke ---


class AmbiguousClient(Exception):
    """Одно число указывает на двух разных клиентов."""

    def __init__(self, value: int, by_id: int, by_telegram: int) -> None:
        self.value = value
        self.by_id = by_id
        self.by_telegram = by_telegram
        super().__init__(f"{value}: клиенты {by_id} и {by_telegram}")


def resolve_client(value: int, *, path: str | Path | None = None) -> tuple[int, str] | None:
    """Кого имел в виду владелец: внутренний id или Telegram ID из уведомления.

    Владелец видит клиента в двух видах: внутренним id в /stats и /tasks и
    Telegram-аккаунтом в уведомлении о новом счёте. Переводить одно число в
    другое руками он не должен, поэтому команды понимают оба.

    Правило разбора предсказуемое, а не «как угадается». Внутренний id имеет
    приоритет: это то, что означает слово «клиент» во всех ответах владельцу.
    Telegram ID подхватывается, только когда внутреннего такого нет. А если
    число значит и то и другое у разных клиентов, команда не выполняется
    вовсе: выдача доступа стоит денег, и промах здесь дороже лишнего вопроса.

    Отдаёт пару (внутренний id, как узнали) или None, если такого нет.
    """
    admin = db.admin_repo(path)
    by_id = admin.client(value)
    by_telegram = admin.client_by_telegram(value)
    if by_id is not None and by_telegram is not None and int(by_telegram["id"]) != value:
        raise AmbiguousClient(value, int(by_id["id"]), int(by_telegram["id"]))
    if by_id is not None:
        return int(by_id["id"]), "id"
    if by_telegram is not None:
        return int(by_telegram["id"]), "telegram"
    return None


async def _resolved(update, value: int, path) -> tuple[int, str] | None:
    """Разбирает номер клиента и сам объясняет владельцу, если не вышло."""
    try:
        found = resolve_client(value, path=path)
    except AmbiguousClient as clash:
        await _reply(
            update,
            CLIENT_AMBIGUOUS,
            value=clash.value,
            by_id=clash.by_id,
            by_telegram=clash.by_telegram,
        )
        return None
    if found is None:
        await _reply(update, UNKNOWN_CLIENT, client_id=value)
        return None
    return found


async def grant_command(
    update, context, *, path: str | Path | None = None, now: datetime | None = None
) -> None:
    if _owner(update) is None:
        return
    args = list(getattr(context, "args", None) or [])
    if len(args) < 5:
        await _reply(update, GRANT_USAGE)
        return

    raw_client, module, raw_months, raw_method = args[:4]
    payment_ref = " ".join(args[4:]).strip()
    method = METHODS.get(raw_method.strip().lower())
    if method is None:
        await _reply(update, GRANT_USAGE)
        return
    try:
        asked = int(raw_client)
        months = int(raw_months)
    except ValueError:
        await _reply(update, BAD_NUMBER)
        return
    if module not in config.modules():
        # Имя модуля здесь ровно то, что владелец набрал в команде, и обратно
        # в сообщение оно едет через ту же подстановку, что и всё остальное.
        await _reply(update, UNKNOWN_MODULE, module=module, known=", ".join(config.modules()))
        return
    found = await _resolved(update, asked, path)
    if found is None:
        return
    client_id, how = found
    note = fill("\n" + FOUND_BY_TELEGRAM, client_id=client_id) if how == "telegram" else Safe("")

    days = billing.months_to_days(months)
    granted = access.grant_access(
        client_id, module, days, payment_ref, method=method, actor="owner", now=now, path=path
    )
    if granted.duplicate:
        await _reply(
            update,
            Safe(
                fill(
                    GRANT_DUPLICATE,
                    ref=payment_ref,
                    module=granted.module,
                    until=local_date(granted.until),
                )
                + note
            ),
        )
        return
    await _reply(
        update,
        Safe(
            fill(
                GRANT_DONE,
                client_id=client_id,
                module=module,
                until=local_date(granted.until),
                ref=payment_ref,
                method=raw_method,
            )
            + note
        ),
    )


async def revoke_command(
    update, context, *, path: str | Path | None = None, now: datetime | None = None
) -> None:
    """Отмена доступа (возврат). Причина обязательна и уходит в журнал.

    Гасит `core.access.revoke_access` - та же единственная дверь, что и у
    выдачи, только с другой стороны. Прямой записи в module_access здесь нет.

    Слово «все» вместо модуля гасит всё, что у клиента работает: зернистость
    у выдачи и отмены одна, поэтому это обход `status()` с вызовом на каждый
    модуль, а не особый случай внутри ядра.

    Важное следствие идемпотентности: отмена не открывает платёж заново.
    `grant_access` с тем же номером по-прежнему считает его дублем, так что
    после возврата новая выдача идёт по новому номеру платежа.
    """
    if _owner(update) is None:
        return
    args = list(getattr(context, "args", None) or [])
    if len(args) < 3:
        await _reply(update, REVOKE_USAGE)
        return
    try:
        asked = int(args[0])
    except ValueError:
        await _reply(update, REVOKE_USAGE)
        return
    module = args[1]
    reason = " ".join(args[2:]).strip()
    everything = module.strip().lower() in ALL_MODULES
    if not everything and module not in config.modules():
        await _reply(update, UNKNOWN_MODULE, module=module, known=", ".join(config.modules()))
        return
    found = await _resolved(update, asked, path)
    if found is None:
        return
    client_id, how = found
    note = fill("\n" + FOUND_BY_TELEGRAM, client_id=client_id) if how == "telegram" else Safe("")

    if everything:
        names = [item.module for item in access.status(client_id, now=now, path=path) if item.works]
    else:
        names = [module] if access.access_of(client_id, module, now=now, path=path).works else []
    if not names:
        await _reply(update, REVOKE_NOTHING, client_id=client_id, module=module)
        return

    burned = 0
    for name in names:
        before = access.access_of(client_id, name, now=now, path=path)
        access.revoke_access(client_id, name, reason, actor="owner", now=now, path=path)
        burned += before.days_left
    await _reply(
        update,
        Safe(
            fill(REVOKE_DONE, client_id=client_id, module=", ".join(names), days=burned) + note
        ),
    )


# --- H1a и H1b: /tasks и перезапуск ---


def _task_line(row) -> Safe:
    client = row["client_id"]
    who = f"клиент #{int(client)}" if client is not None else "общая"
    line = fill(
        "#{task_id} {kind} ({who}), попыток {attempts}",
        task_id=row["id"],
        kind=row["kind"],
        who=who,
        attempts=int(row["attempts"] or 0),
    )
    # last_error пишет очередь, и туда попадает сырой ответ WB. Показывать его
    # владельцу можно только через ту же чистку, что стоит у журнала: один раз
    # увиденный в чате токен уже не развидеть. Чистка снимает секреты, но не
    # разметку, поэтому текст ошибки идёт дальше через ту же подстановку.
    error = audit.redact(str(row["last_error"] or "")).strip()
    if error:
        line = Safe(line + fill("\n    {error}", error=error))
    return line


def tasks_text(running, failed, errors) -> Safe:
    lines = [TASKS_HEAD, CLIENT_NOTE, ""]
    if not running and not failed:
        lines.append(TASKS_NONE)
    if running:
        lines.append(TASKS_RUNNING)
        lines.extend(_task_line(row) for row in running)
        lines.append("")
    if failed:
        lines.append(TASKS_FAILED)
        lines.extend(_task_line(row) for row in failed)
        lines.append("")
    if errors:
        lines.append(TASKS_EVENTS)
        for row in errors:
            client = row["client_id"]
            who = f"клиент #{int(client)}" if client is not None else "общая"
            lines.append(
                fill(
                    "{at} {kind} ({who}): {message}",
                    at=row["at"],
                    kind=row["kind"],
                    who=who,
                    message=row["message"],
                )
            )
    return Safe("\n".join(lines).strip())


def tasks_keyboard(failed) -> InlineKeyboardMarkup | None:
    """У каждой упавшей задачи своя кнопка «Перезапустить»."""
    if not failed:
        return None
    rows = [
        [InlineKeyboardButton(RETRY_BUTTON.format(task_id=row["id"]), callback_data=f"{RETRY}{row['id']}")]
        for row in failed
    ]
    return InlineKeyboardMarkup(rows)


async def tasks_command(update, context, *, path: str | Path | None = None) -> None:
    if _owner(update) is None:
        return
    admin = db.admin_repo(path)
    running = admin.tasks(state=queue.RUNNING, limit=TASKS_LIMIT)
    failed = admin.tasks(state=queue.FAILED, limit=TASKS_LIMIT)
    errors = admin.events(limit=EVENTS_LIMIT, level="error")
    await _reply(update, tasks_text(running, failed, errors), reply_markup=tasks_keyboard(failed))


def _task_by_id(task_id: int, path) -> Any:
    for row in db.admin_repo(path).tasks(limit=1000):
        if int(row["id"]) == task_id:
            return row
    return None


async def retry_callback(update, context, *, path: str | Path | None = None) -> None:
    """Кнопка «Перезапустить»: попытки обнуляются, задача снова в очереди."""
    query = getattr(update, "callback_query", None)
    if query is None:
        return
    if _owner(update) is None:
        # Чужому не отвечаем даже «нельзя»: кнопки он всё равно не видел.
        return
    try:
        task_id = int(str(query.data).rsplit(":", 1)[1])
    except (IndexError, ValueError):
        await query.answer(RETRY_GONE)
        return
    row = _task_by_id(task_id, path)
    if row is None:
        await query.answer(RETRY_GONE)
        return
    db.admin_repo(path).update_task(
        task_id,
        state=queue.PENDING,
        attempts=0,
        last_error=None,
        next_run_at=None,
        finished_at=None,
    )
    client_id = row["client_id"]
    audit.log(
        "admin.task.retry",
        int(client_id) if client_id is not None else None,
        f"задача #{task_id} ({row['kind']}) перезапущена владельцем",
        path=path,
    )
    await query.answer(RETRY_DONE.format(task_id=task_id))
    await query.edit_message_text(RETRY_DONE.format(task_id=task_id))


# --- регистрация ---


def register(app, *, path: str | Path | None = None) -> None:
    """Сам себя регистрирует: bot/app.py никто не трогает."""
    # TypeHandler, а не MessageHandler: ограничитель должен видеть все типы
    # обновлений, включая документы и нажатия кнопок.
    app.add_handler(TypeHandler(Update, rate_guard), group=GUARD_GROUP)
    app.add_handler(CommandHandler("stats", functools.partial(stats_command, path=path)))
    app.add_handler(CommandHandler("acts", functools.partial(acts_command, path=path)))
    app.add_handler(CommandHandler("grant", functools.partial(grant_command, path=path)))
    app.add_handler(CommandHandler("revoke", functools.partial(revoke_command, path=path)))
    app.add_handler(CommandHandler("tasks", functools.partial(tasks_command, path=path)))
    app.add_handler(
        CallbackQueryHandler(functools.partial(retry_callback, path=path), pattern=f"^{PREFIX}")
    )
    scheduler.register_daily(ACTS_JOB, make_acts_job(app, path))
