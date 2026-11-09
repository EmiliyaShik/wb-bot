"""Ссылка на оплату картой: что в ней лежит и чего в ней быть не должно.

Сети тут нет и быть не может: модуль собирает строку адреса, а не ходит в
Prodamus. Проверяется то, из-за чего оплата ломается молча: сумма не та,
номер заказа гуляет, поле для чека пустое.
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import parse_qs, urlparse

import pytest

from core import config
from core.billing import payform

NOW = datetime(2026, 11, 10, 0, 30)
PAGE = "https://tvoibot.payform.ru/"
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
