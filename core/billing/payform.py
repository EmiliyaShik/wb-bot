"""Оплата картой: ссылка на платёжную страницу Prodamus и ответ оттуда.

Файл один на обе стороны разговора нарочно. Ссылку собираем мы, уведомление
об оплате приходит от Prodamus, и общего у них больше, чем различий: номер
заказа, сумма и секретный ключ. Разнести это по двум файлам значит однажды
поменять формат номера в одном и забыть про другой.

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

**Уведомление об оплате проверяется подписью, и только она открывает
доступ.** Адрес, на который Prodamus стучится, рано или поздно узнают: он
ездит в открытом виде. Единственное, чего не узнает чужой, это секретный ключ
платёжной страницы, поэтому сходится подпись - уведомление наше, не сходится
- мы о нём только рассказываем владельцу и не трогаем доступ. Ошибка в
расчёте подписи может только отвергнуть настоящее уведомление, но не принять
поддельное: несовпадение хеша работает в одну сторону.

HTTP отсюда никто не делает: тут собирается строка и разбирается готовый
словарь полей. Единственное место с запросами наружу в проекте остаётся
`core/wbapi`, а приём запроса снаружи живёт в поверхности бота.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlencode

from core import access, audit, config
from core.billing import months_to_days

logger = logging.getLogger(__name__)

# Адрес платёжной страницы и адрес бота. Оба в окружении: страница своя у
# каждого продавца, а имя бота меняется вместе с самим ботом.
URL_VAR = "PRODAMUS_URL"
BOT_VAR = "BOT_LINK"
# Секретный ключ платёжной страницы: им подписаны уведомления об оплате.
SECRET_VAR = "PRODAMUS_SECRET"

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
    return f"{ORDER_PREFIX}{int(client_id)}-{module}-{int(months)}-{moment:%Y%m%d%H%M}"


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


# --- уведомление об оплате ---------------------------------------------------

# Статусы Prodamus. Доступ открывает только первый.
PAID = "success"
CANCELED = "order_canceled"
DENIED = "order_denied"

# Способ оплаты в журнале выдачи. Тот же, что набирает владелец руками.
CARD_METHOD = "card"

# Чем кончился разбор уведомления. Текстов тут нет: их пишет поверхность бота,
# а здесь только причина, по которой доступ открыт или не открыт.
OK = "ok"
DUPLICATE = "duplicate"
BAD_SIGN = "bad_sign"
NOT_PAID = "not_paid"
BAD_ORDER = "bad_order"
FOREIGN = "foreign"
SHORT_PAY = "short_pay"
NO_SECRET = "no_secret"

# Начало номера заказа: по нему оплата этого бота отличается от оплаты
# соседнего. Платёжная страница у них может быть одна на всех, и тогда
# уведомление об оплате приходит каждому боту: чужое надо узнать и промолчать,
# а не поднимать тревогу. Префикс один на файл, и шаблон разбора собран из
# него же: разойдись они, бот перестал бы узнавать собственные номера.
ORDER_PREFIX = "RP-"

# Номер заказа, который мы же и выдали: RP-клиент-модуль-месяцы-время.
ORDER_RE = re.compile(
    r"^" + re.escape(ORDER_PREFIX) + r"(\d+)-([a-z_]+)-(\d+)-(\d{12})$"
)


def is_ours(order_num: str) -> bool:
    """Наш ли это номер заказа хотя бы по началу.

    Проверка нарочно грубая: ею отличается чужая оплата (про неё молчим) от
    своей, но испорченной (про неё кричим). Разбирает номер до конца
    `read_order`, и это разные вопросы.
    """
    return (order_num or "").strip().startswith(ORDER_PREFIX)


def secret() -> str:
    """Секретный ключ платёжной страницы. Пустой значит «проверять нечем»."""
    return config.env(SECRET_VAR)


def _strings(value: Any) -> Any:
    """Все значения строками, структура как была.

    Так делает Prodamus на своей стороне перед подписью, и так же обязаны
    сделать мы: число 1 и строка «1» дают разный JSON, а значит разный хеш.
    """
    if isinstance(value, dict):
        return {str(key): _strings(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_strings(item) for item in value]
    return "" if value is None else str(value)


def signature(data: Mapping[str, Any], key: str) -> str:
    """Подпись набора полей по правилам Prodamus.

    Порядок шагов взят из их документации: значения в строки, ключи по
    алфавиту вместе с вложенными, JSON без пробелов, экранированный слеш,
    HMAC SHA-256. Слеш экранируется потому, что на той стороне PHP, а его
    json_encode пишет обратный слеш перед косой чертой по умолчанию; Python
    так не делает, и без этой строки подпись расходилась бы на каждом
    адресе внутри данных.
    """
    clean = {name: item for name, item in data.items() if name != "signature"}
    payload = json.dumps(_strings(clean), sort_keys=True, separators=(",", ":"))
    payload = payload.replace("/", "\\/")
    return hmac.new(key.encode(), payload.encode(), hashlib.sha256).hexdigest()


def verify(data: Mapping[str, Any], sign: str, key: str) -> bool:
    """Сошлась ли подпись. Пустая подпись или пустой ключ это «нет».

    Сравнение идёт compare_digest, а не обычным равенством: сравнение строк
    выходит из цикла на первом несовпавшем знаке, и по времени ответа
    подпись подбирается знак за знаком.
    """
    if not sign or not key:
        return False
    return hmac.compare_digest(signature(data, key).lower(), sign.strip().lower())


def php_dict(flat: Mapping[str, Any]) -> dict:
    """Плоские поля формы в такую же вложенность, какую видит PHP.

    Prodamus шлёт корзину полями вида products[0][name], а подписывает уже
    разобранную структуру. Значит и считать подпись надо по разобранной: по
    плоским полям хеш выйдет другой, и настоящее уведомление будет отвергнуто.
    """
    nested: dict = {}
    for raw, value in flat.items():
        name, bracket, tail = str(raw).partition("[")
        keys = [name]
        if bracket:
            keys += re.findall(r"\[([^\]]*)\]", bracket + tail)
        node = nested
        for key in keys[:-1]:
            step = node.get(key)
            if not isinstance(step, dict):
                step = {}
                node[key] = step
            node = step
        node[keys[-1]] = value
    return _listed(nested)


def _listed(value: Any) -> Any:
    """Словарь с ключами 0, 1, 2 это список: в PHP и в JSON это массив."""
    if not isinstance(value, dict):
        return value
    inner = {key: _listed(item) for key, item in value.items()}
    keys = list(inner)
    if keys and all(str(key).isdigit() for key in keys):
        order = sorted(keys, key=int)
        if [int(key) for key in order] == list(range(len(order))):
            return [inner[key] for key in order]
    return inner


@dataclass(frozen=True)
class Order:
    """Что зашито в наш номер заказа: кто, что и на сколько купил."""

    client_id: int
    module: str
    months: int


def read_order(order_num: str) -> Order | None:
    """Разбирает номер заказа. None означает «это не наш номер».

    Номер приходит снаружи, и верить ему на слово нельзя: модуль сверяется
    с конфигом, срок со списком периодов. Иначе подделанный номер открыл бы
    модуль, которого нет в тарифах, или срок, которого мы не продаём.
    """
    found = ORDER_RE.match((order_num or "").strip())
    if found is None:
        return None
    client_id, module, months = int(found[1]), found[2], int(found[3])
    if module not in config.modules() or months not in config.periods():
        return None
    return Order(client_id=client_id, module=module, months=months)


def _amount(raw: Any) -> Decimal | None:
    try:
        return Decimal(str(raw).replace(",", ".").strip())
    except (InvalidOperation, AttributeError, TypeError):
        return None


@dataclass(frozen=True)
class Result:
    """Чем кончилось уведомление: причина, заказ и суммы для разговора."""

    reason: str
    order_num: str = ""
    order: Order | None = None
    paid: Decimal | None = None
    due: Decimal | None = None
    until: datetime | None = None

    @property
    def granted(self) -> bool:
        """Открыт ли доступ прямо сейчас. Дубль это не выдача."""
        return self.reason == OK


def apply(
    data: Mapping[str, Any],
    sign: str,
    *,
    now: datetime | None = None,
    path: str | Path | None = None,
) -> Result:
    """Разбирает уведомление об оплате и открывает доступ, если всё сошлось.

    Дверь к доступу здесь та же, что у всех: grant_access с номером заказа в
    payment_ref. Prodamus повторяет уведомление, пока не получит 200, поэтому
    повтор обязан быть безопасным, и он безопасен: второй раз с тем же
    номером дверь узнаёт дубль и ничего не продлевает.

    Денег меньше цены - доступ не открывается. Сумму в ссылке клиент может
    поправить в адресной строке, и это то единственное место, где такая
    правка становится заметной.
    """
    key = secret()
    if not key:
        logger.warning("уведомление Prodamus пришло, а %s не задан", SECRET_VAR)
        return Result(reason=NO_SECRET, order_num=str(data.get("order_num") or ""))

    nested = php_dict(data)
    order_num = str(nested.get("order_num") or "")
    if not verify(nested, sign, key):
        audit.log(
            "payment.sign",
            None,
            f"Уведомление об оплате с неверной подписью, заказ {order_num or 'без номера'}.",
            level="warning",
            path=path,
        )
        return Result(reason=BAD_SIGN, order_num=order_num)

    status = str(nested.get("payment_status") or "")
    order = read_order(order_num)
    paid = _amount(nested.get("sum"))
    if status != PAID:
        return Result(reason=NOT_PAID, order_num=order_num, order=order, paid=paid)
    if order is None:
        if not is_ours(order_num):
            # Оплата соседнего бота с той же платёжной страницы. Это
            # обычное дело, а не беда: отвечаем «принято» и молчим, иначе
            # владелец получал бы сообщение на каждую чужую продажу.
            logger.info("уведомление не нашего бота, заказ %s", order_num or "пуст")
            return Result(reason=FOREIGN, order_num=order_num, paid=paid)
        audit.log(
            "payment.order",
            None,
            f"Оплата прошла, номер заказа {order_num} наш, а разобрать его не вышло.",
            level="error",
            path=path,
        )
        return Result(reason=BAD_ORDER, order_num=order_num, paid=paid)

    due = config.price_decimal(order.module, order.months)
    if paid is None or paid < due:
        audit.log(
            "payment.short",
            order.client_id,
            f"Оплата {paid} меньше цены {due}, заказ {order_num}, доступ не открыт.",
            level="error",
            path=path,
        )
        return Result(
            reason=SHORT_PAY, order_num=order_num, order=order, paid=paid, due=due
        )

    moment = now or datetime.now()
    granted = access.grant_access(
        order.client_id,
        order.module,
        months_to_days(order.months, moment.date()),
        order_num,
        method=CARD_METHOD,
        actor="prodamus",
        path=path,
    )
    return Result(
        reason=DUPLICATE if granted.duplicate else OK,
        order_num=order_num,
        order=order,
        paid=paid,
        due=due,
        until=granted.until,
    )
