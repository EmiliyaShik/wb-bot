"""Старая функция бота: артикул или ссылка возвращают карточку товара."""

import pytest

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


# --- витрина заблокирована, карточка приходит с хранилища WB ---


def test_blocked_showcase_says_only_the_price_is_missing():
    """«Нет данных» прочиталось бы как «товара нет»: это разные вещи."""
    text = _card(price=None, showcase_blocked=True)

    assert "Цена: сейчас недоступна" in text
    assert "нет в наличии" not in text


def test_without_the_showcase_an_empty_price_still_means_out_of_stock():
    assert "нет в наличии" in _card(price=None)


@pytest.mark.asyncio
async def test_a_blocked_showcase_falls_back_to_the_storage(monkeypatch):
    """403 от витрины это не отказ клиенту: карточка целиком лежит на CDN.

    Витрина отвечает 403 на запрос с не-российского адреса, а хранилище
    отдаёт ту же карточку кому угодно. Цены там нет, и только она теряется.
    """
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host.endswith("wb.ru") and "cards" in request.url.path:
            return httpx.Response(403)
        if host.startswith("basket-") and request.url.path.endswith("card.json"):
            return httpx.Response(
                200,
                json={
                    "imt_id": 777,
                    "imt_name": "Расчёска",
                    "subj_name": "Расчёски",
                    "vendor_code": "Арт.11",
                    "selling": {"brand_name": "Favorite Pets!"},
                    "description": "Чешет",
                    "options": [{"name": "Цвет", "value": "синий"}],
                },
            )
        if "feedbacks" in request.url.path:
            return httpx.Response(200, json={"valuation": "4.8", "feedbackCount": 531})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs.pop("proxy", None)
        kwargs.pop("trust_env", None)
        return real_client(*args, transport=transport, **kwargs)

    monkeypatch.setattr(card.httpx, "AsyncClient", fake_client)

    product = await card.fetch_product(213817876)

    assert product.showcase_blocked is True
    assert product.name == "Расчёска"
    assert product.brand == "Favorite Pets!"
    assert product.price is None
    assert product.rating == 4.8 and product.feedbacks == 531
    assert product.characteristics == [("Цвет", "синий")]


@pytest.mark.asyncio
async def test_no_card_anywhere_is_still_an_honest_refusal(monkeypatch):
    """Ни витрины, ни хранилища: тогда отказ, но без слова «403» клиенту."""
    import httpx

    transport = httpx.MockTransport(lambda request: httpx.Response(403))
    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs.pop("proxy", None)
        kwargs.pop("trust_env", None)
        return real_client(*args, transport=transport, **kwargs)

    monkeypatch.setattr(card.httpx, "AsyncClient", fake_client)

    with pytest.raises(card.WBBlockedError) as error:
        await card.fetch_product(213817876)

    # В тексте для журнала причина названа, а имени переменной окружения нет.
    assert "403" in str(error.value)
    assert "WB_PROXY" not in str(error.value)
