"""Прибыльность артикулов: команда `/profit` и доставка отчёта.

Тексты живут здесь, рядом с хендлером, и только по-русски. Считает всё
`agents.profit`, здесь его результат превращается в строки, понятные селлеру:
«кто кормит, а кто ест», а не «margin=41.25».

Три правила, которые видно в коде.

Первое: в WB отсюда никто не ходит. Кнопка периода ставит задачу в очередь и
молчит, «принято» клиенту говорит сама очередь. За расходом рекламы идёт
обработчик задачи: у `fullstats` лимит 3 запроса в минуту, а повтор при
недоступности Wildberries живёт только в очереди.

Второе: доступ к платному модулю проверяет `require_module`, своей проверки
здесь нет. Прибыльность входит в модуль «Финансы».

Третье: чего нет, о том говорится вслух. Нет себестоимости - артикул назван
в отдельном блоке, и прибыль по нему не посчитана. Нет категории
«Продвижение» - в колонке «реклама» стоит «нет данных», а прибыль посчитана
без рекламы, и в сообщении написано, почему. Режим налогообложения не выбран
- налог не считается, и сказано, что прибыль показана до него.

Четвёртое: про налог бот говорит оценкой и никогда «к уплате». Главное в
этом блоке не процент, а база: налог на УСН «доходы» считается с того, что
заплатил покупатель, а не с того, что перечислил Wildberries, поэтому обе
суммы стоят рядом и разница названа словами. Про налог по товарам в файле
сказано по-разному у двух режимов, и это не вежливость: на «доходах» это
налог самого товара, на «доходы минус расходы» наша раскладка кабинетного
налога, и назвать второе первым значило бы соврать уверенным голосом.

Пятое: суммы в сводке и в книге это одни и те же числа, а не два похожих
расчёта. Прибыль за период и прибыль после налога берутся у агента теми же
свойствами, из которых книга собирает итоговую строку. Селлер читает сводку
с телефона, потом открывает файл, и два разных числа под одним названием
стоят ему доверия к обоим: такой случай тут уже был.

Разметку в сообщении ставит только бот. Артикул продавца селлер вписывает
руками в кабинете Wildberries, оттуда он и приезжает в строку топа, а
сводка уходит с `ParseMode.HTML`: одна угловая скобка в артикуле, и отчёта
клиент не увидит вовсе. Поэтому подстановка идёт через общий
`bot.texts.fill`, а граница стоит на ней, а не у каждого поля.
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

from agents import finance, profit, storage
from core import queue
from core import tax as tax_module
from bot.handlers import period
from bot.handlers.tariffs import client_id_of, require_module
from bot import texts
from bot.texts import Safe, fill

logger = logging.getLogger(__name__)

PREFIX = "profit:"

# Подписи относительных кнопок считаются из PERIODS: число дней в подписи и
# есть то, чего кнопке «Месяц» не хватало. Рядом стоят названные месяцы, и
# считаются они иначе, точно по датам.
BUTTONS = tuple(
    (name, f"Последние {finance.days_words(days)}")
    for name, days in profit.PERIODS.items()
)

ASK_PERIOD = (
    "📈 <b>Прибыль по артикулам</b>\n\n"
    "Покажу, какие товары зарабатывают, а какие съедают деньги: выручка, "
    "себестоимость, комиссия, логистика, хранение, штрафы, реклама и возвраты "
    "по каждому артикулу.\n\n"
    "За какой период посчитать?\n\n"
    "Кнопки «Последние ...» считают целыми неделями Wildberries: в отчёт "
    "попадает каждая неделя, которая этот отрезок задела.\n"
    "Месяц и свои даты считают точно по датам операций: ровно запрошенные "
    "дни, ни дня соседнего месяца. Это то, что сходится с бухгалтерией."
)

LONG_PERIOD = (
    "Длинный период Wildberries отдаёт медленно, это надолго. Соберу и "
    "пришлю, закрывать бот не нужно."
)

DATES_ACCEPTED = (
    "Принято: {period}. Посчитаю точно по датам операций и пришлю отчёт."
)

EXACT_NOTE = (
    "\n\nПериод посчитан точно по датам операций: в него вошло ровно "
    "запрошенное, ни дня соседнего месяца."
)

CLIPPED = (
    "\n\n⚠️ Не целиком вошли в период недели: {weeks}. Сверить их нечем: "
    "готовые итоги Wildberries есть только на неделю целиком, поэтому цифры по "
    "этим неделям посчитаны по датам операций, но итогами кабинета не "
    "подтверждены."
)

NO_DATA = (
    "За этот период считать нечего: Wildberries не отдал отчётов о реализации. "
    "Так бывает, когда продаж ещё не было или кабинет подключён недавно. "
    "Попробуйте период побольше."
)

HEADER = "📈 <b>Прибыль по артикулам: {title}</b>\n{period}"

FEED = "\n<b>Кто кормит</b>"
EAT = "\n<b>Кто ест</b>"

NO_COSTS = (
    "\n\n⚠️ Артикулов без себестоимости: {count}. Прибыль по ним не посчитана, "
    "они собраны в отдельном блоке файла. Пришлите себестоимость командой "
    "/costs, и они встанут в общий ряд."
)

LOSSES = "\n\n🔻 Убыточных артикулов: {count}. Все они есть в файле, блок «{sheet}»."

NO_COMMISSION = (
    "\n\n⚠️ У {count} артикулов комиссия Wildberries неизвестна: их строки "
    "сохранены без исходных полей отчёта. Прибыль по ним не посчитана, "
    "подставлять вместо комиссии соседнее поле я не стал. Запросите этот "
    "период заново, и строки перезапишутся."
)

# Компенсация. Владелец увидела «продано 1 шт» у товара, которого давно нет на
# остатках, и спросила, откуда он взялся. Ответ обязан быть в сообщении, а не
# только в файле: иначе товар просто исчезнет из отчёта, и вопрос повторится.
COMPENSATION = (
    "\n\n📦 Компенсации Wildberries: {amount} за {units} шт. Это возмещение за "
    "товар, который покупатель вернул или потерял: деньги пришли, а товар выбыл, "
    "а не продался, поэтому на остатках его нет. В выручку, в проданные штуки и "
    "в прибыль я их не считаю, иначе товар без цены выглядел бы проданным. "
    "В «к перечислению» отчёта /finance эти деньги входят, а по каждому товару "
    "они названы в файле, блок «{sheet}»."
)

COMPENSATION_COST = (
    " Себестоимость выбывшего товара {amount}, в прибыль она тоже не вошла: "
    "почему, написано на листе «Методология»."
)

# Рекламный счёт. Раньше он лежал внутри обезлички и вычитался вторым разом:
# деньги крупные, и молчание о них читалось бы как потеря. Поэтому сумма
# названа, и сказано, какой её частью реклама вычтена по товарам.
AD_DEDUCTION = (
    "\n\n📣 За рекламу Wildberries удержал {amount}: выставил счёт рекламного "
    "кабинета и забрал его из отчёта о реализации. Расход рекламы по товарам я "
    "считаю не по счёту, а по статистике кампаний: только она знает, какой "
    "товар рекламировали, а счёт приходит общей суммой по кабинету. "
    "В файле эти деньги названы в блоке «{block}» листа «{sheet}»."
)

# Две цифры про одни деньги. Та же разница, что в /ads, и по той же причине:
# статистика считает отработавшую рекламу по дням, а счёт Wildberries
# выставляет своим числом и удерживает той неделей, которой дойдёт до отчёта.
AD_DEDUCTION_GAP = (
    " Статистика кампаний за период даёт {amount}: счёт выставляется своим "
    "числом и доходит до отчёта своей неделей, поэтому числа и не обязаны "
    "совпасть. Ту же разницу показывает /ads."
)

# Что сделано с разницей. Главный вопрос селлера: почему расход рекламы в
# отчёте не равен счёту Wildberries. Остаток счёта из расходов не выброшен,
# иначе отчёт обещал бы прибыль, которой нет.
AD_DEDUCTION_REST = (
    " Из счёта по товарам вычтено {covered}, а остаток {rest} вычтен вместе с "
    "расходами без артикула: эти деньги Wildberries правда удержал, а дни "
    "показов за них в период не попали. Дважды реклама не вычитается."
)

AD_DEDUCTION_WHOLE = (
    " Счёт сошёлся со статистикой целиком, поэтому второй раз в расходы он не "
    "идёт: реклама вычтена один раз, расходом по товарам."
)

# Хранение и приёмка по товарам. Это второй отчёт Wildberries, и молчать о
# том, что он другой, нельзя: селлер сложит столбец, сверит с /finance и не
# получит того же числа. Поэтому названы оба числа, разница и источник.
REPORT_SOURCE = (
    "\n\n📦 {title} по товарам: {amount}. Это {source}, отдельный отчёт "
    "Wildberries: в нём сумма стоит по каждому товару и по каждым суткам, "
    "поэтому в файле есть колонка «{column}». В отчёте о реализации та же "
    "статья приходит общей суммой по кабинету, и за период это {billed}."
)

REPORT_GAP = (
    " Разница {gap}: два отчёта сойтись не обязаны, Wildberries этого и не "
    "обещает. Причины он называет сам: перерасчёты задним числом, сутки против "
    "недель отчёта о реализации, скидка лояльности отдельным полем и разные "
    "сервисы за двумя отчётами. Ту же разницу показывают сверка /finance и две "
    "цифры расхода /ads."
)

REPORT_MATCH = " Разница в пределах рубля, то есть два отчёта сошлись."

REPORT_PARTIAL = (
    " Суток периода собрано {days} из {total}: окно одного запроса у этого "
    "отчёта короткое, историю я добираю по ночам. Несобранные сутки уже "
    "ставлю в работу, в следующем отчёте за этот период они будут."
)

REPORT_REST = (
    " Из статьи отчёта о реализации по товарам вычтено {covered}, а остаток "
    "{rest} вычтен вместе с расходами без артикула: эти деньги Wildberries "
    "правда удержал. Дважды не вычитается ничего."
)

REPORT_WHOLE = (
    " Второй отчёт объяснил статью целиком, поэтому второй раз в расходы она "
    "не идёт."
)

REPORT_FINANCE = (
    " В /finance ничего не подменено: там стоит сумма из отчёта о реализации, "
    "по нему вы сверяетесь с Wildberries."
)

FACELESS = "\n\n🧾 Расходы без артикула (обезличка): {amount}."

# Из чего эти деньги. «Расходы без артикула: 900 ₽» без состава читается как
# отговорка, а селлеру важно знать, что там хранение, а не штрафы.
FACELESS_PARTS = " Из чего они складываются: {parts}."

FACELESS_ALL_UNSHARED = (
    " Эти статьи Wildberries по товарам не разносит вовсе, поэтому отдельных "
    "колонок под них в файле нет."
)

FACELESS_UNSHARED = (
    " Часть из них Wildberries по товарам не разносит вовсе, поэтому отдельных "
    "колонок под них в файле нет: {names}."
)

FACELESS_SPREAD = (
    " По артикулам я разнёс их сам, пропорционально выручке: правило записано "
    "на листе «Методология»."
)

FACELESS_LEFT = (
    " Не разнесённый остаток {amount} в прибыль не вошёл: разносить его не по чему."
)

ADS_MISSING = {
    profit.ADS_NO_CATEGORY: (
        "\n\n⚠️ Расход рекламы посчитать не вышло: в токене нет категории "
        "«Продвижение». В колонке «реклама» стоит «нет данных», прибыль "
        "посчитана без неё, поэтому она завышена. Выдайте категорию в кабинете "
        "WB и подключите токен заново командой /connect."
    ),
    profit.ADS_UNAVAILABLE: (
        "\n\n⚠️ Wildberries не отдал статистику рекламы. В колонке «реклама» "
        "стоит «нет данных», прибыль посчитана без неё, поэтому она завышена. "
        "Запросите отчёт ещё раз чуть позже."
    ),
    profit.ADS_NOT_REQUESTED: (
        "\n\n⚠️ Расход рекламы в этот отчёт не попал. В колонке «реклама» стоит "
        "«нет данных», прибыль посчитана без неё."
    ),
}

# Налог. Ради чего этот блок вообще написан: селлер на УСН «доходы» видит
# поступление от Wildberries и считает процент с него, а налоговая считает с
# того, что заплатил покупатель. Поэтому обе суммы стоят рядом и разница
# названа словами, а не оставлена на вычитание в уме.
TAX_HEAD = "\n🧾 <b>Налог: {mode}, ставка {rate}%</b>"

TAX_BASE = (
    "\nДоходы, с которых считается налог: {income}"
    "\nЭто то, что заплатил покупатель. Wildberries перечислил вам {transferred}, "
    "разница {gap} осталась у него: комиссия, логистика, хранение и скидка "
    "площадки. Налог берётся с полной цены продажи, а не с поступления на счёт. "
    "Возвраты базу уменьшают."
)

TAX_EXPENSES = "\nРасходы, принятые в базу: {amount}\nБаза налога: {base}"

TAX_EXPENSES_NOTE = (
    "\nВ расходы я взял себестоимость проданного товара, удержания Wildberries и "
    "рекламу. Штрафы не взял: такие расходы обычно не принимают. Выставленный "
    "счёт за рекламу внутри удержаний не считается второй раз: реклама стоит в "
    "расходах один раз, иначе база вышла бы меньше настоящей, а налог по ней "
    "ниже. Страховые "
    "взносы, зарплату, аренду и всё, что идёт мимо кабинета, я не вижу, их "
    "добавляйте сами."
)

TAX_GAPS = "\n⚠️ Расходы посчитаны не полностью: {what}. Налог в этой оценке завышен."

# Чего не хватило. Ключи знает core.tax, слова живут здесь.
TAX_MISSING = {
    tax_module.MISSING_COST: "себестоимость есть не по всем артикулам",
    tax_module.MISSING_ADS: "расход рекламы получить не вышло",
}

TAX_LOSS = (
    "\nЗа этот период расходы больше доходов, поэтому оценка налога нулевая. "
    "Налог считается по итогам года, и там картина может быть другой."
)

TAX_MINIMUM = (
    "\nОтметка минимального налога: 1% от доходов это {amount}. За год на этом "
    "режиме платят не меньше неё. Правило годовое, а отчёт за период, поэтому "
    "честно посчитать минимальный налог я не могу и показываю отметку для "
    "сравнения."
)

TAX_BELOW_MINIMUM = " Оценка за этот период ниже отметки."

TAX_AMOUNT = "\nНалог, оценка: {amount}"

TAX_PROFIT = "\nПрибыль после налога: <b>{amount}</b>"

# Почему это число не равно «прибыль минус оценка налога» строкой выше. Оно
# равно итогу листа «Прибыль по артикулам», и равно нарочно: селлер читает
# сводку с телефона, потом открывает файл, и два разных числа под одним
# названием стоят ему доверия к обоим. Но тогда вычитание в сводке не
# сходится, и промолчать об этом нельзя: селлер вычтет и решит, что бот
# ошибся. Оговорка короткая и появляется только там, где разница есть, а
# копеечную разницу округления агент сюда не отдаёт вовсе.
TAX_PROFIT_PART = (
    "\nИз прибыли вычтено не всё: {taken} это налог тех товаров, по которым "
    "прибыль посчитана. Остальные {rest} приходятся на то, по чему прибыли "
    "нет вовсе, и вычитать их не из чего. В файле, в листе «{sheet}», итог "
    "такой же."
)

# Про колонки в файле. Два режима ведут себя по-разному, и смешивать их под
# одной формулировкой нельзя: на «доходах» налог товара это его собственный
# налог, на «доходы минус расходы» наша раскладка кабинетного налога. Селлер
# читает это перед тем, как открыть файл, поэтому сказано здесь, а не только
# на листе «Методология».
TAX_PER_ARTICLE_EXACT = (
    "\nВ файле налог стоит у каждого товара: на этом режиме он считается с "
    "выручки, поэтому налог товара это ставка от его выручки за вычетом "
    "возвратов, а не доля общей суммы. Рядом прибыль после налога по каждому "
    "товару. В топе ниже прибыль до налога."
)

TAX_PER_ARTICLE_SPREAD = (
    "\nВ файле налог стоит у каждого товара, но на этом режиме это моя "
    "раскладка, а не налог именно этого товара: база считается по кабинету "
    "целиком, и по товарам я разложил её пропорционально выручке. Так и "
    "подписана колонка. Рядом прибыль после налога по каждому товару. В топе "
    "ниже прибыль до налога."
)

TAX_NOTE = (
    "\nЭто оценка, а не налог к уплате: я не веду налоговый учёт, не знаю про "
    "ваши страховые взносы и вычеты и бухгалтера не заменяю."
)

TAX_OFF = (
    "\n🧾 <b>Налог не учтён</b>\n"
    "Прибыль выше показана до налога: режим налогообложения не выбран, и "
    "подставлять вместо него ноль я не стал. На УСН «доходы» это ещё несколько "
    "процентов от выручки, и они уходят. Выберите режим и ставку в /settings, и "
    "налог встанет отдельной строкой."
)

UNVERIFIED = (
    "\n\n⚠️ Эти недели Wildberries не подтвердил итогами отчёта: {weeks}. "
    "Считайте цифры по ним предварительными."
)

# Незнакомый тип документа. Wildberries вправе завести новый тип в любой
# день, и бот такую строку не выбрасывает: деньги из неё в раскладку входят,
# а штуки нет, потому что продажа это или нет, неизвестно. Прежде об этом
# знал только журнал владельца, и селлер видел прибыль с тихо непосчитанными
# штуками. Теперь тип назван поимённо: по нему и разбираются.
UNKNOWN_DOCS = (
    "\n\n❓ В отчёте Wildberries встретился тип документа, которого бот не "
    "знает: {types}. Деньги по таким строкам я посчитал, а штуки нет: "
    "продажа это или нет, по типу не понять, а угадать значило бы наврать в "
    "себестоимости и в маржинальности. Напишите владельцу командой "
    "/paysupport, если видите эту строку: разобрать новый тип недолго."
)

INCOMPLETE = (
    "\n\n🚫 Данные за эти недели неполные: {weeks}. Выгрузка упёрлась в предел "
    "страниц, и прибыль по ним занижена. Сторож расходов такие недели из "
    "сравнения выбрасывает, а я считаю: это не спор, а два разных взгляда на "
    "одну неделю. Чтобы цифры сошлись с кабинетом, запросите период короче."
)

FILE_NOTE = (
    "\n\nПолная таблица в файле: все артикулы, у каждого налог и прибыль после "
    "него, итог по кабинету, отдельный блок «убыточные, компенсации, обезличка, "
    "без себестоимости», лист «Налог» и лист «Методология»."
)

CENT = Decimal("0.01")

# Сколько знаков названия помещается в строку топа. Название карточки пишет
# сам продавец, и на Wildberries оно бывает в сотню знаков («Наматрасник на
# резинке 160х200 непромокаемый с бортами»), а в той же строке стоят ещё
# прибыль, маржа и доля: без предела строка разъезжается на телефоне на три
# строки, и топ перестаёт читаться. Это факт про ширину экрана, а не
# настройка владельца, поэтому число стоит здесь, а не в config.toml.
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


def _percent(value: float | None) -> str:
    if value is None:
        return "нет данных"
    text = f"{Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP):.2f}"
    return f"{text.rstrip('0').rstrip('.').replace('.', ',')}%"


def _day(text: str) -> str:
    """ГГГГ-ММ-ДД в привычное ДД.ММ.ГГГГ."""
    parts = str(text)[:10].split("-")
    return ".".join(reversed(parts)) if len(parts) == 3 else str(text)


def _week_days(week: Any) -> str:
    """Неделя обеими датами: ДД.ММ.ГГГГ - ДД.ММ.ГГГГ."""
    return f"{_day(week.date_from)} - {_day(week.date_to)}"


def _short(text: str) -> str:
    """Название в ширину строки: режем по слову, на месте среза знак.

    Обрывок в одно слово оставляем как есть: половина слова читается хуже
    целого, но лучше пустого места.
    """
    name = " ".join(str(text or "").split())
    if len(name) <= NAME_LIMIT:
        return name
    cut = name[:NAME_LIMIT].rstrip()
    head, space, _ = cut.rpartition(" ")
    if space and len(head) >= NAME_LIMIT // 2:
        cut = head
    return cut.rstrip(" ,.;:") + CUT


def _name(item: Any) -> str:
    """Чем товар назван в строке топа: название и артикулы рядом.

    Артикул из строки не исчезает никогда: по нему селлер ищет товар в
    кабинете и в файле, а названия у десятка карточек бывают почти
    одинаковые. Названия нет вовсе (карточку удалили или за ней ещё не
    ходили) - остаются одни артикулы, как было раньше.
    """
    marks = [str(part) for part in (item.nm_id, item.vendor_code) if part]
    if not item.title:
        return f"{marks[0]} ({marks[1]})" if len(marks) > 1 else (marks[0] if marks else "")
    return f"{_short(item.title)} ({', '.join(marks)})"


def _line(number: int, item: Any) -> Safe:
    """Одна строка топа: товар, прибыль, маржинальность, доля.

    И название карточки, и артикул продавца селлер пишет сам в кабинете
    Wildberries, поэтому имя уходит в сообщение через ту же подстановку, что
    и всё остальное.
    """
    name = _name(item)
    parts = [
        fill(
            "{number}. {name}: {profit}",
            number=number,
            name=name,
            profit=_money(item.profit),
        )
    ]
    if item.margin is not None:
        parts.append(fill("маржа {percent}", percent=_percent(item.margin)))
    if item.share is not None:
        parts.append(fill("доля {percent}", percent=_percent(item.share)))
    return Safe(", ".join(parts))


def _faceless_parts(report: Any) -> Safe:
    """Состав обезлички словами: что именно Wildberries отдал без артикула.

    Раньше отчёт говорил только «расходы без артикула такие-то», и селлеру
    оставалось гадать, штрафы это или хранение. Состав считает агент, здесь
    он превращается в строку.
    """
    items = report.faceless_parts
    if not items:
        return Safe("")
    # Печатается та часть статьи, которая правда попала в обезличку: у
    # рекламного счёта это его неразобранный остаток, а не весь счёт. Иначе
    # сложение строк не сошлось бы с названной выше суммой.
    parts = ", ".join(
        fill("{title} {amount}", title=item.title.lower(), amount=_money(item.in_pool))
        for item in items
    )
    text = fill(FACELESS_PARTS, parts=Safe(parts))
    unshared = report.unshared_items
    if unshared and len(unshared) == len(items):
        # Перечислять те же статьи второй раз незачем: они и есть весь состав.
        text = Safe(text + FACELESS_ALL_UNSHARED)
    elif unshared:
        names = ", ".join(item.title.lower() for item in unshared)
        text = Safe(text + fill(FACELESS_UNSHARED, names=names))
    return Safe(text)


def report_sources_block(report: Any) -> Safe:
    """Хранение и приёмка по товарам: два числа про одни деньги и разница.

    Молчать здесь нельзя. Селлер сложит колонку в файле, сверит её с /finance
    и получит другое число: если бот об этом не сказал, он решит, что одна из
    цифр выдумана. Поэтому названы оба числа, разница, источник и то, что в
    /finance не подменено ничего.
    """
    text = Safe("")
    for source in report.report_sources:
        # Денег нет ни в одном из двух источников: абзац про ноль рублей в
        # сводке с телефона стоит дороже самого нуля. Колонка в книге при этом
        # остаётся: настоящий ноль это ответ.
        if not source.notable:
            continue
        text = Safe(
            text
            + fill(
                REPORT_SOURCE,
                title=source.title,
                amount=_money(source.total),
                source=source.source,
                column=source.column,
                billed=_money(source.billed),
            )
        )
        text = Safe(
            text
            + (
                REPORT_MATCH
                if source.matches
                else fill(REPORT_GAP, gap=_money(source.gap))
            )
        )
        if not source.complete:
            text = Safe(
                text
                + fill(REPORT_PARTIAL, days=source.days, total=source.days_total)
            )
        rest = source.uncovered
        if rest > 0:
            text = Safe(
                text
                + fill(
                    REPORT_REST,
                    covered=_money(source.replaced),
                    rest=_money(rest),
                )
            )
        elif rest == 0:
            text = Safe(text + REPORT_WHOLE)
        text = Safe(text + REPORT_FINANCE)
    return text


def tax_block(report: Any) -> Safe:
    """Налог словами: база, сумма и прибыль после него.

    Режим не выбран - здесь не ноль и не пустота, а прямая фраза о том, что
    прибыль показана до налога, и дорога к настройке. Считает всё агент,
    отсюда в базу и в WB не ходят.
    """
    estimate = report.tax
    if estimate is None:
        return Safe(TAX_OFF)
    rule = estimate.rule
    text = Safe(
        fill(TAX_HEAD, mode=rule.title, rate=rule.rate_text)
        + fill(
            TAX_BASE,
            income=_money(estimate.income),
            transferred=_money(estimate.transferred),
            gap=_money(estimate.gap),
        )
    )
    if estimate.expenses is not None:
        text = Safe(
            text
            + fill(
                TAX_EXPENSES,
                amount=_money(estimate.expenses),
                base=_money(estimate.base),
            )
            + TAX_EXPENSES_NOTE
        )
        if estimate.missing:
            what = ", ".join(
                TAX_MISSING[key] for key in estimate.missing if key in TAX_MISSING
            )
            text = Safe(text + fill(TAX_GAPS, what=what))
        if estimate.loss:
            text = Safe(text + TAX_LOSS)
    text = Safe(text + fill(TAX_AMOUNT, amount=_money(estimate.amount)))
    if estimate.expenses is not None:
        minimum = fill(TAX_MINIMUM, amount=_money(estimate.minimum))
        if estimate.below_minimum:
            minimum = Safe(minimum + TAX_BELOW_MINIMUM)
        text = Safe(text + minimum)
    allocation = report.tax_by_article
    per_article = TAX_PER_ARTICLE_EXACT if allocation.exact else TAX_PER_ARTICLE_SPREAD
    after = report.cabinet_after_tax
    text = Safe(text + fill(TAX_PROFIT, amount=_money(after)))
    rest = report.tax_outside_profit
    # Объяснять вычитание, которого в сводке нет, незачем: прибыль не
    # посчитана ни по одному артикулу, и на её месте стоит «нет данных».
    # Почему её нет, скажет блок про себестоимость ниже.
    if after is not None and rest is not None:
        text = Safe(
            text
            + fill(
                TAX_PROFIT_PART,
                taken=_money(report.tax_in_profit),
                rest=_money(rest),
                sheet=profit.ARTICLES_SHEET,
            )
        )
    return Safe(text + per_article + TAX_NOTE)


TAX_PREFIX = f"{PREFIX}tax:"
TAX_SKIP_DATA = f"{PREFIX}taxoff"

ASK_TAX = (
    "🧾 <b>Сначала про налог</b>\n\n"
    "Скажите, на какой системе налогообложения вы работаете, и я посчитаю "
    "налог по каждому товару. Без этого прибыль в отчёте будет до налога, а "
    "колонок с налогом в книге не будет вовсе.\n\n"
    "Спрошу один раз. Поменять режим и ставку можно в любой момент командой "
    "<code>/settings</code>."
)

TAX_SET = (
    "Записал: <b>{mode}</b>, ставка {rate}. Если ставка у вас другая, "
    "поменяйте её командой <code>/settings</code>, там весь список.\n\n"
    "Теперь выберите период."
)

TAX_SKIPPED = (
    "Хорошо, считаю прибыль до налога. Передумаете, включите налог командой "
    "<code>/settings</code>.\n\n"
    "Теперь выберите период."
)

TAX_SKIP_LABEL = "Без налога, считать до него"


def tax_keyboard() -> InlineKeyboardMarkup:
    """Режимы налога и отказ. Список режимов и названия берутся из `core.tax`.

    Своего списка здесь нет намеренно: он один и лежит там же, где проверка.
    Разойдись они, кнопка предлагала бы режим, которого расчёт не знает.
    """
    rows = [
        [
            InlineKeyboardButton(
                tax_module.MODE_TITLES[mode], callback_data=f"{TAX_PREFIX}{mode}"
            )
        ]
        for mode in tax_module.MODES
    ]
    rows.append([InlineKeyboardButton(TAX_SKIP_LABEL, callback_data=TAX_SKIP_DATA)])
    return InlineKeyboardMarkup(rows)


def tax_needed(client_id: int | None, path: Any = None) -> bool:
    """Спросить ли про налог перед отчётом.

    Спрашиваем один раз и только у того, кто режим не выбирал: отказ это тоже
    ответ, и переспрашивать его перед каждым отчётом значит навязываться.
    Выбранный режим виден в `/settings`, и там же он меняется.
    """
    if client_id is None:
        return False
    if tax_module.rule_of(client_id, path=path).known:
        return False
    return not tax_module.was_asked(client_id, path=path)


def keyboard(today: Any = None) -> InlineKeyboardMarkup:
    """Кнопки периода: относительные, названные месяцы и свои даты."""
    rows = [
        [InlineKeyboardButton(title, callback_data=f"{PREFIX}{key}")]
        for key, title in BUTTONS
    ]
    return InlineKeyboardMarkup(rows + period.month_rows(PREFIX, today=today))


def summary_text(report: Any) -> Safe:
    """Топ 5 и антитоп 5 в сообщении. Полная таблица уходит файлом."""
    if report.empty:
        return Safe(NO_DATA)

    period = f"{_day(report.date_from.isoformat())} - {_day(report.date_to.isoformat())}"
    lines = [
        fill(HEADER, title=report.title, period=period),
        "",
        # Число берётся у агента, а не складывается здесь: ровно это же стоит
        # в итоге книги. Прибыль не посчитана ни по одному артикулу - здесь
        # «нет данных», а не ноль: ноль читается как «не заработали».
        fill("Прибыль за период: <b>{amount}</b>", amount=_money(report.cabinet_profit)),
        fill(
            "Выручка за вычетом возвратов: {amount}",
            amount=_money(report.total_revenue),
        ),
        fill("Реклама: {amount}", amount=_money(report.total_ad_spend)),
        fill("Артикулов в отчёте: {count}", count=len(report.articles)),
        # Налог стоит сразу за суммой прибыли, а не в конце: «чистая прибыль»
        # без него это не деньги в кармане, и узнавать об этом через десять
        # строк поздно.
        tax_block(report),
    ]

    if report.top:
        lines.append(FEED)
        lines += [_line(number, item) for number, item in enumerate(report.top, 1)]
    if report.bottom:
        lines.append(EAT)
        lines += [_line(number, item) for number, item in enumerate(report.bottom, 1)]

    text = Safe("\n".join(lines))
    if report.losses:
        text = Safe(
            text + fill(LOSSES, count=len(report.losses), sheet=profit.PROBLEMS_SHEET)
        )
    if report.without_cost:
        text = Safe(text + fill(NO_COSTS, count=len(report.without_cost)))
    if report.without_commission:
        text = Safe(text + fill(NO_COMMISSION, count=len(report.without_commission)))
    if report.compensations:
        text = Safe(
            text
            + fill(
                COMPENSATION,
                amount=_money(report.total_compensation),
                units=report.total_compensation_units,
                sheet=profit.PROBLEMS_SHEET,
            )
        )
        if report.total_compensation_cost:
            text = Safe(
                text
                + fill(
                    COMPENSATION_COST, amount=_money(report.total_compensation_cost)
                )
            )
    # Хранение и приёмка по своим отчётам стоят до разговора об обезличке: это
    # те самые деньги, которые раньше целиком лежали в ней, и читать про
    # остаток удобнее уже зная, откуда он взялся.
    text = Safe(text + report_sources_block(report))
    if report.ad_deductions != 0 and not report.ad_deductions_counted:
        text = Safe(
            text
            + fill(
                AD_DEDUCTION,
                amount=_money(report.ad_deductions),
                block=profit.AD_DEDUCTION_BLOCK,
                sheet=profit.PROBLEMS_SHEET,
            )
        )
        spend = report.total_ad_spend
        if spend is not None:
            text = Safe(text + fill(AD_DEDUCTION_GAP, amount=_money(spend)))
            # Остаток счёта, который статистика не объяснила. Ноль значит, что
            # счёт заменён целиком, и об этом тоже сказано словами: иначе
            # селлер не поймёт, вычтена реклама один раз или полтора.
            rest = report.ad_uncovered
            if rest > 0:
                text = Safe(
                    text
                    + fill(
                        AD_DEDUCTION_REST,
                        covered=_money(report.ad_replaced),
                        rest=_money(rest),
                    )
                )
            elif rest == 0:
                text = Safe(text + AD_DEDUCTION_WHOLE)
    # Обезличка называется и когда она отрицательная. Так бывает, когда
    # Wildberries вернул по кабинету больше, чем удержал (добровольная выплата
    # за испорченный товар приходит той же строкой удержания с минусом), и
    # промолчать о такой сумме значит оставить селлера с необъяснённой
    # прибавкой к прибыли. Состав строкой ниже и говорит, откуда минус.
    if report.unallocated != 0:
        text = Safe(text + fill(FACELESS, amount=_money(report.unallocated)))
        text = Safe(text + _faceless_parts(report) + FACELESS_SPREAD)
        if report.unallocated_left:
            text = Safe(
                text + fill(FACELESS_LEFT, amount=_money(report.unallocated_left))
            )
    if not report.ads.available:
        text = Safe(
            text
            + ADS_MISSING.get(report.ads.reason, ADS_MISSING[profit.ADS_NOT_REQUESTED])
        )
    # Неделя называется обеими датами, а не одной. Сторож расходов неполные
    # недели пропускает, а тут они посчитаны: без точных границ два ответа
    # выглядели бы как противоречие, а не как разные взгляды на одну неделю.
    if report.exact:
        text = Safe(text + EXACT_NOTE)
    for template, weeks in (
        (INCOMPLETE, report.incomplete),
        (UNVERIFIED, report.unverified),
        (CLIPPED, report.clipped),
    ):
        if weeks:
            text = Safe(
                text
                + fill(template, weeks="; ".join(_week_days(week) for week in weeks))
            )
    # Имя типа документа придумал Wildberries, и в сообщение оно уходит через
    # ту же подстановку, что и всё остальное чужое.
    if report.unknown_doc_types:
        text = Safe(
            text + fill(UNKNOWN_DOCS, types="; ".join(report.unknown_doc_types))
        )
    return Safe(text + FILE_NOTE)


# --- команда и кнопки --------------------------------------------------------


async def profit_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    path: str | Path | None = None,
) -> None:
    """`/profit`: спрашивает период. Считать тут нечего, это делает очередь."""
    message = update.effective_message
    if message is None:
        return
    # Про налог спрашиваем до периода, а не после отчёта: без режима отчёт
    # считается честно, но до налога, и узнавал об этом селлер, только открыв
    # книгу и не найдя там колонки.
    if tax_needed(client_id_of(update, path), path):
        await message.reply_text(
            ASK_TAX, parse_mode=ParseMode.HTML, reply_markup=tax_keyboard()
        )
        return
    await message.reply_text(ASK_PERIOD, parse_mode=ParseMode.HTML, reply_markup=keyboard())


async def period_chosen(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    path: str | Path | None = None,
) -> None:
    """Кнопка периода: ставит задачу в очередь и отвечает сразу.

    Кнопок три вида: относительная, названный месяц (приезжает датами) и
    «Свои даты». Период из кнопки проверяет функция, а не клавиатура:
    `callback_data` присылает клиент, и подделать его ничего не стоит.
    """
    query = update.callback_query
    if query is None:
        return
    tail = str(query.data or "")[len(PREFIX) :]

    if tail == "taxoff" or tail.startswith("tax:"):
        await _tax_chosen(update, query, tail, path=path)
        return

    if tail == period.CUSTOM:
        await query.answer()
        await period.ask(update, context, profit.MODULE)
        return

    if tail.startswith(period.MARK):
        window = period.period_of_callback(tail)
        if window is None:
            await query.answer()
            if query.message is not None:
                await query.message.reply_text(
                    period.stale_text("profit"), parse_mode=ParseMode.HTML
                )
            return
    elif tail in profit.PERIODS:
        window = finance.period_of(tail)
    else:
        await query.answer()
        return

    client_id = client_id_of(update, path)
    if client_id is None:
        await query.answer()
        return

    # Задача могла уже стоять: тогда честный ответ «уже считаю», а не второе
    # «принято». Текст берётся у очереди, чтобы всплывающее окно и сообщение
    # не рассказывали клиенту разное.
    task = profit.request_report(client_id, window, path=path)
    await query.answer("Принято" if task.created else queue.ALREADY_QUEUED)
    if _long(window) and query.message is not None:
        await query.message.reply_text(LONG_PERIOD)


async def _tax_chosen(update: Update, query: Any, tail: str, *, path: Any = None) -> None:
    """Ответ про налог, а следом тот же вопрос про период.

    Режим приходит в `callback_data`, то есть от клиента, и проверяет его
    `core.tax.set_rule`, а не эта кнопка: чужой режим сюда не доедет. Ставка
    ставится та, что по умолчанию у режима, и сообщение говорит, где её
    поменять: выбор ставки в этом месте растянул бы разговор на три шага
    ради числа, которое у большинства и так по умолчанию.
    """
    client_id = client_id_of(update, path)
    if client_id is None:
        await query.answer()
        return
    if tail == "taxoff":
        tax_module.mark_asked(client_id, path=path)
        answer = Safe(TAX_SKIPPED)
    else:
        try:
            rule = tax_module.set_rule(client_id, tail[len("tax:"):], path=path)
        except ValueError:
            await query.answer()
            return
        tax_module.mark_asked(client_id, path=path)
        answer = fill(TAX_SET, mode=rule.title, rate=rule.rate_text + "%")
    await query.answer()
    if query.message is None:
        return
    await query.message.reply_text(
        answer, parse_mode=ParseMode.HTML, reply_markup=keyboard()
    )


def _long(window: Any) -> bool:
    """Стоит ли предупредить, что это надолго. Решают сутки, а не имя кнопки.

    Ручной период бывает длиной в год, и молчать о нём только потому, что
    кнопка называется иначе, значило бы обмануть того, кто ждёт дольше всех.
    """
    return window.days > profit.PERIODS["quarter"]


async def dates_chosen(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    window: Any,
    *,
    path: str | Path | None = None,
) -> None:
    """Период прислали текстом: ставим задачу и отвечаем сообщением.

    Доступ проверяется ещё раз, обёрткой `require_module` при регистрации:
    пока человек набирал даты, оплаченные дни могли кончиться.
    """
    message = update.effective_message
    client_id = client_id_of(update, path)
    if message is None or client_id is None:
        return
    task = profit.request_report(client_id, window, path=path)
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
    if _long(window):
        await message.reply_text(LONG_PERIOD)


# --- доставка готового отчёта ------------------------------------------------


def make_sender(app: Any, path: str | Path | None = None):
    """Чем агент отдаёт готовый отчёт: сводка сообщением, таблица файлом."""
    from core import db

    async def send(client_id: int, report: Any, data: bytes) -> None:
        row = db.admin_repo(path).client(client_id)
        if row is None:
            logger.warning("некому отправить отчёт о прибыли: клиента %s нет", client_id)
            return
        chat_id = int(row["telegram_id"])
        await app.bot.send_message(
            chat_id=chat_id,
            text=texts.clip(summary_text(report)),
            parse_mode=ParseMode.HTML,
        )
        if not report.empty:
            await app.bot.send_document(
                chat_id=chat_id, document=data, filename=profit.file_name(report)
            )

    return send


def register(app, *, path: str | Path | None = None) -> None:
    """Хендлер ставит себя сам: bot/app.py никто не трогает."""
    profit.set_sender(make_sender(app, path))
    profit.register_jobs()
    # Хранение и приёмку по товарам копит агент 8, своей команды у него нет:
    # его данные это колонки в этой книге. Поэтому его работы ставит тот
    # хендлер, которому они нужны.
    storage.register_jobs()

    guard = require_module(profit.MODULE, path=path)
    # Ввод дат приходит текстом, мимо кнопки, поэтому доступ к модулю
    # проверяется и на нём той же обёрткой.
    period.set_owner(profit.MODULE, guard(functools.partial(dates_chosen, path=path)))
    app.add_handler(
        CommandHandler("profit", guard(functools.partial(profit_command, path=path)))
    )
    app.add_handler(
        CallbackQueryHandler(
            guard(functools.partial(period_chosen, path=path)), pattern=f"^{PREFIX}"
        )
    )
