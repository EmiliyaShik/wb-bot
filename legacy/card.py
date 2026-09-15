"""Клиент для получения данных карточки товара с Wildberries.

Источники данных:
  * card.wb.ru/cards/v4/detail - базовая карточка (название, цена, рейтинг, root);
  * feedbacks{1,2}.wb.ru/feedbacks/v2/{root} - отзывы (ответ приходит в gzip);
  * basket-XX.wbbasket.ru/.../card.json - описание и характеристики товара.
"""

import asyncio
import gzip
import json
import os
from dataclasses import dataclass, field

import httpx

# Прокси только для запросов к Wildberries (напр. чтобы обойти гео-блок).
# Управляется двумя переменными окружения:
#   WB_PROXY_ENABLED - включает прокси (1/true/yes/on; по умолчанию выключен);
#   WB_PROXY         - адрес, напр. http://user:pass@host:port или socks5://host:port.
# Пока WB_PROXY_ENABLED выключен, запросы идут напрямую (см. trust_env ниже).
# Конфиг читается лениво (в момент запроса), чтобы не зависеть от порядка
# импорта и вызова load_dotenv().


def _env_flag(name: str, default: bool = False) -> bool:
    """Читает булеву переменную окружения (1/true/yes/on → True)."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _proxy_config() -> tuple[str | None, bool]:
    """Возвращает (proxy, trust_env) для httpx-клиента исходя из окружения.

    Когда прокси выключен, trust_env=False - иначе httpx подхватил бы системные
    HTTP_PROXY/HTTPS_PROXY, и запросы шли бы не «напрямую», как задумано.
    """
    if _env_flag("WB_PROXY_ENABLED"):
        return (os.getenv("WB_PROXY") or None), True
    return None, False

# Актуальный публичный эндпоинт карточки товара (v1/v2 больше не работают).
# dest=-1257786 - регион Москвы (нужен, чтобы вернулась цена).
# Пробуем несколько зеркал: если одно недоступно, идём к следующему.
CARD_API_HOSTS = [
    "https://card.wb.ru/cards/v4/detail",
    "https://u-card.wb.ru/cards/v4/detail",
]

DEFAULT_PARAMS = {
    "appType": "1",
    "curr": "rub",
    "dest": "-1257786",
    "spp": "30",
}

# Зеркала сервиса отзывов. root может «жить» на любом из них - пробуем по очереди.
FEEDBACKS_HOSTS = [
    "https://feedbacks1.wb.ru",
    "https://feedbacks2.wb.ru",
]

# Сколько последних отзывов подтягивать для показа.
MAX_REVIEWS = 3

# Диапазоны vol -> номер basket-хоста (по возрастанию верхней границы диапазона).
# Таблица со временем «дрейфует», поэтому это лишь подсказка для первого запроса -
# если она не сработает, мы всё равно переберём остальные корзины.
BASKET_VOL_RANGES = [
    (143, 1), (287, 2), (431, 3), (719, 4), (1007, 5), (1061, 6),
    (1115, 7), (1169, 8), (1313, 9), (1601, 10), (1655, 11), (1919, 12),
    (2045, 13), (2189, 14), (2405, 15), (2621, 16), (2837, 17), (3053, 18),
    (3269, 19), (3485, 20), (3701, 21), (3917, 22),
]
BASKET_MAX_HOST = 40  # верхняя граница перебора корзин

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

BASE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}


class ProductNotFoundError(Exception):
    """Товар с указанным артикулом не найден."""


class WBApiError(Exception):
    """Ошибка при обращении к API Wildberries."""


class WBBlockedError(WBApiError):
    """Wildberries заблокировал запрос (403) - обычно из-за не-российского IP."""


@dataclass
class Review:
    """Один отзыв о товаре."""

    text: str
    rating: int | None   # оценка автора отзыва, 1-5
    date: str | None     # дата создания (как пришла от API)


@dataclass
class Product:
    """Данные карточки товара."""

    article: int
    name: str
    brand: str
    price: float | None          # актуальная цена (со скидкой), руб.
    old_price: float | None      # цена без скидки, руб.
    rating: float | None         # рейтинг товара
    feedbacks: int               # количество отзывов
    supplier: str | None         # продавец
    root: int | None = None      # id карточки (imtId) - ключ для отзывов и CDN
    description: str | None = None                 # описание с CDN
    characteristics: list[tuple[str, str]] = field(default_factory=list)  # (название, значение)
    reviews: list[Review] = field(default_factory=list)                   # последние отзывы

    @property
    def url(self) -> str:
        return f"https://www.wildberries.ru/catalog/{self.article}/detail.aspx"


def _kopecks_to_rub(value: int | None) -> float | None:
    """Цены в API приходят в копейках - переводим в рубли."""
    if value is None:
        return None
    return round(value / 100, 2)


def _extract_products(payload: dict) -> list[dict]:
    """Достаёт список товаров из ответа v4.

    В v4 products лежит на верхнем уровне, но на всякий случай поддерживаем
    и старую обёртку data.products.
    """
    products = payload.get("products")
    if isinstance(products, list):
        return products
    return (payload.get("data") or {}).get("products") or []


def _extract_price(product: dict) -> tuple[float | None, float | None]:
    """Возвращает (актуальная_цена, старая_цена) из карточки."""
    # В v4 цена лежит в product["sizes"][i]["price"].
    for size in product.get("sizes", []):
        price = size.get("price")
        if price:
            actual = _kopecks_to_rub(price.get("product") or price.get("total"))
            old = _kopecks_to_rub(price.get("basic"))
            return actual, old
    # Запасной вариант - старый формат с salePriceU/priceU.
    actual = _kopecks_to_rub(product.get("salePriceU") or product.get("priceU"))
    old = _kopecks_to_rub(product.get("priceU"))
    return actual, old


async def _request_card(client: httpx.AsyncClient, article: int) -> dict:
    """Обходит зеркала card.wb.ru и возвращает первый удачный JSON-ответ."""
    params = {**DEFAULT_PARAMS, "nm": str(article)}

    last_error: Exception | None = None
    blocked = False

    for url in CARD_API_HOSTS:
        try:
            response = await client.get(url, params=params, headers=BASE_HEADERS)
            if response.status_code == 403:
                blocked = True
                continue
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            last_error = exc
        except ValueError as exc:  # некорректный JSON
            last_error = exc

    if blocked:
        raise WBBlockedError(
            "Wildberries заблокировал запрос (403). Обычно так бывает при "
            "обращении с не-российского IP - запустите бот с сервера в РФ/СНГ "
            "или включите прокси (WB_PROXY_ENABLED)."
        )
    raise WBApiError(f"Не удалось получить данные с Wildberries: {last_error}")


def _loads_maybe_gzip(content: bytes) -> dict:
    """Парсит JSON, при необходимости распаковывая gzip вручную.

    httpx сам разжимает ответ, если сервер прислал Content-Encoding: gzip,
    но feedbacks нередко отдаёт gzip без этого заголовка - тогда content
    остаётся сжатым, и обычный .json() падает.
    """
    try:
        return json.loads(content)
    except (ValueError, UnicodeDecodeError):
        return json.loads(gzip.decompress(content))


async def _fetch_feedbacks(
    client: httpx.AsyncClient, root: int
) -> tuple[float | None, int | None, list[Review]]:
    """Забирает отзывы по root. Возвращает (рейтинг, кол-во_отзывов, отзывы).

    Best-effort: при любой ошибке возвращает пустой результат.
    """
    for host in FEEDBACKS_HOSTS:
        try:
            response = await client.get(
                f"{host}/feedbacks/v2/{root}", headers=BASE_HEADERS
            )
            if response.status_code != 200 or not response.content:
                continue
            data = _loads_maybe_gzip(response.content)
        except (httpx.HTTPError, ValueError, OSError):
            continue

        feedbacks = data.get("feedbacks")
        count = data.get("feedbackCount")
        # root, которого нет на этом зеркале, отдаёт пустой набор - идём дальше.
        if not feedbacks and not count:
            continue

        valuation = data.get("valuation")
        rating = float(valuation) if valuation else None

        reviews: list[Review] = []
        for fb in (feedbacks or [])[:MAX_REVIEWS]:
            text = (fb.get("text") or "").strip()
            if not text:
                continue
            reviews.append(
                Review(
                    text=text,
                    rating=fb.get("productValuation"),
                    date=fb.get("createdDate"),
                )
            )
        return rating, count, reviews

    return None, None, []


def _basket_host_candidates(vol: int) -> list[int]:
    """Порядок перебора basket-хостов: сначала подсказка по таблице, затем остальные."""
    guess = BASKET_MAX_HOST
    for max_vol, host in BASKET_VOL_RANGES:
        if vol <= max_vol:
            guess = host
            break
    ordered = [guess] + [n for n in range(1, BASKET_MAX_HOST + 1) if n != guess]
    return ordered


def _parse_characteristics(card: dict) -> list[tuple[str, str]]:
    """Собирает характеристики из card.json (options / grouped_options)."""
    result: list[tuple[str, str]] = []

    for opt in card.get("options") or []:
        name = opt.get("name")
        value = opt.get("value")
        if name and value:
            result.append((str(name), str(value)))

    for group in card.get("grouped_options") or []:
        for opt in group.get("options") or []:
            name = opt.get("name")
            value = opt.get("value")
            if name and value:
                result.append((str(name), str(value)))

    return result


async def _fetch_card_details(
    client: httpx.AsyncClient, nm_id: int
) -> tuple[str | None, list[tuple[str, str]]]:
    """Забирает описание и характеристики с CDN basket-XX. Best-effort.

    Путь на CDN строится по nmId (артикулу), а не по root: описание и
    характеристики лежат именно под nmId. root тут не подходит - по нему
    подтягивается карточка чужого товара.
    """
    vol = nm_id // 100000
    part = nm_id // 1000

    for host_num in _basket_host_candidates(vol):
        url = (
            f"https://basket-{host_num:02d}.wbbasket.ru"
            f"/vol{vol}/part{part}/{nm_id}/info/ru/card.json"
        )
        try:
            response = await client.get(url, headers=BASE_HEADERS)
            if response.status_code != 200:
                continue
            card = response.json()
        except (httpx.HTTPError, ValueError):
            continue

        description = (card.get("description") or "").strip() or None
        characteristics = _parse_characteristics(card)
        return description, characteristics

    return None, []


async def fetch_product(article: int) -> Product:
    """Запрашивает карточку товара по артикулу из всех трёх источников."""
    proxy, trust_env = _proxy_config()
    async with httpx.AsyncClient(
        timeout=15, proxy=proxy, trust_env=trust_env
    ) as client:
        payload = await _request_card(client, article)

        products = _extract_products(payload)
        if not products:
            raise ProductNotFoundError(f"Товар с артикулом {article} не найден")

        item = products[0]
        actual_price, old_price = _extract_price(item)
        root = item.get("root")

        product = Product(
            article=article,
            name=item.get("name") or "Без названия",
            brand=item.get("brand") or "",
            price=actual_price,
            old_price=old_price if old_price != actual_price else None,
            rating=item.get("reviewRating") or item.get("rating"),
            feedbacks=item.get("feedbacks") or 0,
            supplier=item.get("supplier"),
            root=root,
        )

        if not root:
            return product

        # Отзывы и карточку с CDN тянем параллельно - оба источника опциональны.
        (fb_rating, fb_count, reviews), (description, characteristics) = (
            await asyncio.gather(
                _fetch_feedbacks(client, root),
                _fetch_card_details(client, article),
            )
        )

    if fb_rating is not None:
        product.rating = fb_rating
    if fb_count:
        product.feedbacks = fb_count
    product.reviews = reviews
    product.description = description
    product.characteristics = characteristics

    return product
