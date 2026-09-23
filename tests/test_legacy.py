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


# --- безопасность: чужой HTML с витрины WB ---

PHISHING = '<a href="https://phishing.example/">Открыть на Wildberries</a>'


def _card(**fields):
    base = dict(
        article=179323396,
        name="Кружка",
        brand="",
        price=100.0,
        old_price=None,
        rating=None,
        feedbacks=0,
        supplier="",
    )
    base.update(fields)
    return format_product(card.Product(**base))


def test_seller_cannot_smuggle_a_link_into_our_message():
    text = _card(name=f"Кружка {PHISHING}", description=f"Купите тут {PHISHING}")
    assert "<a href=\"https://phishing.example/\">" not in text
    assert "&lt;a href=&quot;https://phishing.example/&quot;&gt;" in text
    # наша собственная ссылка на витрину остаётся живой разметкой
    assert '<a href="https://www.wildberries.ru/catalog/179323396/detail.aspx">' in text


def test_single_angle_bracket_does_not_break_the_message():
    text = _card(name="Кружка < 300 мл", supplier="ИП <Иванов>")
    assert "Кружка &lt; 300 мл" in text
    assert "ИП &lt;Иванов&gt;" in text
    # разметка бота осталась парной
    assert text.count("<b>") == text.count("</b>")


def test_every_field_from_wb_is_escaped():
    text = _card(
        name="<i>имя</i>",
        brand="<b>бренд</b>",
        supplier="<u>продавец</u>",
        description="<s>описание</s>",
        characteristics=[("<код>", "<значение>")],
        reviews=[card.Review(text="<script>alert(1)</script>", rating=5, date=None)],
    )
    for raw in ("<i>", "<b>бренд", "<u>", "<s>", "<код>", "<script>"):
        assert raw not in text, f"в сообщение просочилось {raw}"
    assert "&lt;script&gt;" in text
