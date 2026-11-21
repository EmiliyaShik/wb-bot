"""Ссылка на оплату картой: что в ней лежит и чего в ней быть не должно.

Сети тут нет и быть не может: модуль собирает строку адреса, а не ходит в
Prodamus. Проверяется то, из-за чего оплата ломается молча: сумма не та,
номер заказа гуляет, поле для чека пустое.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime
from urllib.parse import parse_qs, urlparse

import pytest

from core import access, config, db
from core.billing import payform

NOW = datetime(2026, 11, 10, 0, 30)
PAGE = "https://primer.payform.ru/"
BOT = "https://t.me/RentgenProdazh_bot"


@pytest.fixture
def paying(monkeypatch):
    monkeypatch.setenv(payform.URL_VAR, PAGE)
    monkeypatch.setenv(payform.BOT_VAR, BOT)


def params(url: str) -> dict[str, str]:
    return {key: value[0] for key, value in parse_qs(urlparse(url).query).items()}


def make(module="finance", months=3, title="Финансы", period="3 месяца"):
    return payform.payment(57, module, months, title=title, period=period, now=NOW)


def test_without_the_payment_page_there_is_no_link(monkeypatch):
    """Пустая переменная это «оплата не настроена», а не повод выдумать адрес."""
    monkeypatch.setenv(payform.URL_VAR, "")

    assert payform.ready() is False
    assert make() is None


def test_the_amount_is_the_one_from_the_price_list(paying):
    """Цена в ссылке та же, что в витрине: клиент сверяет её глазами."""
    pay = make()

    assert pay.amount == config.price_decimal("finance", 3)
    assert params(pay.url)["products[0][price]"] == "2673"


def test_the_order_number_says_who_bought_what(paying):
    pay = make()

    assert pay.order_id == "RP-57-finance-3-202611100030"
    assert params(pay.url)["order_id"] == pay.order_id


def test_two_presses_of_one_purchase_give_one_number(paying):
    """Нажал дважды - это одна покупка: номер заказа тот же, и доступ один."""
    assert make().order_id == make().order_id


def test_different_periods_are_different_purchases(paying):
    assert make(months=3).order_id != make(months=12).order_id


def test_the_client_number_travels_through_the_payment(paying):
    """По нему будет видно, чей доступ включать, когда выдача станет автоматической."""
    assert params(make().url)["_param_client"] == "57"


def test_the_telegram_id_does_not_travel_in_the_link(paying):
    """В адресе едет внутренний номер, а не личный идентификатор клиента."""
    assert "777" not in params(make().url)["_param_client"]


def test_the_receipt_field_is_never_empty(paying):
    """Без «доступов к материалам» Prodamus не пробьёт чек, и это нарушение 54-ФЗ."""
    got = params(make().url)

    assert got["paid_content"].strip()
    assert BOT in got["paid_content"]


def test_the_product_name_says_the_module_and_the_period(paying):
    """Это наименование увидит и клиент в чеке, и налоговая."""
    pay = make()

    assert "Финансы" in pay.product
    assert "3 месяца" in pay.product
    assert params(pay.url)["products[0][name]"] == pay.product


def test_the_service_is_a_service_and_the_quantity_is_one(paying):
    got = params(make().url)

    assert got["products[0][type]"] == "service"
    assert got["products[0][quantity]"] == "1"
    assert got["do"] == "pay"


def test_the_page_address_is_taken_as_it_is_given(paying, monkeypatch):
    """Адрес страницы пишет владелец, и со слешем, и без, и с вопросом."""
    for base in ("https://x.payform.ru", "https://x.payform.ru/", "https://x.payform.ru/?"):
        monkeypatch.setenv(payform.URL_VAR, base)
        url = make().url
        assert url.startswith("https://x.payform.ru")
        assert "?do=pay" in url
        assert "??" not in url


def test_a_demo_payment_is_not_offered_by_accident(paying):
    """Демо-режим это проверка владельца, а не состояние боевой ссылки."""
    assert "demo_mode" not in make().url


# --- уведомление об оплате ---------------------------------------------------

SECRET = "kluch-platyozhnoy-stranicy"
ORDER = "RP-{client}-finance-3-202611100030"


@pytest.fixture
def paid(monkeypatch):
    monkeypatch.setenv(payform.SECRET_VAR, SECRET)


@pytest.fixture
def buyer(db_path):
    return db.admin_repo(db_path).ensure_client(880001)


def notice(client_id: int, total: str = "2673.00", status: str = "success") -> dict:
    """Поля уведомления так, как их присылает Prodamus: плоско и строками."""
    return {
        "date": "2026-11-10T00:40:00+03:00",
        "order_id": "300155",
        "order_num": ORDER.format(client=client_id),
        "domain": "primer.payform.ru",
        "sum": total,
        "customer_phone": "+79990000000",
        "payment_type": "Пластиковая карта Visa, MasterCard, МИР",
        "attempt": "1",
        "products[0][name]": "Рентген Продаж, модуль Финансы, 3 месяца",
        "products[0][price]": total,
        "products[0][quantity]": "1",
        "products[0][sum]": total,
        "payment_status": status,
        "payment_status_description": "Успешная оплата",
    }


def signed(fields: dict) -> str:
    return payform.signature(payform.php_dict(fields), SECRET)


def test_the_basket_is_unpacked_the_way_php_sees_it():
    """Подпись считается по разобранной корзине, а не по плоским полям."""
    got = payform.php_dict(
        {"a": "1", "products[0][name]": "Товар", "products[1][name]": "Второй"}
    )

    assert got["a"] == "1"
    assert got["products"] == [{"name": "Товар"}, {"name": "Второй"}]


def test_the_signature_notices_a_changed_amount():
    """Ради этого всё и делается: сумму в уведомлении подправить нельзя."""
    fields = payform.php_dict(notice(1))
    sign = payform.signature(fields, SECRET)
    fields["sum"] = "1.00"

    assert payform.verify(fields, sign, SECRET) is False


def test_a_foreign_key_does_not_open_the_door():
    fields = payform.php_dict(notice(1))

    assert payform.verify(fields, payform.signature(fields, "chuzhoy"), SECRET) is False


def test_an_empty_signature_is_not_a_signature():
    fields = payform.php_dict(notice(1))

    assert payform.verify(fields, "", SECRET) is False
    assert payform.verify(fields, payform.signature(fields, SECRET), "") is False


def test_the_order_number_is_read_back_the_way_it_was_written(paying):
    """Номер из ссылки и номер из уведомления это один и тот же номер."""
    made = make().order_id
    back = payform.read_order(made)

    assert back == payform.Order(client_id=57, module="finance", months=3)


def test_a_module_or_a_period_we_do_not_sell_is_not_our_order():
    assert payform.read_order("RP-1-chuzhoy-3-202611100030") is None
    assert payform.read_order("RP-1-finance-7-202611100030") is None
    assert payform.read_order("order-1") is None
    assert payform.read_order("") is None


def test_payment_opens_the_module(paid, db_path, buyer):
    fields = notice(buyer)

    got = payform.apply(fields, signed(fields), path=db_path)

    assert got.reason == payform.OK
    assert got.granted is True
    assert access.access_of(buyer, "finance", path=db_path).works is True


def test_a_repeated_notification_does_not_extend_anything(paid, db_path, buyer):
    """Prodamus повторяет уведомление, пока не получит 200. Это безопасно."""
    fields = notice(buyer)
    sign = signed(fields)
    first = payform.apply(fields, sign, path=db_path)
    second = payform.apply(fields, sign, path=db_path)

    assert second.reason == payform.DUPLICATE
    assert second.granted is False
    assert access.access_of(buyer, "finance", path=db_path).until == first.until


def test_a_wrong_signature_opens_nothing(paid, db_path, buyer):
    fields = notice(buyer)

    got = payform.apply(fields, "0" * 64, path=db_path)

    assert got.reason == payform.BAD_SIGN
    assert access.access_of(buyer, "finance", path=db_path).works is False


def test_without_the_secret_key_nothing_is_opened(db_path, buyer, monkeypatch):
    """Проверять нечем - значит не открываем. Молча доверять нельзя."""
    monkeypatch.setenv(payform.SECRET_VAR, "")
    fields = notice(buyer)

    got = payform.apply(fields, "whatever", path=db_path)

    assert got.reason == payform.NO_SECRET
    assert access.access_of(buyer, "finance", path=db_path).works is False


def test_a_cancelled_order_opens_nothing(paid, db_path, buyer):
    fields = notice(buyer, status="order_canceled")

    got = payform.apply(fields, signed(fields), path=db_path)

    assert got.reason == payform.NOT_PAID
    assert access.access_of(buyer, "finance", path=db_path).works is False


def test_an_order_number_that_is_not_ours_opens_nothing(paid, db_path, buyer):
    """Чужой номер доступа не открывает. Тревогой это не считается: такой
    номер приходит от соседнего бота с той же платёжной страницы."""
    fields = notice(buyer)
    fields["order_num"] = "chuzhoy-zakaz-1"

    got = payform.apply(fields, signed(fields), path=db_path)

    assert got.reason == payform.FOREIGN
    assert access.access_of(buyer, "finance", path=db_path).works is False


def test_paying_less_than_the_price_opens_nothing(paid, db_path, buyer):
    """Сумму в ссылке клиент может поправить в адресной строке. Здесь это видно."""
    fields = notice(buyer, total="1.00")

    got = payform.apply(fields, signed(fields), path=db_path)

    assert got.reason == payform.SHORT_PAY
    assert got.due == config.price_decimal("finance", 3)
    assert access.access_of(buyer, "finance", path=db_path).works is False


def test_paying_more_than_the_price_is_fine(paid, db_path, buyer):
    fields = notice(buyer, total="3000.00")

    assert payform.apply(fields, signed(fields), path=db_path).reason == payform.OK


# --- одна платёжная страница на несколько ботов -------------------------------


def test_a_foreign_bot_order_is_recognised_as_foreign():
    """Платёжная страница бывает одна на несколько ботов."""
    assert payform.is_ours("RP-1-finance-3-202611100030") is True
    assert payform.is_ours("MARY-1-tarif-3-202611100030") is False
    assert payform.is_ours("") is False


def test_a_broken_order_of_ours_is_still_ours():
    """Начало наше, а разобрать не вышло: это беда, а не чужая продажа."""
    assert payform.is_ours("RP-sloman") is True
    assert payform.read_order("RP-sloman") is None


def test_a_foreign_payment_opens_nothing_and_is_not_an_alarm(paid, db_path, buyer):
    fields = notice(buyer)
    fields["order_num"] = "MARY-7-tarif-1-202611100030"

    got = payform.apply(fields, signed(fields), path=db_path)

    assert got.reason == payform.FOREIGN
    assert got.granted is False
    assert access.access_of(buyer, "finance", path=db_path).works is False


def test_our_own_broken_order_is_still_an_alarm(paid, db_path, buyer):
    """Деньги пришли, номер наш, а кому включать, непонятно. Это надо знать."""
    fields = notice(buyer)
    fields["order_num"] = "RP-sloman"

    got = payform.apply(fields, signed(fields), path=db_path)

    assert got.reason == payform.BAD_ORDER


# --- как именно собирается строка под подпись ---------------------------------


def test_cyrillic_is_not_escaped_before_signing():
    """Поймано живым тестовым запросом Prodamus, вернувшимся с «bad sign».

    Python по умолчанию превращает русские буквы в \\uXXXX, PHP на той
    стороне пишет их как есть. В уведомлении кириллица почти всегда: способ
    оплаты словами, название товара, примечание. Поэтому строка под подпись
    закреплена здесь целиком, а не проверяется «сама с собой».
    """
    payload = '{"a":"тест"}'
    want = hmac.new(b"kluch", payload.encode("utf-8"), hashlib.sha256).hexdigest()

    assert payform.signature({"a": "тест"}, "kluch") == want


def test_the_slash_is_escaped_the_way_php_writes_it():
    """json_encode в PHP пишет \\/ вместо /, и в адресах это видно всегда."""
    payload = '{"u":"https:\\/\\/t.me\\/bot"}'
    want = hmac.new(b"kluch", payload.encode("utf-8"), hashlib.sha256).hexdigest()

    assert payform.signature({"u": "https://t.me/bot"}, "kluch") == want


def test_keys_are_sorted_and_there_are_no_spaces():
    payload = '{"a":"1","b":"2"}'
    want = hmac.new(b"kluch", payload.encode("utf-8"), hashlib.sha256).hexdigest()

    assert payform.signature({"b": 2, "a": 1}, "kluch") == want


def test_the_basket_is_signed_as_a_nested_array():
    """Корзина подписывается разобранной: PHP видит массив, а не плоские поля."""
    payload = '{"products":[{"name":"Товар"}]}'
    want = hmac.new(b"kluch", payload.encode("utf-8"), hashlib.sha256).hexdigest()

    assert payform.signature(payform.php_dict({"products[0][name]": "Товар"}), "kluch") == want


def test_a_boolean_is_written_the_php_way():
    """PHP пишет истину единицей, Python словом True."""
    payload = '{"no":"0","yes":"1"}'
    want = hmac.new(b"kluch", payload.encode("utf-8"), hashlib.sha256).hexdigest()

    assert payform.signature({"yes": True, "no": False}, "kluch") == want


# --- тестовая оплата ----------------------------------------------------------


def test_the_test_link_says_demo_and_carries_its_own_order_number(paying):
    """Демо-режим включается в ссылке, а не настройкой страницы.

    Настройка выключила бы приём денег у всех сразу, включая настоящих
    покупателей. Поле в адресе действует только на эту одну оплату.
    """
    pay = payform.payment(
        0, "funnel", 1, title="Воронка", period="1 месяц", now=NOW, test=True
    )

    assert params(pay.url)["demo_mode"] == "1"
    assert pay.order_id.startswith(payform.TEST_PREFIX)
    assert payform.is_test(pay.order_id) is True


def test_a_usual_link_stays_without_demo(paying):
    """Признак демо-режима не должен протечь в обычную покупку."""
    assert "demo_mode" not in params(make().url)
    assert payform.is_test(make().order_id) is False


def test_a_test_order_is_ours_for_the_neighbours(paying):
    """Начало номера общее нарочно: соседний бот обязан узнать чужое и промолчать."""
    pay = payform.payment(
        0, "funnel", 1, title="Воронка", period="1 месяц", now=NOW, test=True
    )

    assert payform.is_ours(pay.order_id) is True


def test_a_test_order_cannot_be_read_as_a_purchase():
    """Доступ по тестовому номеру не открывается даже подделкой.

    Шаблон разбора ждёт после начала номера цифры клиента, и слово «test»
    ему не подходит никогда. Значит ни клиента, ни модуля, ни срока из
    такого номера не достать, сколько их туда ни впиши.
    """
    assert payform.read_order(f"{payform.TEST_PREFIX}57-finance-3-202611100030") is None
