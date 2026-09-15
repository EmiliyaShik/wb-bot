"""Старая функция бота: артикул или ссылка возвращают карточку товара."""

from legacy import card
from legacy.handlers import format_product, parse_article


def test_parse_article_from_number():
    assert parse_article("179323396") == 179323396


def test_parse_article_from_link():
    link = "https://www.wildberries.ru/catalog/179323396/detail.aspx"
    assert parse_article(link) == 179323396


def test_parse_article_rejects_junk():
    assert parse_article("привет") is None
    assert parse_article("123") is None
    assert parse_article("") is None


def test_format_product_keeps_old_card():
    product = card.Product(
        article=179323396,
        name="Кружка",
        brand="Бренд",
        price=1234.0,
        old_price=2000.0,
        rating=4.5,
        feedbacks=17,
        supplier="Продавец",
    )
    text = format_product(product)
    assert "<b>Кружка</b>" in text
    assert "1 234 ₽" in text
    assert "2 000 ₽" in text
    assert "💬 Отзывов: 17" in text
    assert "<code>179323396</code>" in text
    assert "https://www.wildberries.ru/catalog/179323396/detail.aspx" in text


def test_card_module_keeps_public_names():
    for name in ("fetch_product", "Product", "WBApiError", "WBBlockedError", "ProductNotFoundError"):
        assert hasattr(card, name), f"в legacy.card пропало имя {name}"
