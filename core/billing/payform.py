"""Ссылка на оплату картой: платёжная страница Prodamus.

Ссылка собирается на каждого клиента своя, а не берётся готовой из кабинета
Prodamus. Готовая («общая платёжная ссылка») знает только один товар и одну
цену, а у нас пять модулей на три срока, и по оплате всё равно не было бы
видно, **кому** включать доступ. Поэтому в адрес едут название модуля, сумма
за выбранный срок, номер заказа и внутренний номер клиента.

Три вещи, которые тут важнее остального.

**Цена в ссылке это обычный параметр адреса, и подделать её может кто
угодно.** Значит ссылка ничего не доказывает: открывать доступ по факту
перехода нельзя. Доступ включается по оплате, о которой сообщает Prodamus, и
сумму при этом сверяют. Пока это делает владелец руками командой `/grant`.

**`paid_content` обязателен.** Платёжная страница говорит об этом прямым
текстом: без заполненного поля «доступы к материалам» Prodamus не пробьёт
чек, и по 54-ФЗ его придётся пробивать своей онлайн-кассой, которой у нас
нет. Поэтому поле заполняется всегда, даже когда сказать в нём нечего, кроме
адреса бота.

**Номер заказа это будущий `payment_ref`.** По нему `core.access` отличает
повторную выдачу от новой, поэтому он обязан быть одним и тем же у одной
покупки и разным у разных. Минуты в нём хватает: два нажатия подряд это одна
покупка, а не две.

HTTP отсюда никто не делает: функция собирает строку. Единственное место с
запросами наружу в проекте остаётся `core/wbapi`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlencode

from core import config

# Адрес платёжной страницы и адрес бота. Оба в окружении: страница своя у
# каждого продавца, а имя бота меняется вместе с самим ботом.
URL_VAR = "PRODAMUS_URL"
BOT_VAR = "BOT_LINK"

# Что Prodamus показывает клиенту после оплаты и из чего пробивает чек.
# Коротко нарочно: весь адрес ссылки уходит в кнопку Telegram, и каждая
# лишняя строка кириллицы это втрое больше знаков в адресе.
PAID_CONTENT = "Доступ к модулю «{title}» в боте «Рентген Продаж»: {bot}"

# Наименование товара в чеке. Видит его и клиент, и налоговая.
PRODUCT = "Рентген Продаж, модуль {title}, {period}"


def payform_url() -> str:
    """Адрес платёжной страницы. Пустая строка означает «оплата не настроена»."""
    return config.env(URL_VAR)


def bot_link() -> str:
    return config.env(BOT_VAR)


def ready() -> bool:
    """Можно ли выдавать ссылку на оплату картой."""
    return bool(payform_url())


def order_id(
    client_id: int, module: str, months: int, *, now: datetime | None = None
) -> str:
    """Номер заказа, он же будущий `payment_ref`.

    В нём читается, кто и что купил: владелец видит номер в уведомлении
    Prodamus и этим же номером выдаёт доступ, не сверяясь ни с чем ещё.
    Минута в конце делает номер разным у разных покупок и одинаковым у
    двух нажатий одной.
    """
    moment = now or datetime.now()
    return f"RP-{int(client_id)}-{module}-{int(months)}-{moment:%Y%m%d%H%M}"


@dataclass(frozen=True)
class Payment:
    """Готовая оплата: адрес ссылки, номер заказа, сумма и название товара."""

    url: str
    order_id: str
    amount: Decimal
    product: str


def payment(
    client_id: int,
    module: str,
    months: int,
    *,
    title: str,
    period: str,
    now: datetime | None = None,
) -> Payment | None:
    """Ссылка на оплату модуля за выбранный срок. None, если оплата не настроена.

    Название модуля и период приходят параметрами, а не берутся из конфига:
    те же слова стоят в сообщении клиенту, и расходиться им нельзя.
    """
    base = payform_url()
    if not base:
        return None
    amount = config.price_decimal(module, months)
    number = order_id(client_id, module, months, now=now)
    product = PRODUCT.format(title=title, period=period)
    params = {
        "do": "pay",
        "order_id": number,
        # Сквозной параметр: Prodamus вернёт его в уведомлении об оплате, и по
        # нему будет видно, чей доступ включать, когда выдача станет
        # автоматической. Внутренний номер, не Telegram ID: внутренний номер
        # и так лежит в каждой строке базы, а Telegram ID это уже личные
        # данные клиента, и светить его в адресе незачем.
        "_param_client": str(int(client_id)),
        "products[0][name]": product,
        "products[0][price]": f"{amount:f}",
        "products[0][quantity]": "1",
        "products[0][type]": "service",
        "paid_content": PAID_CONTENT.format(
            title=title, bot=bot_link() or "Telegram"
        ),
    }
    # Хвост чистится до того, как выбран разделитель, а не после: адрес
    # «…/?» кончается вопросом, но параметров в нём нет, и приклеивать к
    # нему «&» значит собрать ссылку, которую Prodamus не разберёт.
    base = base.strip().rstrip("&?")
    joiner = "&" if "?" in base else "?"
    return Payment(
        url=f"{base}{joiner}{urlencode(params)}",
        order_id=number,
        amount=amount,
        product=product,
    )
