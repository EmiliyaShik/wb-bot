"""Уведомление об оплате от Prodamus: слова и ответ, решение принимает core.

Здесь нет ни проверки подписи, ни выдачи доступа: и то и другое живёт в
`core/billing/payform.py`, рядом с правилами платежей. Этот файл отвечает на
три вопроса: что сказать клиенту, что сказать владельцу и каким кодом
ответить Prodamus.

Приёма самого запроса тут тоже нет. Функция получает уже разобранные поля
формы и подпись из заголовка `Sign`, а каким сервером они получены, ей
неважно: пока у бота нет постоянного адреса в сети, эту часть нечем
включить, и привязываться к будущему способу запуска рано.

Коды ответа выбраны по документации Prodamus. Он повторяет уведомление, пока
не получит 200, и это хорошо ровно в одном случае: когда повтор может
помочь. Подпись не сошлась - повтор не поможет никогда, это не ошибка связи;
секретного ключа нет - поможет, как только владелец его задаст. Всё, что мы
поняли и обработали, получает 200, даже отменённый заказ и дубль: иначе
Prodamus будет стучаться с ним до скончания века.

Разметку в сообщениях ставит только бот: номер заказа и суммы приезжают из
чужого запроса и уходят в сообщение с `ParseMode.HTML`, поэтому подстановка
идёт через общий `bot.texts.fill`.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from bot.handlers.billing import tell_client, tell_owner
from bot.texts import Safe, fill
from core import billing, config
from core.billing import payform

logger = logging.getLogger(__name__)

PAID_CLIENT = (
    "✅ <b>Оплата прошла, модуль «{title}» включён.</b>\n"
    "Доступ действует до {until}.\n\n"
    "Чек придёт на телефон или почту, которые вы указали при оплате. "
    "Что бот умеет в этом модуле, покажет команда <code>/help</code>."
)

PAID_OWNER = (
    "💳 Оплата прошла, доступ открыт автоматически.\n"
    "Клиент {client_id}, {title}, {period}, {amount}.\n"
    "Заказ <code>{order}</code>, работает до {until}."
)

# Денег меньше цены. Решение тут не автоматическое нарочно: вернуть деньги
# или включить за неполную оплату, знает только владелец.
SHORT_OWNER = (
    "⚠️ Оплата меньше цены, доступ не открыт.\n"
    "Заказ <code>{order}</code>: пришло {paid}, цена {due}.\n"
    "Включить всё равно: <code>/grant {client_id} {module} {months} карта {order}</code>"
)

# Номер начинается как наш, а дальше не разбирается. Текст именно про это:
# чужие номера сюда больше не попадают, у них своя дорога и своё молчание.
BAD_ORDER_OWNER = (
    "⚠️ Оплата прошла, а номер заказа <code>{order}</code> разобрать не вышло.\n"
    "Начало номера наше, то есть платёж наш, но кому и что включать, по нему "
    "непонятно. Доступ не открыт, платёж виден в кабинете Prodamus."
)

BAD_SIGN_OWNER = (
    "⛔️ Пришло уведомление об оплате с неверной подписью, доступ не открыт.\n"
    "Так и задумано: подпись это единственное, чем настоящее уведомление "
    "отличается от поддельного. Если оплата правда была, она видна в кабинете "
    "Prodamus, и доступ включается командой <code>/grant</code>."
)

# Тестовая оплата владельца. Говорится вслух, что доступ не выдан: иначе
# следующий вопрос будет «а кому он включился».
TEST_OWNER = (
    "🧪 Тестовая оплата дошла до бота, подпись верна.\n"
    "Заказ <code>{order}</code>, сумма {amount}. Денег не списано, доступ "
    "никому не открыт.\n"
    "Проверено всё, кроме самих денег: ссылка, подпись Prodamus, приём "
    "уведомления и разбор номера заказа."
)

NO_SECRET_OWNER = (
    "⛔️ Пришло уведомление об оплате, а секретный ключ платёжной страницы "
    "боту не задан (переменная <code>PRODAMUS_SECRET</code>). Проверить "
    "подпись нечем, доступ не открыт."
)

# Что уходит в ответ Prodamus. Тело он не читает, это для нас и для логов.
ANSWERS = {
    payform.OK: (200, "ok"),
    payform.DUPLICATE: (200, "duplicate"),
    payform.NOT_PAID: (200, "not paid"),
    payform.BAD_ORDER: (200, "unknown order"),
    payform.FOREIGN: (200, "not our order"),
    payform.SHORT_PAY: (200, "short payment"),
    payform.TEST_OK: (200, "test ok"),
    payform.BAD_SIGN: (403, "bad sign"),
    payform.NO_SECRET: (503, "no secret"),
}


def _title(module: str) -> str:
    info = config.modules().get(module)
    return (info.title if info and info.title else module) or module


def _day(moment: datetime | None) -> str:
    return moment.strftime("%d.%m.%Y") if moment else "неизвестно"


def _money(amount: Any) -> str:
    """Деньги строкой тем же способом, что и везде в боте."""
    from bot.handlers.tariffs import rubles

    return rubles(amount) if amount is not None else "неизвестно"


def client_text(result: payform.Result) -> Safe:
    """Что видит клиент. Пишется только тому, кому открыли доступ."""
    order = result.order
    return fill(
        PAID_CLIENT,
        title=_title(order.module) if order else "",
        until=_day(result.until),
    )


def owner_text(result: payform.Result) -> Safe | None:
    """Что видит владелец. None означает «говорить не о чем».

    Молчим о трёх вещах: об отменённом заказе (клиент просто передумал, и
    письмо об этом Prodamus владельцу и так пришлёт), о дубле уведомления
    (деньги те же, доступ тот же, сообщение было бы вторым о том же самом)
    и о чужом заказе. Последнее про то, что платёжная страница бывает одна
    на несколько ботов, и тогда уведомление приходит каждому из них:
    сообщение на каждую чужую продажу владелец перестал бы читать через
    неделю, а вместе с ними перестал бы читать и настоящие.
    """
    reason, order = result.reason, result.order
    if reason in (payform.NOT_PAID, payform.DUPLICATE, payform.FOREIGN):
        return None
    if reason == payform.BAD_SIGN:
        return Safe(BAD_SIGN_OWNER)
    if reason == payform.NO_SECRET:
        return Safe(NO_SECRET_OWNER)
    if reason == payform.TEST_OK:
        return fill(TEST_OWNER, order=result.order_num, amount=_money(result.paid))
    if reason == payform.BAD_ORDER:
        return fill(BAD_ORDER_OWNER, order=result.order_num)
    if reason == payform.SHORT_PAY and order is not None:
        return fill(
            SHORT_OWNER,
            order=result.order_num,
            paid=_money(result.paid),
            due=_money(result.due),
            client_id=order.client_id,
            module=order.module,
            months=order.months,
        )
    if reason == payform.OK and order is not None:
        return fill(
            PAID_OWNER,
            client_id=order.client_id,
            title=_title(order.module),
            period=billing.months_words(order.months),
            amount=_money(result.paid),
            order=result.order_num,
            until=_day(result.until),
        )
    return None


# Разговоры, которые ещё идут. Множество, а не одна ссылка: уведомлений
# может прийти несколько подряд, а задача без ссылки на неё может быть
# убрана сборщиком мусора на середине.
_talking: set[asyncio.Task] = set()


def pending() -> tuple[asyncio.Task, ...]:
    """Незаконченные разговоры об оплатах. Их ждут при остановке бота."""
    return tuple(_talking)


def _say_later(coro: Any) -> None:
    task = asyncio.ensure_future(coro)
    _talking.add(task)
    task.add_done_callback(_talking.discard)


async def _talk(app: Any, result: payform.Result) -> None:
    """Рассказывает об оплате клиенту и владельцу. Сбой гасится в журнал."""
    bot = getattr(app, "bot", None)
    if bot is None:
        return
    try:
        if result.granted and result.order is not None:
            await tell_client(bot, result.order.client_id, client_text(result))
        words = owner_text(result)
        if words is not None:
            await tell_owner(bot, words)
    except Exception:  # noqa: BLE001 - молчание Telegram не отменяет оплату
        logger.exception("не удалось рассказать об оплате по заказу %s", result.order_num)


def make_receiver(app: Any, path: str | Path | None = None):
    """Чем принимать уведомление. Возвращает (код ответа, тело).

    Подпись и выдача считаются сразу: это хеш и несколько строк в своей
    базе, то есть доли секунды. Разговор с Telegram уходит в фон, и вот
    почему. Перед ботом на хостинге стоит прокси со своим таймаутом, а
    Telegram умеет молчать минутами (`bot/netwatch.py` про это целиком).
    Дожидаться его значило бы не ответить Prodamus вовремя, а тот повторит
    уведомление, и на повторе бот промолчит: выдача-то уже не новая, а
    дубль. Клиент остался бы с оплаченным доступом и без единого слова.

    Отвечать 200 до выдачи тоже нельзя: Prodamus больше не повторит, и
    сорвавшаяся выдача никогда бы не повторилась.
    """

    async def receive(fields: Mapping[str, Any], sign: str) -> tuple[int, str]:
        try:
            result = payform.apply(fields, sign, path=path)
        except Exception:  # noqa: BLE001 - своя ошибка это не ответ «принято»
            logger.exception("не удалось разобрать уведомление об оплате")
            return 500, "error"
        _say_later(_talk(app, result))
        return ANSWERS.get(result.reason, (200, result.reason))

    return receive
