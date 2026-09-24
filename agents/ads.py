"""Агент 5. Реклама: расход, заказы, цена заказа и ДРР по кампаниям и дням.

Источники три, и только первые два требуют похода в Wildberries.

1. **Статистика кампаний.** `GET /adv/v3/fullstats`, категория токена
   «Продвижение». Разрез по кампаниям, дням, площадкам и артикулам готовый,
   складывать его не надо. Ограничения WB (50 кампаний и 31 день за запрос,
   3 запроса в минуту) держит `core.wbapi`, агент о них не думает.
2. **Названия кампаний и фактически списанные суммы.** В статистике названия
   нет вовсе, там один `advertId`: имя приходит из информации о кампаниях.
   Фактически списанное это отдельный метод, история затрат.
3. **Выручка товаров.** Её уже собрал агент 1 в `fin_rows`. В WB за ней
   отсюда не ходят.

**Две цифры ДРР, и это главное решение модуля.**

Первая, «ДРР рекламы», считается внутри одного источника: расход `sum` и
выручка от рекламы `sum_price` лежат в одной и той же строке ответа
Wildberries. Вторая, «ДРР кабинета», сшивает два источника: расход отсюда,
а всю выручку товара из финансового отчёта. Поэтому вторая в текстах
названа нашим расчётом, а не полем Wildberries: показать число, собранное
из другого поля, и не сказать об этом, значит соврать уверенным голосом.

Ценность в расхождении этих двух цифр, и оно названо прямым текстом, а не
оставлено селлеру складывать самому. Вторая цифра всегда не больше первой:
выручка товара не меньше той её части, которую Wildberries приписал рекламе.
Значит, чем ближе цифры друг к другу, тем больше товар живёт на рекламе, а
чем дальше они разошлись, тем лучше товар продаётся сам. Тот же приём уже
работает в финансах: там проценты считаются двумя способами намеренно, и
разница между ними и есть признак утечки.

Копить расход у себя приходится не из-за глубины истории, а из-за статусов:
Wildberries отдаёт статистику только по кампаниям в статусах 7, 9 и 11.
Удалённая или отменённая кампания уносит свою историю с собой, и выгрузить
её задним числом уже нельзя. Поэтому сбор идёт каждый день, у всех
подключённых кабинетов, независимо от подписки, ровно как у воронки.

Деньги в базе целые копейки, в расчётах `Decimal`. `float` в денежном пути
не используется нигде, даже промежуточно.

Текстов бота здесь нет. Отчёт это данные (`AdsReport`), а во что их
превратить, знает `bot/handlers/ads.py`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from agents import finance
from core import clients, config, db, queue, scheduler, wbapi

logger = logging.getLogger(__name__)

__all__ = [
    "MODULE",
    "TASK_KIND",
    "COLLECT_ALL",
    "COLLECT_ONE",
    "CLEANUP",
    "PERIODS",
    "TOP_SIZE",
    "TOLERANCE",
    "OK",
    "NO_CATEGORY",
    "UNAVAILABLE",
    "NOT_COLLECTED",
    "CAMPAIGNS_SHEET",
    "DAYS_SHEET",
    "ARTICLES_SHEET",
    "METHOD_SHEET",
    "Campaign",
    "Day",
    "ArticleAds",
    "AdsReport",
    "Collected",
    "drr",
    "cpo",
    "target_drr",
    "set_target_drr",
    "default_target_drr",
    "collect_days",
    "history_days",
    "cleanup",
    "campaign_days",
    "campaign_articles",
    "upd_rows",
    "collect",
    "build",
    "excel_bytes",
    "file_name",
    "request_report",
    "report_task",
    "set_sender",
    "register_jobs",
    "METHODOLOGY",
]

MODULE = "ads"

# Виды задач. Первая это просьба клиента: собрать период и сразу отдать.
# Вторая это работа расписания, она ставит третью на каждый подключённый
# кабинет: недоступность WB у одного клиента не отменяет сбор у остальных,
# а повтор достаётся ровно тому, кто упал.
TASK_KIND = "ads_report"
COLLECT_ALL = "ads_collect"
COLLECT_ONE = "ads_collect_client"
# Третья работа расписания: убрать суточную рекламу глубже срока хранения.
# Данные копятся у всех подключённых кабинетов каждый день и независимо от
# подписки, а удалять их до сих пор было некому.
CLEANUP = "ads_cleanup"

# Периоды те же, что у соседних отчётов: селлер не должен запоминать, что в
# рекламе кнопки другие.
PERIODS = finance.PERIODS

# Сколько кампаний называем в сообщении. Остальные в файле.
TOP_SIZE = 5

# Расхождение статистического и фактически списанного расхода, начиная с
# которого о нём говорят вслух. Рубль, как и в сверке финансового отчёта:
# копеечная разница это округление, а не потерянные деньги.
TOLERANCE = Decimal("1")

# Сколько суток забираем за один заход, если в config.toml про это молчат.
COLLECT_DAYS_DEFAULT = 7

# Сколько суток храним собранную рекламу, если в config.toml про это молчат.
HISTORY_DAYS_DEFAULT = 400

# Целевой ДРР по умолчанию, если в config.toml его нет.
TARGET_DRR_DEFAULT = Decimal("15")

# Почему рекламы в отчёте может не быть. Это ключи, а не тексты: словами про
# них говорит `bot/handlers/ads.py`.
OK = "ok"
NO_CATEGORY = "no_category"
UNAVAILABLE = "unavailable"
NOT_COLLECTED = "not_collected"

# Ключ в настройках клиента, где лежит его целевой ДРР.
SETTINGS_KEY = "ads"

ZERO = Decimal("0")
CENT = Decimal("0.01")

# Статусы кампаний Wildberries, одинаковые во всех методах.
STATUS_TITLES: dict[int, str] = {
    -1: "удалена",
    4: "готова к запуску",
    7: "завершена",
    8: "отменена",
    9: "активна",
    11: "на паузе",
}


# --- чистые расчёты ----------------------------------------------------------


def _money(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value or 0))


def _int(value: Any) -> int:
    try:
        return int(Decimal(str(value or 0)))
    except Exception:  # noqa: BLE001 - чужое поле не должно ронять сбор
        return 0


def _kop(value: Any) -> int:
    try:
        return db.to_kop(Decimal(str(value or 0)))
    except Exception:  # noqa: BLE001 - чужое поле не должно ронять сбор
        return 0


def drr(spend: Any, revenue: Any) -> Decimal | None:
    """Доля расходов на рекламу: расход / выручка * 100.

    Без выручки доли не существует, и это не ноль и не бесконечность. Ноль
    прочитался бы как «реклама бесплатна», а нулевая выручка от рекламы при
    ненулевом расходе это обычное дело: заказов по рекламе не было, а деньги
    ушли. Об этом надо сказать словами, а не числом.
    """
    base = _money(revenue)
    if base <= ZERO:
        return None
    return (_money(spend) * 100 / base).quantize(CENT, rounding=ROUND_HALF_UP)


def cpo(spend: Any, orders: Any) -> Decimal | None:
    """Цена заказа: расход / заказы. Без заказов цены заказа не существует."""
    count = _int(orders)
    if count <= 0:
        return None
    return (_money(spend) / Decimal(count)).quantize(CENT, rounding=ROUND_HALF_UP)


# --- настройки клиента -------------------------------------------------------


def default_target_drr() -> Decimal:
    """Целевой ДРР по умолчанию. Число живёт в `config.toml`, секция `[ads]`.

    В коде его нет намеренно: это настройка владельца, а не факт про
    Wildberries. Личная цель клиента лежит в его настройках и перебивает эту.
    """
    try:
        section = config.settings().get("ads") or {}
        value = _money(section.get("target_drr", TARGET_DRR_DEFAULT))
    except (KeyError, TypeError, ValueError, OSError):
        return TARGET_DRR_DEFAULT
    return value if value > ZERO else TARGET_DRR_DEFAULT


def _share(name: str, default: int) -> Decimal:
    """Граница вывода про две цифры ДРР, в процентах. Тоже из конфига."""
    try:
        section = config.settings().get("ads") or {}
        return _money(section.get(name, default))
    except (KeyError, TypeError, ValueError, OSError):
        return Decimal(default)


def ad_driven_share() -> Decimal:
    return _share("ad_driven_share", 70)


def self_selling_share() -> Decimal:
    return _share("self_selling_share", 30)


def collect_days() -> int:
    """Окно суточного сбора рекламы в днях. Число из `config.toml`."""
    try:
        section = config.settings().get("ads") or {}
        return max(1, int(section.get("collect_days", COLLECT_DAYS_DEFAULT)))
    except (KeyError, TypeError, ValueError, OSError):
        return COLLECT_DAYS_DEFAULT


def history_days() -> int:
    """Срок хранения суточной рекламы в днях. Число из `config.toml`.

    Секция `[storage]`, а не `[ads]`: тот же срок держит суточную воронку, и
    два числа с одним смыслом разошлись бы в первый же раз. С
    `[access] retention_days` это не одно и то же: там про данные клиента,
    который ушёл совсем, а тут про глубину истории у работающего кабинета.
    Ноль и меньше значат «не чистить».
    """
    try:
        section = config.settings().get("storage") or {}
        return int(section.get("daily_history_days", HISTORY_DAYS_DEFAULT))
    except (KeyError, TypeError, ValueError, OSError):
        return HISTORY_DAYS_DEFAULT


def target_drr(client_id: int, *, path: str | Path | None = None) -> Decimal:
    """Целевой ДРР этого селлера. Не задан - значение по умолчанию из конфига.

    Цель одна на кабинет, а не на артикул: у ходового товара и у новинки
    нормальный ДРР правда разный, но экран настройки на каждый артикул это
    уже не одна кнопка, и владелец такого решения не принимал.
    """
    raw = clients.settings_of(client_id, path=path).get(SETTINGS_KEY)
    raw = raw if isinstance(raw, dict) else {}
    value = raw.get("target_drr")
    if value is None:
        return default_target_drr()
    try:
        found = _money(value)
    except Exception:  # noqa: BLE001 - испорченная настройка это «не задана»
        return default_target_drr()
    return found if found > ZERO else default_target_drr()


def set_target_drr(
    client_id: int, percent: Any, *, path: str | Path | None = None
) -> Decimal:
    """Ставит целевой ДРР селлера. Ноль и меньше это возврат к умолчанию."""
    value = _money(percent)
    data = clients.settings_of(client_id, path=path)
    current = data.get(SETTINGS_KEY)
    current = dict(current) if isinstance(current, dict) else {}
    if value <= ZERO:
        current.pop("target_drr", None)
    else:
        current["target_drr"] = str(value)
    data[SETTINGS_KEY] = current
    clients.save_settings(client_id, data, path=path)
    return target_drr(client_id, path=path)


# --- разбор ответов WB -------------------------------------------------------

# Числа, которые Wildberries кладёт на каждом из четырёх уровней ответа
# статистики. Штуки и деньги разведены: деньги идут в копейки, штуки нет.
COUNTERS: tuple[str, ...] = ("views", "clicks", "atbs", "orders", "shks", "canceled")
AMOUNTS: tuple[tuple[str, str], ...] = (
    ("spend_kop", "sum"),
    ("ad_revenue_kop", "sum_price"),
)


def _day_of(row: Mapping[str, Any]) -> str:
    """Дата строки WB в виде ГГГГ-ММ-ДД. Время, если оно есть, отбрасывается."""
    return str((row or {}).get("date") or "")[:10]


def _numbers(row: Mapping[str, Any], counters: Sequence[str] = COUNTERS) -> dict[str, int]:
    values = {name: _int(row.get(name)) for name in counters}
    values.update({column: _kop(row.get(field)) for column, field in AMOUNTS})
    return values


def _add(into: dict[str, int], values: Mapping[str, int]) -> dict[str, int]:
    for name, value in values.items():
        into[name] = into.get(name, 0) + int(value)
    return into


def campaign_days(campaigns: Iterable[Mapping[str, Any]]) -> dict[tuple[str, int], dict]:
    """Кампания за сутки: ключ (дата, номер кампании).

    Разрез приходит готовым, суммировать ничего не нужно: складываем только
    затем, чтобы одна и та же пара из двух ответов не затёрла другую. Пачки
    кампаний и окна дат не пересекаются, поэтому двойного счёта тут нет.
    """
    found: dict[tuple[str, int], dict] = {}
    for campaign in campaigns or []:
        advert_id = _int((campaign or {}).get("advertId"))
        if not advert_id:
            continue
        for day_row in (campaign or {}).get("days") or []:
            if not isinstance(day_row, dict):
                continue
            stamp = _day_of(day_row)
            if not stamp:
                continue
            _add(found.setdefault((stamp, advert_id), {}), _numbers(day_row))
    return found


def campaign_articles(
    campaigns: Iterable[Mapping[str, Any]]
) -> dict[tuple[str, int, int], dict]:
    """Кампания, сутки и артикул: ключ (дата, кампания, артикул).

    Расход по артикулу за день размазан по площадкам (`apps[]`), и вот его
    сложить как раз надо. Артикул везде называется `nmId`; имя `nm` живёт
    только в средних позициях и к расходу отношения не имеет.
    """
    found: dict[tuple[str, int, int], dict] = {}
    for campaign in campaigns or []:
        advert_id = _int((campaign or {}).get("advertId"))
        if not advert_id:
            continue
        for day_row in (campaign or {}).get("days") or []:
            if not isinstance(day_row, dict):
                continue
            stamp = _day_of(day_row)
            if not stamp:
                continue
            for app in day_row.get("apps") or []:
                for item in (app or {}).get("nms") or []:
                    nm_id = _int((item or {}).get("nmId"))
                    if not nm_id:
                        continue
                    key = (stamp, advert_id, nm_id)
                    counters = tuple(name for name in COUNTERS if name != "canceled")
                    _add(found.setdefault(key, {}), _numbers(item, counters))
    return found


def upd_rows(records: Iterable[Mapping[str, Any]], fallback: date) -> list[dict]:
    """История затрат в строки таблицы. Списание без времени идёт на `fallback`.

    Время у Wildberries бывает пустым, а период запроса при этом задан нами.
    Значит, запись точно относится к этому окну, и терять её из-за пустого
    поля нельзя: тогда фактический расход занизился бы, и бот сам себе
    нарисовал бы расхождение, которого нет. Такая запись кладётся на
    последний день окна, и об этом написано на листе «Методология».

    Номер документа тоже бывает пустым, и вот его придумать нельзя: два
    списания по одной кампании за одни сутки без номера ничем не различаются.
    Раньше оба получали номер ноль и схлопывались в одну строку, то есть факт
    занижался. Теперь каждое такое списание получает свой отрицательный номер
    по порядку внутри пары «сутки плюс кампания»: с настоящими номерами
    Wildberries (они положительные) он не столкнётся, а хранить их по
    отдельности позволяет. Порядковый номер придуман нами и сам по себе
    ничего не значит; работает он только потому, что окно списаний
    переписывается целиком, одним ответом Wildberries, а не дополняется
    строками от прошлых заходов (см. `collect`).
    """
    found: list[dict] = []
    nameless: dict[tuple[str, int], int] = {}
    for record in records or []:
        row = record or {}
        advert_id = _int(row.get("advertId"))
        if not advert_id:
            continue
        stamp = str(row.get("updTime") or "")[:10] or fallback.isoformat()
        upd_num = _int(row.get("updNum"))
        if not upd_num:
            slot = nameless.get((stamp, advert_id), 0) + 1
            nameless[(stamp, advert_id)] = slot
            upd_num = -slot
        found.append(
            {
                "date": stamp,
                "advert_id": advert_id,
                "upd_num": upd_num,
                "sum_kop": _kop(row.get("updSum")),
                "payment_type": str(row.get("paymentType") or "").strip(),
                "name": str(row.get("campName") or "").strip(),
            }
        )
    return found


def campaign_names(adverts: Iterable[Mapping[str, Any]]) -> dict[int, dict]:
    """Название, тип и статус кампании из информации о кампаниях.

    Название пишет сам селлер: в книгу Excel оно едет как есть, а в сообщение
    бота попадёт только через `bot.texts.fill`.
    """
    found: dict[int, dict] = {}
    for item in adverts or []:
        row = item or {}
        advert_id = _int(row.get("id", row.get("advertId")))
        if not advert_id:
            continue
        settings = row.get("settings")
        settings = settings if isinstance(settings, dict) else {}
        found[advert_id] = {
            "name": str(settings.get("name") or row.get("name") or "").strip(),
            "advert_type": _int(row.get("type")) or None,
            "status": _int(row.get("status")) or None,
        }
    return found


# --- сбор из WB --------------------------------------------------------------


@dataclass(frozen=True)
class Collected:
    """Чем закончился сбор. `reason` объясняет, чего в базе не прибавилось."""

    campaigns: int = 0
    days: int = 0
    articles: int = 0
    charges: int = 0
    reason: str = OK

    @property
    def ok(self) -> bool:
        return self.reason == OK


def _remember_campaigns(
    client_id: int,
    ids: Sequence[int],
    info: Mapping[int, dict],
    *,
    path: str | Path | None = None,
) -> int:
    """Кладёт номера и названия кампаний в базу.

    Номер записывается даже без названия: кампания есть, а как её зовут, мы
    можем и не узнать. Пустое название в отчёте превратится в номер, и это
    честнее выдуманного имени.
    """
    repo = db.repo(client_id, path)
    stamp = _now()
    for advert_id in ids:
        known = info.get(int(advert_id)) or {}
        values = {"updated_at": stamp}
        if known.get("name"):
            values["name"] = known["name"]
        if known.get("advert_type") is not None:
            values["advert_type"] = known["advert_type"]
        if known.get("status") is not None:
            values["status"] = known["status"]
        repo.upsert("ad_campaigns", {"advert_id": int(advert_id)}, **values)
    return len(ids)


TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def _now() -> str:
    """Отметка времени в UTC: в базе время хранится только так."""
    return datetime.now(timezone.utc).strftime(TIME_FORMAT)


async def collect(
    client_id: int,
    date_from: date,
    date_to: date,
    *,
    http: Any = None,
    path: str | Path | None = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], Any] | None = None,
) -> Collected:
    """Забирает рекламу за период и складывает её в базу. Зовётся из очереди.

    Разбивку по ограничениям Wildberries (не больше 50 кампаний и 31 дня за
    запрос, 3 запроса в минуту) делает `core.wbapi`: помнить её каждому
    вызывающему незачем, а лимит тут второй по жёсткости в проекте. Длинный
    период поэтому идёт долго, и это осознанно: лучше медленнее, чем
    блокировка токена клиента.

    Стадии разведены нарочно. Если названия кампаний или история затрат не
    приехали, статистика всё равно останется в базе: потерять весь сбор из-за
    необязательной части нельзя, а не собранное сегодня Wildberries завтра не
    отдаст. Наружу летит только 401: его разбирает `core.clients`, и без
    этого модули не встанут на паузу.
    """
    client = wbapi.get_wb_client(client_id, http=http, path=path, clock=clock, sleep=sleep)
    try:
        ids = await client.advert_ids()
    except wbapi.WBForbiddenError:
        logger.info("у клиента %s нет категории «Продвижение», рекламу пропускаю", client_id)
        return Collected(reason=NO_CATEGORY)
    except (wbapi.WBUnavailable, wbapi.WBRateLimited, wbapi.WBApiError) as error:
        logger.warning("список кампаний клиента %s не получен: %s", client_id, error)
        return Collected(reason=UNAVAILABLE)

    if not ids:
        return Collected(reason=OK)

    reason = OK

    # Названия. Спрашиваем по всем кампаниям сразу, а не только по незнакомым:
    # дорожка быстрая (5 запросов в секунду, 50 кампаний за запрос), заодно
    # освежается статус, а кампанию селлер может и переименовать.
    info: dict[int, dict] = {}
    try:
        info = campaign_names(await client.adverts_info(ids))
    except (
        wbapi.WBForbiddenError,
        wbapi.WBUnavailable,
        wbapi.WBRateLimited,
        wbapi.WBApiError,
    ) as error:
        logger.warning("названия кампаний клиента %s не получены: %s", client_id, error)
    _remember_campaigns(client_id, ids, info, path=path)

    try:
        stats = await client.fullstats(ids, date_from, date_to)
    except wbapi.WBForbiddenError:
        logger.info("у клиента %s нет категории «Продвижение», рекламу пропускаю", client_id)
        return Collected(campaigns=len(ids), reason=NO_CATEGORY)
    except (wbapi.WBUnavailable, wbapi.WBRateLimited, wbapi.WBApiError) as error:
        logger.warning("статистика рекламы клиента %s не получена: %s", client_id, error)
        return Collected(campaigns=len(ids), reason=UNAVAILABLE)

    repo = db.repo(client_id, path)
    stamp = _now()
    days = campaign_days(stats)
    for (day, advert_id), values in days.items():
        repo.upsert(
            "ad_daily", {"date": day, "advert_id": advert_id}, updated_at=stamp, **values
        )
    articles = campaign_articles(stats)
    for (day, advert_id, nm_id), values in articles.items():
        repo.upsert(
            "ad_nm_daily",
            {"date": day, "advert_id": advert_id, "nm_id": nm_id},
            updated_at=stamp,
            **values,
        )

    # Фактически списанное. Его отсутствие отчёт не роняет: остаётся
    # статистический расход, а сверять его будет не с чем, и так и написано.
    charges = 0
    records: list[dict] = []
    upd_known = True
    try:
        records = upd_rows(await client.advert_upd(date_from, date_to), date_to)
    except (
        wbapi.WBForbiddenError,
        wbapi.WBUnavailable,
        wbapi.WBRateLimited,
        wbapi.WBApiError,
    ) as error:
        logger.warning("история затрат клиента %s не получена: %s", client_id, error)
        upd_known = False

    # Окно списаний переписывается целиком, а не дополняется. Причина в том,
    # что часть ключа строки бот придумывает сам: у списания без времени дата
    # это последний день окна, а у списания без номера документа номер
    # порядковый. Окно суточного сбора скользящее, поэтому дополнение
    # записывало бы одно и то же безвременное списание каждый день под новой
    # датой и складывало бы его с самим собой. Ответ Wildberries за окно и
    # есть вся правда об этом окне, поэтому прежние строки окна уходят, а на
    # их место ложится то, что приехало сейчас. Если поход за историей
    # сорвался, окно не трогается вовсе: пустой ответ это «не знаем», а не
    # «списаний не было».
    if upd_known:
        repo.delete_between(
            "ad_upd", "date", date_from.isoformat(), date_to.isoformat()
        )
    for record in records:
        name = record.pop("name", "")
        repo.upsert(
            "ad_upd",
            {
                "date": record["date"],
                "advert_id": record["advert_id"],
                "upd_num": record["upd_num"],
            },
            sum_kop=record["sum_kop"],
            payment_type=record["payment_type"],
            updated_at=stamp,
        )
        charges += 1
        # История затрат знает название кампании у тех, по которым были
        # списания. Это второй и последний источник имени, и терять его,
        # когда первый не ответил, незачем.
        if name and not (info.get(record["advert_id"]) or {}).get("name"):
            repo.upsert("ad_campaigns", {"advert_id": record["advert_id"]}, name=name)

    return Collected(
        campaigns=len(ids),
        days=len(days),
        articles=len(articles),
        charges=charges,
        reason=reason,
    )


# --- строки отчёта -----------------------------------------------------------


@dataclass(frozen=True)
class Campaign:
    """Одна кампания за период. `None` в деньгах значит «неизвестно»."""

    advert_id: int
    name: str = ""
    status: int | None = None
    spend: Decimal = ZERO
    # Фактически списанное. None значит «истории затрат нет», а не «ноль».
    fact_spend: Decimal | None = None
    ad_revenue: Decimal = ZERO
    # Вся выручка товаров этой кампании из финансового отчёта. None значит
    # «финансовых недель за этот период нет», и второй цифры ДРР не будет.
    revenue: Decimal | None = None
    views: int = 0
    clicks: int = 0
    orders: int = 0
    shks: int = 0
    atbs: int = 0
    canceled: int = 0
    nm_ids: tuple[int, ...] = ()

    @property
    def title(self) -> str:
        """Чем кампания названа в отчёте: имя, а иначе её номер."""
        return self.name or str(self.advert_id)

    @property
    def status_title(self) -> str:
        return STATUS_TITLES.get(int(self.status), "") if self.status is not None else ""

    @property
    def cpo(self) -> Decimal | None:
        return cpo(self.spend, self.orders)

    @property
    def drr_ads(self) -> Decimal | None:
        """ДРР рекламы: обе величины из одной строки ответа Wildberries."""
        return drr(self.spend, self.ad_revenue)

    @property
    def drr_cabinet(self) -> Decimal | None:
        """ДРР кабинета: наш расчёт, расход к выручке товаров кампании."""
        return None if self.revenue is None else drr(self.spend, self.revenue)

    @property
    def gap(self) -> Decimal | None:
        """Насколько списали больше, чем показала статистика."""
        if self.fact_spend is None:
            return None
        return self.fact_spend - self.spend

    def over_target(self, target: Decimal) -> bool:
        """ДРР рекламы выше цели. Без заказов по рекламе это тоже «выше»:
        расход есть, выручки нет, и молчать о такой кампании нельзя."""
        value = self.drr_ads
        if value is None:
            return self.spend > ZERO
        return value > _money(target)


@dataclass(frozen=True)
class Day:
    """Одни сутки по всем кампаниям."""

    date: str
    spend: Decimal = ZERO
    ad_revenue: Decimal = ZERO
    orders: int = 0
    views: int = 0
    clicks: int = 0

    @property
    def cpo(self) -> Decimal | None:
        return cpo(self.spend, self.orders)

    @property
    def drr_ads(self) -> Decimal | None:
        return drr(self.spend, self.ad_revenue)


@dataclass(frozen=True)
class ArticleAds:
    """Один артикул: расход рекламы рядом со всей его выручкой."""

    nm_id: int
    vendor_code: str = ""
    spend: Decimal = ZERO
    ad_revenue: Decimal = ZERO
    revenue: Decimal | None = None
    orders: int = 0

    @property
    def drr_ads(self) -> Decimal | None:
        return drr(self.spend, self.ad_revenue)

    @property
    def drr_cabinet(self) -> Decimal | None:
        return None if self.revenue is None else drr(self.spend, self.revenue)

    @property
    def ad_share(self) -> Decimal | None:
        """Какая доля выручки товара пришла с рекламы, в процентах."""
        return None if self.revenue is None else drr(self.ad_revenue, self.revenue)


@dataclass(frozen=True)
class AdsReport:
    """Данные отчёта. Текстов здесь нет, их знает поверхность бота."""

    client_id: int
    period: str
    date_from: date
    date_to: date
    target: Decimal = TARGET_DRR_DEFAULT
    campaigns: tuple[Campaign, ...] = ()
    days: tuple[Day, ...] = ()
    articles: tuple[ArticleAds, ...] = ()
    # Недели финансового отчёта, из которых взята выручка товаров. Пусто -
    # второй цифры ДРР не будет, и об этом говорится прямо.
    weeks: tuple[Any, ...] = ()
    fact_known: bool = False
    trouble: str = ""

    @property
    def empty(self) -> bool:
        return not self.campaigns

    @property
    def title(self) -> str:
        return finance.PERIOD_TITLES.get(self.period, self.period)

    @property
    def revenue_known(self) -> bool:
        """Есть ли из чего посчитать вторую цифру ДРР."""
        return bool(self.weeks)

    @property
    def spend(self) -> Decimal:
        return sum((item.spend for item in self.campaigns), ZERO)

    @property
    def fact_spend(self) -> Decimal | None:
        if not self.fact_known:
            return None
        return sum(
            (item.fact_spend or ZERO for item in self.campaigns), ZERO
        )

    @property
    def gap(self) -> Decimal | None:
        """Расхождение статистики и фактически списанного, по всему кабинету."""
        fact = self.fact_spend
        return None if fact is None else fact - self.spend

    @property
    def gap_matters(self) -> bool:
        gap = self.gap
        return gap is not None and abs(gap) > TOLERANCE

    @property
    def ad_revenue(self) -> Decimal:
        return sum((item.ad_revenue for item in self.campaigns), ZERO)

    @property
    def revenue(self) -> Decimal | None:
        """Вся выручка рекламируемых товаров. Каждый товар посчитан один раз."""
        if not self.revenue_known:
            return None
        return sum((item.revenue or ZERO for item in self.articles), ZERO)

    @property
    def orders(self) -> int:
        return sum(item.orders for item in self.campaigns)

    @property
    def cpo(self) -> Decimal | None:
        return cpo(self.spend, self.orders)

    @property
    def drr_ads(self) -> Decimal | None:
        return drr(self.spend, self.ad_revenue)

    @property
    def drr_cabinet(self) -> Decimal | None:
        return None if self.revenue is None else drr(self.spend, self.revenue)

    @property
    def ad_share(self) -> Decimal | None:
        """Доля выручки рекламируемых товаров, пришедшая с рекламы."""
        return None if self.revenue is None else drr(self.ad_revenue, self.revenue)

    @property
    def over_target(self) -> tuple[Campaign, ...]:
        """Кампании с ДРР выше цели, от самой дорогой вниз."""
        return tuple(
            item for item in self.campaigns if item.spend > ZERO and item.over_target(self.target)
        )

    @property
    def verdict(self) -> str:
        """Что означает расхождение двух цифр. Пусто - вывода не делаем.

        Вторая цифра всегда не больше первой: выручка товара не меньше той
        части, которую Wildberries приписал рекламе. Значит, сошлись они или
        разошлись, и есть тот самый ответ на вопрос «товар продаётся сам или
        живёт на рекламе».
        """
        share = self.ad_share
        if share is None:
            return ""
        if share >= ad_driven_share():
            return "ad_driven"
        if share <= self_selling_share():
            return "self_selling"
        return ""


# --- сборка отчёта -----------------------------------------------------------


def _revenue_by_article(
    client_id: int,
    date_from: date,
    date_to: date,
    *,
    path: str | Path | None = None,
) -> tuple[dict[int, Decimal], tuple[Any, ...]]:
    """Выручка товаров из недель финансового отчёта, попавших в период.

    Разрез по артикулам один на проект, и живёт он у агента 1: своей копии
    здесь нет намеренно, иначе два места решали бы по-разному, что такое
    «выручка артикула» и «возврат». Границы недель Wildberries и границы
    периода не совпадают, поэтому недели, из которых взята выручка, отчёт
    называет датами, а не прячет.
    """
    weeks = finance.weeks_of(client_id, date_from, date_to, path=path)
    if not weeks:
        return {}, ()
    found: dict[int, Decimal] = {}
    for item in finance.articles_of(client_id, weeks, path=path):
        if item.nm_id is None:
            continue
        found[int(item.nm_id)] = (
            item.amounts.revenue - item.amounts.returns_amount
        )
    return found, tuple(weeks)


def build(
    client_id: int,
    period: str = "month",
    *,
    today: date | None = None,
    path: str | Path | None = None,
    trouble: str = "",
) -> AdsReport:
    """Отчёт по рекламе за период. В WB отсюда не ходят ни разу.

    Всё уже лежит в базе: расход положил сбор этого же агента, выручку
    товаров агент 1. Отчёт только читает и считает.
    """
    date_from, date_to = finance.period_bounds(period, today)
    first, last = date_from.isoformat(), date_to.isoformat()
    repo = db.repo(client_id, path)

    names: dict[int, dict] = {}
    for row in repo.rows("ad_campaigns"):
        names[int(row["advert_id"])] = {
            "name": str(row["name"] or ""),
            "status": None if row["status"] is None else int(row["status"]),
        }

    # Суточные таблицы читаются одним запросом на границы периода, а не
    # запросом на каждый его день и не целиком: индексы по дате в схеме стоят
    # ровно для этого. Целиком читается только справочник кампаний: в нём
    # строка на кампанию, а не на кампанию за сутки. Дат в базе нет иных, кроме
    # ГГГГ-ММ-ДД, поэтому сравнение строк тут это сравнение дат.
    totals: dict[int, dict[str, Any]] = {}
    days: dict[str, dict[str, Any]] = {}
    for row in repo.rows_between("ad_daily", "date", first, last):
        stamp = str(row["date"])
        advert_id = int(row["advert_id"])
        slot = totals.setdefault(
            advert_id,
            {name: 0 for name in COUNTERS} | {"spend_kop": 0, "ad_revenue_kop": 0},
        )
        day = days.setdefault(
            stamp, {"spend_kop": 0, "ad_revenue_kop": 0, "orders": 0, "views": 0, "clicks": 0}
        )
        for name in COUNTERS:
            slot[name] += int(row[name] or 0)
        for column in ("spend_kop", "ad_revenue_kop"):
            slot[column] += int(row[column] or 0)
            day[column] += int(row[column] or 0)
        for name in ("orders", "views", "clicks"):
            day[name] += int(row[name] or 0)

    # Какие товары рекламировала каждая кампания и сколько на них ушло.
    by_campaign_nm: dict[int, set[int]] = {}
    by_article: dict[int, dict[str, int]] = {}
    for row in repo.rows_between("ad_nm_daily", "date", first, last):
        advert_id = int(row["advert_id"])
        nm_id = int(row["nm_id"])
        by_campaign_nm.setdefault(advert_id, set()).add(nm_id)
        slot = by_article.setdefault(
            nm_id, {"spend_kop": 0, "ad_revenue_kop": 0, "orders": 0}
        )
        slot["spend_kop"] += int(row["spend_kop"] or 0)
        slot["ad_revenue_kop"] += int(row["ad_revenue_kop"] or 0)
        slot["orders"] += int(row["orders"] or 0)

    charges: dict[int, int] = {}
    fact_known = False
    for row in repo.rows_between("ad_upd", "date", first, last):
        fact_known = True
        advert_id = int(row["advert_id"])
        charges[advert_id] = charges.get(advert_id, 0) + int(row["sum_kop"] or 0)

    revenue, weeks = _revenue_by_article(client_id, date_from, date_to, path=path)
    known_revenue = bool(weeks)

    campaigns = []
    for advert_id, slot in totals.items():
        info = names.get(advert_id) or {}
        nm_ids = tuple(sorted(by_campaign_nm.get(advert_id, ())))
        # Выручка товаров кампании. Один товар может быть в двух кампаниях, и
        # тогда его выручка учтена в обеих: это написано на листе
        # «Методология», а делить её между кампаниями было бы выдумкой.
        campaign_revenue = None
        if known_revenue:
            campaign_revenue = sum((revenue.get(nm_id, ZERO) for nm_id in nm_ids), ZERO)
        campaigns.append(
            Campaign(
                advert_id=advert_id,
                name=info.get("name") or "",
                status=info.get("status"),
                spend=db.from_kop(slot["spend_kop"]),
                fact_spend=db.from_kop(charges[advert_id]) if advert_id in charges else (
                    ZERO if fact_known else None
                ),
                ad_revenue=db.from_kop(slot["ad_revenue_kop"]),
                revenue=campaign_revenue,
                views=slot["views"],
                clicks=slot["clicks"],
                orders=slot["orders"],
                shks=slot["shks"],
                atbs=slot["atbs"],
                canceled=slot["canceled"],
                nm_ids=nm_ids,
            )
        )
    campaigns.sort(key=lambda item: (-item.spend, item.advert_id))

    article_rows = [
        ArticleAds(
            nm_id=nm_id,
            spend=db.from_kop(slot["spend_kop"]),
            ad_revenue=db.from_kop(slot["ad_revenue_kop"]),
            revenue=revenue.get(nm_id, ZERO) if known_revenue else None,
            orders=slot["orders"],
        )
        for nm_id, slot in by_article.items()
    ]
    article_rows.sort(key=lambda item: (-item.spend, item.nm_id))

    day_rows = [
        Day(
            date=stamp,
            spend=db.from_kop(slot["spend_kop"]),
            ad_revenue=db.from_kop(slot["ad_revenue_kop"]),
            orders=slot["orders"],
            views=slot["views"],
            clicks=slot["clicks"],
        )
        for stamp, slot in sorted(days.items())
    ]

    return AdsReport(
        client_id=client_id,
        period=period,
        date_from=date_from,
        date_to=date_to,
        target=target_drr(client_id, path=path),
        campaigns=tuple(campaigns),
        days=tuple(day_rows),
        articles=tuple(article_rows),
        weeks=weeks,
        fact_known=fact_known,
        trouble=trouble,
    )


# --- книга Excel -------------------------------------------------------------

CAMPAIGNS_SHEET = "По кампаниям"
DAYS_SHEET = "По дням"
ARTICLES_SHEET = "По артикулам"
METHOD_SHEET = "Методология"

NO_DATA = "нет данных"

CAMPAIGN_HEADERS = (
    "Кампания",
    "Номер",
    "Статус",
    "Расход, ₽",
    "Фактически списано, ₽",
    "Заказы, шт",
    "Цена заказа, ₽",
    "Выручка от рекламы, ₽",
    "ДРР рекламы, %",
    "Выручка товаров, ₽",
    "ДРР кабинета, %",
    "Цель, %",
    "Показы",
    "Клики",
)

DAY_HEADERS = (
    "Дата",
    "Расход, ₽",
    "Заказы, шт",
    "Цена заказа, ₽",
    "Выручка от рекламы, ₽",
    "ДРР рекламы, %",
    "Показы",
    "Клики",
)

ARTICLE_HEADERS = (
    "Артикул WB",
    "Расход, ₽",
    "Заказы, шт",
    "Выручка от рекламы, ₽",
    "ДРР рекламы, %",
    "Выручка всего, ₽",
    "ДРР кабинета, %",
    "Доля выручки от рекламы, %",
)

METHOD_HEADERS = ("Показатель", "Источник", "Формула", "Пояснение")

# Каждая строка: показатель, источник, формула, пояснение. По ней цифру можно
# проверить руками. Разница между двумя цифрами ДРР названа здесь прямым
# текстом, а не подразумевается.
METHODOLOGY: tuple[tuple[str, str, str, str], ...] = (
    (
        "Расход, ₽",
        "GET /adv/v3/fullstats, поле sum",
        "сумма sum по дням кампании",
        "Это статистический расход: столько Wildberries насчитал по показам и "
        "кликам. Ограничения метода: не больше 50 кампаний и 31 дня за запрос, "
        "3 запроса в минуту. Статистика приходит только по кампаниям в "
        "статусах «завершена», «активна» и «на паузе»: удалённая кампания "
        "уносит свою историю с собой, поэтому бот забирает расход каждый день "
        "и хранит его у себя.",
    ),
    (
        "Фактически списано, ₽",
        "GET /adv/v1/upd, поле updSum",
        "сумма updSum по кампании за период",
        "Это выставленная сумма, и она может не сойтись со статистической: "
        "часть расхода могла уйти бонусами или кэшбэком. Расхождение больше "
        "рубля показывается, а не прячется. Списание, у которого Wildberries "
        "не указал время, отнесено к последнему дню запрошенного окна: оно "
        "точно внутри периода, а выбросить его значило бы занизить факт. "
        "Учтено оно при этом один раз, сколько бы раз бот ни забирал этот "
        "период заново. Списания без номера документа тоже сохраняются все: "
        "различить их нечем, но сумма от этого не страдает.",
    ),
    (
        "Заказы, шт",
        "GET /adv/v3/fullstats, поле orders",
        "сумма orders",
        "Заказы, которые Wildberries приписал рекламе. Это не все заказы "
        "товара: заказ, пришедший без рекламы, сюда не попадает.",
    ),
    (
        "Цена заказа (CPO), ₽",
        "-",
        "расход / заказы",
        "Готового поля у Wildberries нет, это наше деление. Без заказов цены "
        "заказа не существует, и в клетке стоит «нет данных», а не ноль.",
    ),
    (
        "ДРР рекламы, %",
        "GET /adv/v3/fullstats, поля sum и sum_price",
        "sum / sum_price * 100",
        "Обе величины лежат в одной строке ответа Wildberries, сшивки "
        "источников здесь нет. Готового поля ДРР у Wildberries тоже нет, "
        "процент наш, поэтому он может не совпасть с тем, что показывает "
        "рекламный кабинет. Если заказов по рекламе не было, sum_price равен "
        "нулю, и процента не существует: делить на ноль нельзя.",
    ),
    (
        "Выручка товаров и ДРР кабинета, %",
        "fin_rows (агент «Финансист») плюс расход рекламы",
        "расход / выручка товаров * 100",
        "Это наш расчёт, а не поле Wildberries: расход берётся из рекламы, а "
        "вся выручка товара из недельных отчётов о реализации. Выручка взята "
        "за вычетом возвратов. Границы недель Wildberries и границы периода "
        "не совпадают, поэтому недели, из которых взята выручка, названы "
        "датами в сообщении к отчёту. Без модуля «Финансы» этой цифры нет "
        "вовсе, и в клетке стоит «нет данных»: выдумывать её не из чего.",
    ),
    (
        "Почему цифр ДРР две",
        "-",
        "-",
        "Вторая цифра всегда не больше первой: вся выручка товара не меньше "
        "той её части, которую Wildberries приписал рекламе. Поэтому важно "
        "не каждая цифра сама по себе, а насколько они разошлись. Сошлись "
        "почти в одну - товар живёт на рекламе и сам почти не продаётся: "
        "выключите кампанию, и выручка уйдёт вместе с ней. Разошлись сильно - "
        "товар продаётся сам, а реклама только добавляет сверху.",
    ),
    (
        "Выручка товаров кампании, ₽",
        "fin_rows по артикулам кампании",
        "сумма выручки товаров, которые кампания рекламировала",
        "Какие товары рекламировала кампания, видно из её же статистики. Если "
        "один товар рекламируют две кампании, его выручка учтена в обеих: "
        "делить её между кампаниями было бы выдумкой. Поэтому сумма по "
        "кампаниям может оказаться больше выручки кабинета, а строка «Итого» "
        "по этой колонке не складывается.",
    ),
    (
        "Цель, %",
        "настройки селлера, команда /settings",
        "-",
        "Целевой ДРР ставите вы сами. Пока он не задан, берётся значение по "
        "умолчанию из настроек бота. Цель одна на кабинет.",
    ),
)


def _cell(value: Any) -> Any:
    """Деньги с копейками, а «неизвестно» словами, а не пустой клеткой."""
    if value is None:
        return NO_DATA
    if isinstance(value, Decimal):
        return value.quantize(CENT, rounding=ROUND_HALF_UP)
    return value


def excel_sheets(report: AdsReport) -> list:
    """Четыре листа книги: кампании, дни, артикулы и методология."""
    from core.xlsx import Sheet

    campaigns = [
        [
            item.title,
            item.advert_id,
            item.status_title,
            _cell(item.spend),
            _cell(item.fact_spend),
            item.orders,
            _cell(item.cpo),
            _cell(item.ad_revenue),
            _cell(item.drr_ads),
            _cell(item.revenue),
            _cell(item.drr_cabinet),
            _cell(report.target),
            item.views,
            item.clicks,
        ]
        for item in report.campaigns
    ]
    days = [
        [
            item.date,
            _cell(item.spend),
            item.orders,
            _cell(item.cpo),
            _cell(item.ad_revenue),
            _cell(item.drr_ads),
            item.views,
            item.clicks,
        ]
        for item in report.days
    ]
    articles = [
        [
            item.nm_id,
            _cell(item.spend),
            item.orders,
            _cell(item.ad_revenue),
            _cell(item.drr_ads),
            _cell(item.revenue),
            _cell(item.drr_cabinet),
            _cell(item.ad_share),
        ]
        for item in report.articles
    ]
    return [
        Sheet(CAMPAIGNS_SHEET, CAMPAIGN_HEADERS, campaigns),
        Sheet(DAYS_SHEET, DAY_HEADERS, days),
        Sheet(ARTICLES_SHEET, ARTICLE_HEADERS, articles),
        Sheet(METHOD_SHEET, METHOD_HEADERS, [list(row) for row in METHODOLOGY]),
    ]


def excel_bytes(report: AdsReport) -> bytes:
    """Книга целиком, байтами: её чаще отправляют, чем сохраняют."""
    from core.xlsx import write_book

    return write_book(excel_sheets(report))


def file_name(report: AdsReport) -> str:
    return f"ads-{report.date_from.isoformat()}-{report.date_to.isoformat()}.xlsx"


# --- очередь и расписание ----------------------------------------------------

_sender: Callable[[int, AdsReport, bytes], Any] | None = None


def set_sender(fn: Callable[[int, AdsReport, bytes], Any] | None) -> None:
    """Чем отдаётся готовый отчёт: `fn(client_id, report, xlsx)`."""
    global _sender
    _sender = fn


def request_report(
    client_id: int, period: str = "month", *, path: str | Path | None = None
) -> queue.TaskId:
    """Ставит отчёт в очередь. «Принято» клиенту говорит сама очередь.

    Из хендлера в WB не ходят: у статистики рекламы лимит 3 запроса в минуту,
    и повтор при недоступности Wildberries живёт только в очереди.
    """
    if period not in PERIODS:
        raise ValueError(f"неизвестный период: {period}")
    return queue.enqueue(client_id, TASK_KIND, {"period": period}, path=path)


def _payload_date(task: Any) -> date:
    raw = str((getattr(task, "payload", None) or {}).get("date") or "")
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return datetime.now(scheduler.tz()).date()


def _connected_clients(path: str | Path | None = None) -> list[int]:
    """Клиенты с подключённым кабинетом. Подписка тут ни при чём."""
    found: list[int] = []
    for row in db.admin_repo(path).all_clients():
        client_id = int(row["id"])
        try:
            if db.repo(client_id, path).count("wb_tokens"):
                found.append(client_id)
        except Exception:  # noqa: BLE001 - один клиент не ломает обход
            logger.exception("не удалось проверить кабинет клиента %s", client_id)
    return found


def fan_out_collect(task: Any, *, path: str | Path | None = None) -> list[int]:
    """Суточный сбор: по задаче на каждый подключённый кабинет.

    **Доступ к модулю здесь не проверяется, и это не упущение.** Кампанию
    селлер может удалить в любой день, и вместе с ней Wildberries перестанет
    отдавать её статистику навсегда. Подписку клиент может оформить и через
    месяц, а не собранный расход не вернуть.
    """
    stamp = _payload_date(task).isoformat()
    return [
        queue.enqueue(client_id, COLLECT_ONE, {"date": stamp}, notify=False, path=path)
        for client_id in _connected_clients(path)
    ]


CLEANED_TABLES = ("ad_daily", "ad_nm_daily", "ad_upd")


def cleanup(task: Any = None, *, path: str | Path | None = None) -> int:
    """Убирает суточную рекламу глубже срока хранения. Возвращает число строк.

    В Wildberries отсюда не ходят: это работа по базе. Справочник кампаний
    (`ad_campaigns`) не чистится: в нём строка на кампанию, а не на кампанию
    за сутки, и по нему в отчёте находится название.
    """
    days = history_days()
    if days <= 0:
        return 0
    edge = (_payload_date(task) - timedelta(days=days)).isoformat()
    removed = 0
    for row in db.admin_repo(path).all_clients():
        client_id = int(row["id"])
        try:
            repo = db.repo(client_id, path)
            for table in CLEANED_TABLES:
                removed += repo.delete_before(table, "date", edge)
        except Exception:  # noqa: BLE001 - один клиент не ломает обход
            logger.exception("не удалось почистить рекламу клиента %s", client_id)
    return removed


async def collect_client(task: Any, *, path: str | Path | None = None) -> Collected:
    """Сбор одного кабинета за окно из конфига. Повтор делает очередь."""
    day = _payload_date(task)
    start = day - timedelta(days=max(1, collect_days()) - 1)
    return await collect(int(task.client_id), start, day, path=path)


async def report_task(task: Any, *, path: str | Path | None = None) -> AdsReport:
    """Обработчик задачи клиента: собрать период и сразу отдать собранное.

    Сбор идёт и по расписанию, но клиент мог подключиться вчера, а спросить
    квартал: без похода в WB прямо сейчас он получил бы пустую таблицу.
    """
    client_id = int(task.client_id)
    period = str((task.payload or {}).get("period") or "month")
    date_from, date_to = finance.period_bounds(period)
    collected = await collect(client_id, date_from, date_to, path=path)
    report = build(
        client_id,
        period,
        path=path,
        trouble="" if collected.ok else collected.reason,
    )
    if _sender is None:
        raise RuntimeError("некому отправить отчёт по рекламе: доставка не подключена")
    result = _sender(client_id, report, excel_bytes(report))
    if hasattr(result, "__await__"):
        await result
    return report


def register_jobs() -> None:
    """Ставит суточный сбор в расписание, а разбор по клиентам в очередь."""
    scheduler.register_daily(COLLECT_ALL, fan_out_collect)
    # Чистка идёт тем же утром и в WB не ходит: это работа по базе.
    scheduler.register_daily(CLEANUP, cleanup)
    # Сбор идёт ночью и клиентом не заказан: о его сбое знает владелец.
    queue.register(COLLECT_ONE, collect_client, quiet=True)
    queue.register(TASK_KIND, report_task, title="разбор рекламы")
