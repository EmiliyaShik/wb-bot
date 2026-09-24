"""Воронка: команда `/funnel` и доставка отчёта.

Тексты живут здесь, рядом с хендлером, и только по-русски. Считает всё
`agents.funnel`, здесь его результат превращается в строки, понятные селлеру:
«из ста зашедших в карточку до корзины дошли десять», а не `to_cart=10.0`.

Пять правил, которые видно в коде.

Первое: в Wildberries отсюда никто не ходит. Кнопка периода ставит задачу в
очередь и молчит, «принято» клиенту говорит сама очередь.

Второе: доступ к платному модулю проверяет `require_module`, своей проверки
здесь нет, а `callback_data` приходит от клиента и подделывается свободно.

Третье: этапов четыре, и об этом говорится спокойно и один раз. Показов у
Wildberries нет ни в каком виде, и четыре этапа это не урезанная версия, а
всё, что вообще существует. Пятый этап, видимость в поиске, появляется только
у кабинетов с подпиской Джем, и он назван тем, что он есть: вероятностью в
процентах, а не количеством показов. Отсутствие Джема это нормальное
состояние, и пугающих слов про него здесь нет ни одного.

Четвёртое: подсказка это наблюдение плюс предположение, и предположение
названо предположением. Строка подсказки одна на бота и живёт в
`agents.funnel.ADVICE`: та же самая уезжает в книгу Excel.

Пятое: чего нет, о том говорится вслух. Истории мало - сказано, сколько
суток накоплено. У товара дыры в собранных сутках - сказано, сколько таких
товаров и почему они так вышли. Истории нет вовсе - сказано, появится она
или нет: без категории токена «Аналитика» суточную воронку забрать нечем, и
«подождите пару дней» в этом случае было бы обещанием, которое не сбудется
никогда.

Разметку в сообщении ставит только бот. Название товара пишет сам селлер в
кабинете Wildberries, оттуда оно и приезжает в строку отчёта, а сводка уходит
с `ParseMode.HTML`: одна угловая скобка в названии, и отчёта клиент не увидит
вовсе. Поэтому подстановка идёт через общий `bot.texts.fill`.
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

from agents import funnel
from core import queue
from bot.handlers.tariffs import client_id_of, require_module
from bot.texts import Safe, fill

logger = logging.getLogger(__name__)

PREFIX = "funnel:"

BUTTONS = (
    ("week", "Последняя неделя"),
    ("month", "Месяц"),
    ("quarter", "Квартал"),
)

ASK_PERIOD = (
    "🔻 <b>Воронка</b>\n\n"
    "Покажу путь покупателя по каждому товару: зашёл в карточку, положил в "
    "корзину, заказал, выкупил. Сравню с прошлым периодом и назову шаг, на "
    "котором люди стали теряться чаще.\n\n"
    "За какой период посчитать?"
)

# Истории нет вовсе: бот только что познакомился с кабинетом. Обещание
# «через пару дней» тут уместно ровно в одном случае: сбор правда идёт.
NO_HISTORY = (
    "🔻 <b>Воронка</b>\n\n"
    "Сравнивать пока не с чем: суточной истории ещё не накопилось.\n\n"
    "Wildberries отдаёт воронку по дням только за последнюю неделю, поэтому "
    "бот забирает её каждый день и хранит у себя. Сбор уже идёт: он начинается "
    "сразу после подключения кабинета и не ждёт оплаты. Через пару дней "
    "сравнение появится, а дальше будет становиться глубже."
)

# Истории нет и не будет: в токене нет категории «Аналитика». Говорить такому
# клиенту «подождите пару дней» значит обещать то, чего не случится никогда:
# без категории Wildberries не отдаёт суточную воронку вовсе, и ждать он может
# месяцами. Соседний модуль «Реклама» ведёт себя так же: называет категорию.
NO_CATEGORY_YET = (
    "🔻 <b>Воронка</b>\n\n"
    "⚠️ Ждать нечего: суточная история не копится и сама не появится. В токене "
    "нет категории «Аналитика», а воронку по дням Wildberries отдаёт только по "
    "ней.\n\n"
    "Что сделать: откройте кабинет WB, отметьте категорию «Аналитика», "
    "выпустите токен заново и пришлите его командой /connect. С этого дня "
    "история начнёт копиться, и через пару дней появится сравнение."
)

# Кабинет не подключён вовсе: копить историю не из чего.
NO_CABINET = (
    "🔻 <b>Воронка</b>\n\n"
    "Кабинет Wildberries ещё не подключён, поэтому суточной истории нет и "
    "взяться ей неоткуда.\n\n"
    "Пришлите токен командой /connect, и обязательно с категорией "
    "«Аналитика»: именно по ней Wildberries отдаёт воронку по дням. Сбор "
    "начнётся сразу после подключения и не будет ждать оплаты."
)

# Что показать вместо обещания, когда истории нет. Ключи ставит агент.
NO_HISTORY_TROUBLE = {
    funnel.NO_CATEGORY: NO_CATEGORY_YET,
    funnel.NO_CABINET: NO_CABINET,
}

# История есть, а сравнивать нечего: ни у одного товара не набралось ни суток,
# ни заказов.
NOTHING_TO_COMPARE = (
    "🔻 <b>Воронка: {title}</b>\n\n"
    "Данные есть, а сравнивать пока нечего: ни у одного товара не набралось "
    "столько собранных суток и заказов, чтобы процентам можно было верить.\n\n"
    "Это не поломка: на нескольких заказах один отказ покупателя уводит "
    "конверсию вдвое, и такую «просадку» показывать было бы враньём. "
    "Соберётся больше дней и заказов - сравнение появится само."
)

HEADER = "🔻 <b>Воронка: {title}</b>\n{period}\nСравниваю с {past}"

SHORTENED = (
    "\n\nПериод укорочен: накопленной истории меньше, чем вы просили, поэтому "
    "сравниваю {days} и столько же до них. История копится каждый день, и "
    "дальше период дотянется до полного сам."
)

STEPS = (
    "\n\n<b>Шаги</b>\n"
    "Зашли в карточку: {opens} (было {opens_was})\n"
    "Положили в корзину: {carts} (было {carts_was})\n"
    "Заказали: {orders} (было {orders_was})\n"
    "Выкупили: {buyouts} (было {buyouts_was})"
)

RATES = (
    "\n\n<b>Конверсии</b>\n"
    "Из карточки в корзину: {cart} (было {cart_was})\n"
    "Из корзины в заказ: {order} (было {order_was})\n"
    "Из заказа в выкуп: {buyout} (было {buyout_was})"
)

DROP = (
    "\n\n🔻 <b>Просел шаг «{stage}»</b>\n"
    "Было {was}, стало {now}, то есть на {points} п.п. меньше.\n"
    "{advice}"
)

NO_DROP = (
    "\n\n✅ Ни один шаг не просел сильнее порога: покупатели идут по карточке "
    "так же, как в прошлый раз. Сами цифры при этом стоит смотреть: "
    "стабильная конверсия это не обязательно хорошая конверсия."
)

WORST = "\n\n<b>Где просело сильнее всего</b>"

WORST_LINE = "{number}. {name} ({nm}): {stage}, было {was}, стало {now}"

# Пятый этап. Он есть не у всех, и это нормальное состояние кабинета.
FOUR_STEPS = (
    "\n\nЭтапов четыре, и это не урезанная воронка: показов Wildberries не "
    "отдаёт никому и ни в каком виде. Первое, что он знает о покупателе, это "
    "что карточку уже открыли. Пятый шаг, видимость в поиске, он показывает "
    "только кабинетам с подпиской Джем, и это отдельная услуга Wildberries, а "
    "не настройка бота."
)

FIVE_STEPS = (
    "\n\n👁 <b>Видимость в поиске</b>\n"
    "Пятый шаг есть: у вас подписка Джем, и Wildberries отдаёт видимость по "
    "{count}. Это не показы и не штуки: видимость это вероятность в процентах, "
    "что покупатель увидит карточку в поиске, и считается она по средней "
    "позиции."
)

VISIBILITY_FELL = (
    "\nВидимость заметно упала у {count}. Wildberries считает её изменение "
    "сам, в процентах: сильнее всего просело у товара {name} ({nm}), на "
    "{value}.\n{advice}"
)

VISIBILITY_STEADY = "\nЗаметного падения видимости нет."

# Оговорка про дыры. Её селлер должен узнать из отчёта, а не гадать, почему за
# вторник данные есть, а за среду нет.
HOLES = (
    "\n\n<b>Почему в отчёте не все товары</b>\n"
    "Сравниваю {count}. Ещё по {partial} история собрана не за каждые сутки, и "
    "сравнивать их было бы нечестно: меньше собранных дней это меньше заходов "
    "и заказов, а не упавшая конверсия.\n"
    "Так выходит из-за самого Wildberries: воронку по дням он отдаёт не больше "
    "чем по 20 товарам за запрос и не чаще трёх запросов в минуту, поэтому за "
    "один заход бот забирает {limit} самых оборотистых товаров кабинета. У "
    "медленного товара состав меняется день ото дня, и в истории появляются "
    "пропуски. Все товары и число собранных по каждому суток есть в файле."
)

QUIET = (
    "\n\nЕщё {count} отложил в сторону: заказов за период меньше {orders}, и "
    "проценты на таких числах скачут сами по себе."
)

# Видимости не будет, и причина у этого бывает разная. Ни одна из них не
# поломка: воронка из четырёх этапов работает полностью.
TROUBLE = {
    funnel.NO_CATEGORY: (
        "\n\nПятый шаг, видимость в поиске, посмотреть не вышло: в токене нет "
        "категории «Аналитика». Она же нужна и самой воронке, так что "
        "отметьте её в кабинете WB и пришлите токен заново командой /connect."
    ),
    funnel.UNAVAILABLE: (
        "\n\nЗа видимостью в поиске сходить не вышло: Wildberries не ответил. "
        "На четыре основных шага это не влияет, они посчитаны полностью."
    ),
}

FILE_NOTE = (
    "\n\nПолная таблица в файле: все товары с обоими периодами, разбивка по "
    "дням и лист «Методология»."
)

CENT = Decimal("0.01")

# Сколько знаков названия товара помещается в строку. Название придумывает сам
# селлер, и на Wildberries оно бывает в сотню знаков, а в той же строке стоят
# ещё этап и два процента. Это факт про ширину экрана телефона, а не настройка
# владельца, поэтому число стоит здесь, а не в config.toml.
NAME_LIMIT = 22

# Знак среза. Не тире: длинных тире в текстах бота нет.
CUT = "…"


def _count(value: Any) -> str:
    """Штуки с разделителем тысяч."""
    return f"{int(value or 0):,}".replace(",", " ")


def _percent(value: Decimal | None) -> str:
    """Процент без лишних нулей: 15% и 2,4%."""
    if value is None:
        return "нет данных"
    text = f"{Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP):.2f}"
    return f"{text.rstrip('0').rstrip('.').replace('.', ',')}%"


def _points(value: Decimal) -> str:
    text = f"{Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP):.2f}"
    return text.rstrip("0").rstrip(".").replace(".", ",")


def _day(value: Any) -> str:
    """ГГГГ-ММ-ДД в привычное ДД.ММ.ГГГГ."""
    parts = str(value)[:10].split("-")
    return ".".join(reversed(parts)) if len(parts) == 3 else str(value)


def _days_word(count: int) -> str:
    """«7 суток» с правильным окончанием: читатель это селлер, а не программист."""
    count = int(count)
    tail = count % 10
    hundred = count % 100
    if tail == 1 and hundred != 11:
        return f"{count} сутки"
    return f"{count} суток"


def _goods(count: int) -> str:
    """«по 1 товару» и «по 5 товарам»: дательный падеж, других форм тут нет."""
    count = int(count)
    if count % 10 == 1 and count % 100 != 11:
        return f"{count} товару"
    return f"{count} товарам"


def _of_goods(count: int) -> str:
    """«у 1 товара» и «у 5 товаров»: родительный падеж."""
    count = int(count)
    if count % 10 == 1 and count % 100 != 11:
        return f"{count} товара"
    return f"{count} товаров"


def _items(count: int) -> str:
    count = int(count)
    tail = count % 10
    hundred = count % 100
    if tail == 1 and hundred != 11:
        return f"{count} товар"
    if tail in (2, 3, 4) and hundred not in (12, 13, 14):
        return f"{count} товара"
    return f"{count} товаров"


def _short(text: str, nm_id: int) -> str:
    """Название в ширину строки. Без названия товар зовётся своим артикулом."""
    name = " ".join(str(text or "").split())
    if not name:
        return f"артикул {nm_id}"
    if len(name) <= NAME_LIMIT:
        return name
    cut = name[:NAME_LIMIT].rstrip()
    head, space, _ = cut.rpartition(" ")
    if space and len(head) >= NAME_LIMIT // 2:
        cut = head
    return cut.rstrip(" ,.;:") + CUT


def _worst_line(number: int, item: Any) -> Safe:
    """Одна строка списка просевших товаров. Название пишет селлер, не бот."""
    drop = item.drop
    return fill(
        WORST_LINE,
        number=number,
        name=_short(item.title, item.nm_id),
        nm=item.nm_id,
        stage=drop.title,
        was=_percent(drop.was),
        now=_percent(drop.now),
    )


def keyboard() -> InlineKeyboardMarkup:
    """Кнопки периода. Года здесь нет: суточную историю бот копит сам."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(title, callback_data=f"{PREFIX}{key}")]
            for key, title in BUTTONS
        ]
    )


def _visibility_block(report: Any) -> Safe:
    """Пятый этап или спокойное объяснение, почему их четыре."""
    if not report.jem:
        text = Safe(FOUR_STEPS)
        if report.trouble in TROUBLE:
            text = Safe(text + TROUBLE[report.trouble])
        return text
    known = [item for item in report.compared if item.visibility is not None]
    text = fill(FIVE_STEPS, count=_goods(len(known)))
    fell = report.visibility_fell
    if not fell:
        return Safe(text + VISIBILITY_STEADY)
    worst = fell[0]
    return Safe(
        text
        + fill(
            VISIBILITY_FELL,
            count=_of_goods(len(fell)),
            name=_short(worst.title, worst.nm_id),
            nm=worst.nm_id,
            value=_percent(abs(worst.visibility.dynamics or Decimal("0"))),
            advice=funnel.ADVICE[funnel.VISIBILITY],
        )
    )


def summary_text(report: Any) -> Safe:
    """Сводка сообщением. Полная таблица уходит файлом."""
    if report.span <= 0:
        return Safe(NO_HISTORY_TROUBLE.get(report.trouble, NO_HISTORY))
    if report.empty:
        return fill(NOTHING_TO_COMPARE, title=report.title)

    now, past = report.now, report.past
    period = f"{_day(report.date_from)} - {_day(report.date_to)}"
    was = f"{_day(report.past_from)} - {_day(report.past_to)}"
    text = fill(HEADER, title=report.title, period=period, past=was)
    if report.shortened:
        text = Safe(text + fill(SHORTENED, days=_days_word(report.span)))

    text = Safe(
        text
        + fill(
            STEPS,
            opens=_count(now.opens),
            opens_was=_count(past.opens),
            carts=_count(now.carts),
            carts_was=_count(past.carts),
            orders=_count(now.orders),
            orders_was=_count(past.orders),
            buyouts=_count(now.buyouts),
            buyouts_was=_count(past.buyouts),
        )
    )
    text = Safe(
        text
        + fill(
            RATES,
            cart=_percent(now.to_cart),
            cart_was=_percent(past.to_cart),
            order=_percent(now.to_order),
            order_was=_percent(past.to_order),
            buyout=_percent(now.to_buyout),
            buyout_was=_percent(past.to_buyout),
        )
    )

    drop = report.drop
    if drop is None:
        text = Safe(text + NO_DROP)
    else:
        text = Safe(
            text
            + fill(
                DROP,
                stage=drop.title,
                was=_percent(drop.was),
                now=_percent(drop.now),
                points=_points(drop.points),
                advice=drop.advice,
            )
        )

    troubled = report.troubled
    if troubled:
        text = Safe(text + WORST)
        text = Safe(
            text
            + "\n"
            + "\n".join(
                _worst_line(number, item)
                for number, item in enumerate(troubled[: funnel.TOP_SIZE], 1)
            )
        )

    text = Safe(text + _visibility_block(report))

    partial = report.partial
    if partial:
        text = Safe(
            text
            + fill(
                HOLES,
                count=_items(len(report.compared)),
                partial=_goods(len(partial)),
                limit=funnel.daily_articles(),
            )
        )
    quiet = report.quiet
    if quiet:
        text = Safe(
            text + fill(QUIET, count=_items(len(quiet)), orders=funnel.min_orders())
        )
    return Safe(text + FILE_NOTE)


# --- команда и кнопки --------------------------------------------------------


async def funnel_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    path: str | Path | None = None,
) -> None:
    """`/funnel`: спрашивает период. Считать тут нечего, это делает очередь."""
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
    """Кнопка периода: ставит задачу в очередь и отвечает сразу."""
    query = update.callback_query
    if query is None:
        return
    period = str(query.data or "")[len(PREFIX) :]
    if period not in dict(BUTTONS):
        await query.answer()
        return

    client_id = client_id_of(update, path)
    if client_id is None:
        await query.answer()
        return

    task = funnel.request_report(client_id, period, path=path)
    await query.answer("Принято" if task.created else queue.ALREADY_QUEUED)


# --- доставка готового отчёта ------------------------------------------------


def make_sender(app: Any, path: str | Path | None = None):
    """Чем агент отдаёт готовый отчёт: сводка сообщением, таблица файлом."""
    from core import db

    async def send(client_id: int, report: Any, data: bytes) -> None:
        row = db.admin_repo(path).client(client_id)
        if row is None:
            logger.warning("некому отправить отчёт по воронке: клиента %s нет", client_id)
            return
        chat_id = int(row["telegram_id"])
        await app.bot.send_message(
            chat_id=chat_id, text=summary_text(report), parse_mode=ParseMode.HTML
        )
        if not report.empty:
            await app.bot.send_document(
                chat_id=chat_id, document=data, filename=funnel.file_name(report)
            )

    return send


def register(app, *, path: str | Path | None = None) -> None:
    """Хендлер ставит себя сам: bot/app.py никто не трогает."""
    funnel.set_sender(make_sender(app, path))
    funnel.register_jobs()

    guard = require_module(funnel.MODULE, path=path)
    app.add_handler(
        CommandHandler("funnel", guard(functools.partial(funnel_command, path=path)))
    )
    app.add_handler(
        CallbackQueryHandler(
            guard(functools.partial(period_chosen, path=path)), pattern=f"^{PREFIX}"
        )
    )
