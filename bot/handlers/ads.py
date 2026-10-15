"""Реклама: команда `/ads` и доставка отчёта.

Тексты живут здесь, рядом с хендлером, и только по-русски. Считает всё
`agents.ads`, здесь его результат превращается в строки, понятные селлеру:
«заказ обошёлся в 340 ₽», а не «cpo=340».

Четыре правила, которые видно в коде.

Первое: в WB отсюда никто не ходит. Кнопка периода ставит задачу в очередь и
молчит, «принято» клиенту говорит сама очередь. У статистики рекламы лимит
3 запроса в минуту, и повтор при недоступности Wildberries живёт только там.

Второе: доступ к платному модулю проверяет `require_module`, своей проверки
здесь нет.

Третье: цифр ДРР две, и вторая всегда названа нашим расчётом. Первая это
отношение двух полей одного ответа Wildberries, вторая сшита из рекламы и
финансового отчёта. Показать сшитое число молча значит соврать уверенным
голосом, поэтому подпись стоит прямо в строке. А главное сказано отдельно:
чем ближе цифры друг к другу, тем больше товар живёт на рекламе.

Четвёртое: чего нет, о том говорится вслух. Нет категории «Продвижение» -
бот объясняет, какой именно, и куда её выдать. Нет модуля «Финансы» - второй
цифры ДРР не будет, и вместо выдуманного числа стоит объяснение.

Разметку в сообщении ставит только бот. Название кампании селлер придумывает
сам в кабинете Wildberries, оттуда оно и приезжает в строку отчёта, а сводка
уходит с `ParseMode.HTML`: одна угловая скобка в названии, и отчёта клиент
не увидит вовсе. Поэтому подстановка идёт через общий `bot.texts.fill`, а
граница стоит на ней, а не у каждого поля.
"""

from __future__ import annotations

import functools
import logging
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from agents import ads, finance
from core import queue
from bot.handlers import period
from bot.handlers.tariffs import client_id_of, require_module
from bot.texts import Safe, fill

logger = logging.getLogger(__name__)

PREFIX = "ads:"

# Подписи относительных кнопок считаются из PERIODS: число дней в подписи и
# есть то, чего кнопке «Месяц» не хватало. Названные месяцы стоят ниже и
# считаются точно по датам.
BUTTONS = tuple(
    (name, f"Последние {finance.days_words(days)}")
    for name, days in ads.PERIODS.items()
)

ASK_PERIOD = (
    "📣 <b>Реклама</b>\n\n"
    "Покажу по каждой кампании и по каждому дню: сколько ушло, сколько "
    "заказов пришло, во что обошёлся один заказ и какой получился ДРР рядом "
    "с вашей целью.\n\n"
    "За какой период посчитать?\n\n"
    "Кнопки «Последние ...» отсчитывают дни назад от сегодня. Месяц и свои "
    "даты это ровно те дни, которые вы попросили: расход и заказы бот хранит "
    "по суткам, поэтому месяц здесь выходит точным."
)

LONG_PERIOD = (
    "Квартал Wildberries отдаёт по кусочкам: не больше 31 дня и 50 кампаний "
    "за запрос, три запроса в минуту. Соберу и пришлю, закрывать бот не нужно."
)

NO_DATA = (
    "За этот период рекламы не нашлось: ни одной кампании со статистикой. "
    "Так бывает, когда кампаний не было вовсе или их удалили: Wildberries "
    "отдаёт статистику только по кампаниям завершённым, активным и стоящим "
    "на паузе."
)

TROUBLE = {
    ads.NO_CATEGORY: (
        "⚠️ Рекламу посчитать не вышло: в токене нет категории «Продвижение». "
        "Без неё Wildberries не отдаёт ни кампании, ни их статистику. "
        "Отметьте категорию в кабинете WB и пришлите токен заново командой "
        "/connect."
    ),
    ads.UNAVAILABLE: (
        "⚠️ Wildberries не отдал статистику рекламы. Показываю то, что бот "
        "успел собрать раньше. Запросите отчёт ещё раз чуть позже."
    ),
}

HEADER = "📣 <b>Реклама: {title}</b>\n{period}"

DATES_ACCEPTED = (
    "Принято: {period}. Посчитаю ровно эти дни и пришлю отчёт."
)

CLIPPED = (
    "\n\n⚠️ Вторая цифра ДРР взята из недель финансового отчёта, и не целиком "
    "в период вошли недели: {weeks}. Выручка по ним посчитана по датам "
    "операций, но итогами кабинета не подтверждена. Сам расход рекламы это не "
    "задевает, он лежит по суткам."
)

TOTALS = (
    "\nРасход: <b>{spend}</b>\n"
    "Заказов с рекламы: {orders}\n"
    "Цена заказа: {cpo}"
)

FACT = "\nФактически списано: {amount}"

# Расхождение статистики и выставленной суммы. Прятать его нельзя: селлер
# сверяет отчёт с разделом «Финансы» рекламного кабинета, и там стоит вторая
# цифра. То же правило, что в сверке финансового отчёта.
GAP = (
    "\n\n🧾 Статистика и счёт разошлись на {amount}. Это не ошибка: "
    "статистический расход Wildberries считает по показам и кликам, а "
    "списывает по документам, и часть расхода могла уйти бонусами или "
    "кэшбэком. В файле обе цифры стоят рядом по каждой кампании."
)

DRR_ADS = (
    "\n\n<b>ДРР рекламы: {value}</b>\n"
    "Расход к выручке, которую Wildberries приписал рекламе. Оба числа его, "
    "из одного ответа."
)

DRR_CABINET = (
    "\n<b>ДРР кабинета: {value}</b>\n"
    "Наш расчёт: тот же расход ко всей выручке этих товаров, вместе с теми "
    "продажами, что пришли без рекламы."
)

NO_REVENUE = (
    "\n<b>ДРР кабинета: посчитать не из чего.</b>\n"
    "Вторая цифра считается по выручке из недельных отчётов Wildberries, а их "
    "собирает модуль «Финансы». Пока его нет, остаётся первая цифра: она "
    "честная и полная, просто отвечает на вопрос поуже."
)

TARGET = "\nВаша цель: {value}. Меняется в /settings."

# Ради этой строки в отчёте и стоят две цифры рядом. Без неё селлер получил
# бы два числа и складывал бы их смысл сам.
VERDICT = {
    "ad_driven": (
        "\n\n🔴 Цифры почти сошлись: с рекламы пришло {share} выручки этих "
        "товаров. Сами они почти не продаются: выключите кампании, и выручка "
        "уйдёт вместе с ними."
    ),
    "self_selling": (
        "\n\n🟢 Цифры разошлись сильно: с рекламы пришло только {share} "
        "выручки этих товаров. Основное они продают сами, реклама добавляет "
        "сверху."
    ),
    "": (
        "\n\nСмотреть стоит не на каждую цифру по отдельности, а на то, "
        "насколько они разошлись: чем ближе они друг к другу, тем больше "
        "товар держится на рекламе. С рекламы пришло {share} его выручки."
    ),
}

OVER = "\n\n🔺 <b>Выше вашей цели: {count}</b>"

UNDER_ALL = (
    "\n\n✅ Все кампании уложились в вашу цель по ДРР. Расход при этом всё "
    "равно стоит смотреть: цель выполнена и там, где кампания просто мало "
    "тратила."
)

WEEKS = (
    "\n\nВыручка товаров взята из недельных отчётов Wildberries: {weeks}. "
    "Границы недель и границы периода не совпадают, поэтому вторая цифра "
    "считается по этим неделям целиком."
)

FILE_NOTE = (
    "\n\nПолная таблица в файле: все кампании, разбивка по дням, разбивка по "
    "артикулам и лист «Методология»."
)

CENT = Decimal("0.01")

# Сколько знаков названия кампании помещается в строку. Название придумывает
# сам селлер, и на Wildberries оно бывает в сотню знаков, а в той же строке
# стоят ещё расход, ДРР и цена заказа. Это факт про ширину экрана телефона,
# а не настройка владельца, поэтому число стоит здесь, а не в config.toml.
NAME_LIMIT = 24

# Знак среза. Не тире: длинных тире в текстах бота нет.
CUT = "…"


def _money(value: Decimal | int | None) -> str:
    """Рубли с разделителем тысяч. Копейки показываем, только если они есть.

    `float` в подписи нет намеренно: деньги в этом проекте живут целыми
    копейками и считаются в `Decimal`, и тип, который сюда не приходит,
    не должен выглядеть разрешённым.
    """
    if value is None:
        return "нет данных"
    amount = (value if isinstance(value, Decimal) else Decimal(str(value))).quantize(
        CENT, rounding=ROUND_HALF_UP
    )
    whole, _, cents = f"{amount:.2f}".partition(".")
    sign = "-" if whole.startswith("-") else ""
    digits = whole.lstrip("-")
    grouped = f"{int(digits):,}".replace(",", " ")
    tail = "" if cents == "00" else f",{cents}"
    return f"{sign}{grouped}{tail} ₽"


def _percent(value: Decimal | None) -> str:
    """Процент без лишних нулей: 15% и 2,4%."""
    if value is None:
        return "нет данных"
    text = f"{Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP):.2f}"
    return f"{text.rstrip('0').rstrip('.').replace('.', ',')}%"


def _day(text: str) -> str:
    """ГГГГ-ММ-ДД в привычное ДД.ММ.ГГГГ."""
    parts = str(text)[:10].split("-")
    return ".".join(reversed(parts)) if len(parts) == 3 else str(text)


def _week_days(week: Any) -> str:
    return f"{_day(week.date_from)} - {_day(week.date_to)}"


def _short(text: str) -> str:
    """Название в ширину строки: режем по слову, на месте среза знак."""
    name = " ".join(str(text or "").split())
    if len(name) <= NAME_LIMIT:
        return name
    cut = name[:NAME_LIMIT].rstrip()
    head, space, _ = cut.rpartition(" ")
    if space and len(head) >= NAME_LIMIT // 2:
        cut = head
    return cut.rstrip(" ,.;:") + CUT


def _campaign_line(number: int, item: Any) -> Safe:
    """Одна строка списка: кампания, расход, ДРР и цена заказа.

    Название кампании придумывает сам селлер в кабинете Wildberries, поэтому
    оно уходит в сообщение через ту же подстановку, что и всё остальное.
    Номер из строки не исчезает: по нему кампания ищется в кабинете, а
    названия у соседних кампаний бывают почти одинаковые.
    """
    parts = [
        fill(
            "{number}. {name} ({advert}): {spend}",
            number=number,
            name=_short(item.title),
            advert=item.advert_id,
            spend=_money(item.spend),
        )
    ]
    if item.drr_ads is None:
        # Расход есть, а заказов с рекламы нет: процента не существует, и
        # прочерк на его месте прочитался бы как «ноль», то есть как успех.
        parts.append(Safe("заказов с рекламы нет"))
    else:
        parts.append(fill("ДРР {value}", value=_percent(item.drr_ads)))
        parts.append(fill("заказ {value}", value=_money(item.cpo)))
    return Safe(", ".join(parts))


def keyboard(today: Any = None) -> InlineKeyboardMarkup:
    """Кнопки периода. Года здесь нет: столько рекламы Wildberries отдаёт
    неделями, а копить её бот начал не раньше подключения кабинета. По той же
    причине и ручной период у рекламы короче, чем в деньгах."""
    rows = [
        [InlineKeyboardButton(title, callback_data=f"{PREFIX}{key}")]
        for key, title in BUTTONS
    ]
    return InlineKeyboardMarkup(rows + period.month_rows(PREFIX, today=today))


def summary_text(report: Any) -> Safe:
    """Сводка сообщением. Полная таблица уходит файлом."""
    if report.empty:
        text = Safe(NO_DATA)
        if report.trouble in TROUBLE:
            text = Safe(TROUBLE[report.trouble] + "\n\n" + text)
        return text

    period = f"{_day(report.date_from.isoformat())} - {_day(report.date_to.isoformat())}"
    text = fill(HEADER, title=report.title, period=period)
    text = Safe(
        text
        + fill(
            TOTALS,
            spend=_money(report.spend),
            orders=report.orders,
            cpo=_money(report.cpo),
        )
    )
    if report.fact_known:
        text = Safe(text + fill(FACT, amount=_money(report.fact_spend)))
    if report.gap_matters:
        text = Safe(text + fill(GAP, amount=_money(abs(report.gap))))

    text = Safe(text + fill(DRR_ADS, value=_percent(report.drr_ads)))
    if report.revenue_known:
        text = Safe(text + fill(DRR_CABINET, value=_percent(report.drr_cabinet)))
    else:
        text = Safe(text + NO_REVENUE)
    text = Safe(text + fill(TARGET, value=_percent(report.target)))

    if report.revenue_known and report.ad_share is not None:
        text = Safe(
            text
            + fill(VERDICT[report.verdict], share=_percent(report.ad_share))
        )

    over = report.over_target
    if over:
        text = Safe(text + fill(OVER, count=len(over)))
        text = Safe(
            text
            + "\n"
            + "\n".join(
                _campaign_line(number, item)
                for number, item in enumerate(over[: ads.TOP_SIZE], 1)
            )
        )
    else:
        text = Safe(text + UNDER_ALL)

    if report.weeks:
        text = Safe(
            text
            + fill(WEEKS, weeks="; ".join(_week_days(week) for week in report.weeks))
        )
    # Расход рекламы точный всегда: он лежит по суткам. А вот выручка товара
    # приходит неделями финансового отчёта, и в точном периоде её крайние
    # недели обрезаны. Молчать об этом нельзя: вторая цифра ДРР считается
    # именно от неё.
    if report.clipped:
        text = Safe(
            text
            + fill(
                CLIPPED, weeks="; ".join(_week_days(week) for week in report.clipped)
            )
        )
    if report.trouble in TROUBLE:
        text = Safe(text + "\n\n" + TROUBLE[report.trouble])
    return Safe(text + FILE_NOTE)


# --- команда и кнопки --------------------------------------------------------


async def ads_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    path: str | Path | None = None,
) -> None:
    """`/ads`: спрашивает период. Считать тут нечего, это делает очередь."""
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(ASK_PERIOD, parse_mode=ParseMode.HTML, reply_markup=keyboard())


async def period_chosen(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    path: str | Path | None = None,
) -> None:
    """Кнопка периода: ставит задачу в очередь и отвечает сразу.

    Период из кнопки проверяет функция, а не клавиатура: `callback_data`
    присылает клиент. У рекламы свой предел длины (`ads.max_days`), и
    подделанный период его не обходит.
    """
    query = update.callback_query
    if query is None:
        return
    tail = str(query.data or "")[len(PREFIX) :]

    if tail == period.CUSTOM:
        await query.answer()
        await period.ask(update, context, ads.MODULE, max_days=ads.max_days())
        return

    if tail.startswith(period.MARK):
        window = period.period_of_callback(tail, max_days=ads.max_days())
        if window is None:
            await query.answer()
            if query.message is not None:
                await query.message.reply_text(
                    period.stale_text("ads"), parse_mode=ParseMode.HTML
                )
            return
    elif tail in dict(BUTTONS):
        window = finance.period_of(tail)
    else:
        await query.answer()
        return

    client_id = client_id_of(update, path)
    if client_id is None:
        await query.answer()
        return

    task = ads.request_report(client_id, window, path=path)
    await query.answer("Принято" if task.created else queue.ALREADY_QUEUED)
    if window.days > finance.PERIODS["month"] and query.message is not None:
        await query.message.reply_text(LONG_PERIOD)


async def dates_chosen(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    window: Any,
    *,
    path: str | Path | None = None,
) -> None:
    """Период прислали текстом: ставим задачу и отвечаем сообщением."""
    message = update.effective_message
    client_id = client_id_of(update, path)
    if message is None or client_id is None:
        return
    task = ads.request_report(client_id, window, path=path)
    await message.reply_text(
        fill(
            DATES_ACCEPTED,
            period=f"{_day(window.date_from.isoformat())} - "
            f"{_day(window.date_to.isoformat())}",
        )
        if task.created
        else Safe(queue.ALREADY_QUEUED),
        parse_mode=ParseMode.HTML,
    )
    if window.days > finance.PERIODS["month"]:
        await message.reply_text(LONG_PERIOD)


# --- доставка готового отчёта ------------------------------------------------


def make_sender(app: Any, path: str | Path | None = None):
    """Чем агент отдаёт готовый отчёт: сводка сообщением, таблица файлом."""
    from core import db

    async def send(client_id: int, report: Any, data: bytes) -> None:
        row = db.admin_repo(path).client(client_id)
        if row is None:
            logger.warning("некому отправить отчёт по рекламе: клиента %s нет", client_id)
            return
        chat_id = int(row["telegram_id"])
        await app.bot.send_message(
            chat_id=chat_id, text=summary_text(report), parse_mode=ParseMode.HTML
        )
        if not report.empty:
            await app.bot.send_document(
                chat_id=chat_id, document=data, filename=ads.file_name(report)
            )

    return send


def register(app, *, path: str | Path | None = None) -> None:
    """Хендлер ставит себя сам: bot/app.py никто не трогает."""
    ads.set_sender(make_sender(app, path))
    ads.register_jobs()

    guard = require_module(ads.MODULE, path=path)
    # Ввод дат приходит текстом, мимо кнопки: доступ проверяется той же
    # обёрткой, своей проверки здесь нет.
    period.set_owner(ads.MODULE, guard(functools.partial(dates_chosen, path=path)))
    app.add_handler(
        CommandHandler("ads", guard(functools.partial(ads_command, path=path)))
    )
    app.add_handler(
        CallbackQueryHandler(
            guard(functools.partial(period_chosen, path=path)), pattern=f"^{PREFIX}"
        )
    )
