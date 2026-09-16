"""Бесплатная диагностика в телеграме: `/diagnostic`.

Это первое, что человек видит от сервиса, и последнее, по чему он решает,
платить или нет. Поэтому здесь нет ни одного отказа без объяснения: не
подключён кабинет, нет категории у токена, кабинет уже разбирали с другого
аккаунта - в каждом случае сказано, что именно произошло и что делать.

Два правила, которые видно в коде.

Первое: в Wildberries отсюда никто не ходит. Команда ставит задачу в очередь
и отвечает; выгрузку недели делает `agents.diagnostic`, и повтор при
недоступности WB живёт в очереди, а не здесь.

Второе: доступ не проверяется вовсе, и это не упущение. Диагностика
бесплатная, `require_module` тут не нужен. Единственное ограничение это один
разбор на кабинет WB, и оно стоит в агенте, потому что живёт в базе.
"""

from __future__ import annotations

import functools
import logging
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from telegram.constants import ParseMode
from telegram.ext import CommandHandler

from agents import diagnostic
from bot.handlers.tariffs import client_id_of, rubles
from core import db

logger = logging.getLogger(__name__)

HEADER = "🔍 <b>Бесплатный разбор недели {days}</b>"

REVENUE_LINE = "Выручка за неделю: {revenue}."

# Три вступления на три разных случая. Смешивать их нельзя: в первом речь про
# отклонение от обычного, в двух других про крупные статьи расходов, и это
# разные утверждения о деньгах.
LEAD_DEVIATION = (
    "Вот три места, где на этой неделе денег ушло больше обычного. Сравниваю "
    "со средним за предыдущие недели."
)
LEAD_QUIET = (
    "Хорошая новость: заметных отклонений от обычного на этой неделе нет. "
    "Показываю самые крупные статьи расходов за неделю. Это не отклонение, "
    "просто самые дорогие строки."
)
LEAD_YOUNG = (
    "Сравнивать пока не с чем: недель в моей истории {have}, а чтобы понять, "
    "что расход вырос, нужно {needed}. Поэтому показываю самые крупные статьи "
    "расходов за неделю. Это не отклонение, а просто самые дорогие строки, и "
    "выдумывать рост я не стану."
)

DEVIATION_LINE = "{number}. {title}: было {was}, стало {now}. За неделю это {rubles}."

# У СПП та же арифметика значит другое: это скидка площадки, а не удержание.
# Сложить её с расходами значит сказать неправду.
SPP_LINE = (
    "{number}. Скидка площадки (СПП): было {was}, стало {now}. По выручке "
    "недели это {rubles}, но это не удержание, к расходам эту сумму не "
    "прибавляют."
)

COST_LINE = "{number}. {title}: {rubles}."

MODULES_HEAD = "<b>Что найдут платные модули на ваших цифрах</b>"
MODULE_LINE = "• {title}: {line}"

FOOTER = (
    "Разбор бесплатный и делается один раз на кабинет Wildberries. Дальше: "
    "/trial даёт 7 дней любого модуля бесплатно, /tariffs показывает цены."
)

REPEAT_NOTE = "Это ваш прошлый разбор от {when}. Заново он не считается."

NEED_CABINET = (
    "🔌 Для бесплатного разбора мне нужен доступ к вашему кабинету Wildberries.\n\n"
    "Разбор привязан к кабинету, а не к переписке: пока кабинет не подключён, "
    "я не знаю, чьи цифры считать. Подключить: /connect. Нужен токен только на "
    "чтение, я ничего не меняю в кабинете."
)

NO_FINANCE = (
    "🔒 У вашего токена нет категории «Финансы», а без неё Wildberries не "
    "отдаёт отчёт о реализации. Считать разбор просто не из чего.\n\n"
    "Как поправить: в кабинете Wildberries создайте новый токен и отметьте "
    "категорию «Финансы», потом пришлите его мне командой /connect. Бесплатный "
    "разбор останется за вами, я его не потратил."
)

ALREADY_TAKEN = (
    "Этот кабинет Wildberries уже получал бесплатный разбор.\n\n"
    "Разбор один на кабинет, а не на аккаунт в Telegram, поэтому со второго "
    "аккаунта он не выдаётся. Что можно сделать дальше: /trial даёт 7 дней "
    "любого модуля бесплатно, /tariffs показывает, что в модулях есть и "
    "сколько это стоит."
)

NO_DATA = (
    "Пока считать нечего: Wildberries ещё не отдал ни одного недельного отчёта "
    "по вашему кабинету. Такой отчёт появляется раз в неделю, обычно в "
    "понедельник. Приходите с /diagnostic после него: бесплатный разбор "
    "остаётся за вами, я его не потратил."
)

GONE = (
    "Бесплатный разбор по этому кабинету уже был, но показать его заново мне "
    "не из чего: данные кабинета удалены, а копий цифр я не храню.\n\n"
    "Если кабинет подключён снова, недельные отчёты соберутся заново. Что "
    "можно сделать сейчас: /trial даёт 7 дней любого модуля бесплатно, "
    "/tariffs показывает цены."
)

REFUSALS: dict[str, str] = {
    diagnostic.NOT_CONNECTED: NEED_CABINET,
    diagnostic.NO_CATEGORY: NO_FINANCE,
    diagnostic.TAKEN: ALREADY_TAKEN,
    diagnostic.NO_DATA: NO_DATA,
    diagnostic.GONE: GONE,
}


# --- форматирование ----------------------------------------------------------


def _percent(value: Decimal | None) -> str:
    """Процент с одним знаком после запятой. Нет значения - прочерк."""
    if value is None:
        return "-"
    return f"{Decimal(value):.1f}".replace(".", ",") + "%"


def _day(iso: str) -> str:
    """ГГГГ-ММ-ДД в ДД.ММ. Пустая дата остаётся пустой."""
    try:
        moment = date.fromisoformat(str(iso))
    except ValueError:
        return str(iso)
    return f"{moment.day:02d}.{moment.month:02d}"


def _days(result: diagnostic.Diagnostic) -> str:
    return f"{_day(result.date_from)} по {_day(result.date_to)}"


def _weeks_word(count: int) -> str:
    """Недели по-русски: 1 неделя, 2 недели, 5 недель."""
    tail = count % 10
    if count % 100 in range(11, 15) or tail == 0 or tail > 4:
        return f"{count} недель"
    return f"{count} неделя" if tail == 1 else f"{count} недели"


def leak_line(number: int, leak: diagnostic.Leak) -> str:
    """Одна утечка строкой. Рубли всегда названы, проценты - если они есть."""
    if not leak.deviation:
        return COST_LINE.format(
            number=number, title=leak.title.capitalize(), rubles=rubles(leak.rubles)
        )
    template = SPP_LINE if leak.metric == "spp" else DEVIATION_LINE
    return template.format(
        number=number,
        title=leak.title.capitalize(),
        was=_percent(leak.was),
        now=_percent(leak.now),
        rubles=rubles(leak.rubles),
    )


def refusal_text(reason: str) -> str:
    """Объяснение отказа. Неизвестной причины быть не должно, но молчать нельзя."""
    return REFUSALS.get(reason, NO_DATA)


def report_text(result: diagnostic.Diagnostic) -> str:
    """Готовое сообщение разбора: неделя, три утечки, что дадут модули."""
    if not result.ok:
        return refusal_text(result.reason)
    if not result.leaks:
        return NO_DATA

    lines = [HEADER.format(days=_days(result)), ""]
    if result.revenue > 0:
        lines.append(REVENUE_LINE.format(revenue=rubles(result.revenue)))
        lines.append("")

    if result.deviation:
        lines.append(LEAD_DEVIATION)
    elif result.enough:
        lines.append(LEAD_QUIET)
    else:
        lines.append(
            LEAD_YOUNG.format(
                have=_weeks_word(result.have), needed=_weeks_word(result.needed)
            )
        )
    lines.append("")

    for number, leak in enumerate(result.leaks, start=1):
        lines.append(leak_line(number, leak))

    if result.lines:
        lines.extend(["", MODULES_HEAD, ""])
        for item in result.lines:
            lines.append(MODULE_LINE.format(title=item.title, line=item.line))

    lines.extend(["", FOOTER])
    if result.repeat and result.done_at:
        lines.extend(["", REPEAT_NOTE.format(when=str(result.done_at)[:10])])
    return "\n".join(lines)


# --- команда -----------------------------------------------------------------


async def diagnostic_command(
    update: Any,
    context: Any,
    *,
    path: str | Path | None = None,
    http: Any = None,
) -> None:
    """`/diagnostic`: бесплатный разбор прошлой недели, один раз на кабинет.

    Отказы отвечаются сразу, а сам разбор ставится в очередь: за ним надо
    сходить в Wildberries, а из хендлера туда не ходят. `http` принимается
    только ради теста, который следит, что транспорт остался нетронутым.
    """
    message = update.effective_message
    client_id = client_id_of(update, path)
    if message is None or client_id is None:
        return

    reason = diagnostic.availability(client_id, path=path)
    if reason == diagnostic.REPEAT:
        done = diagnostic.previous(client_id, path=path)
        # Разбор был, а цифр больше нет: это отдельный ответ, а не старая копия.
        text = report_text(done) if done is not None else refusal_text(diagnostic.GONE)
        await message.reply_text(text, parse_mode=ParseMode.HTML)
        return
    if reason != diagnostic.OK:
        await message.reply_text(refusal_text(reason), parse_mode=ParseMode.HTML)
        return

    diagnostic.request(client_id, path=path)


# --- доставка ----------------------------------------------------------------


def make_delivery(app: Any, path: str | Path | None = None):
    """Чем агент отправляет готовый разбор. Текст собирается здесь."""

    async def deliver(client_id: int, result: diagnostic.Diagnostic) -> None:
        row = db.admin_repo(path).client(client_id)
        if row is None:
            logger.warning("некому отправить диагностику: клиента %s нет", client_id)
            return
        await app.bot.send_message(
            chat_id=int(row["telegram_id"]),
            text=report_text(result),
            parse_mode=ParseMode.HTML,
        )

    return deliver


def register(app, *, path: str | Path | None = None) -> None:
    """Хендлер ставит себя сам: bot/app.py никто не трогает."""
    diagnostic.set_delivery(make_delivery(app, path))
    diagnostic.register_jobs(path=path)
    app.add_handler(
        CommandHandler(
            "diagnostic", functools.partial(diagnostic_command, path=path)
        )
    )
