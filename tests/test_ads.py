"""Агент 5, реклама.

Швы те же два, что и у всех: путь к базе (временный файл) и транспорт WB
(`httpx.MockTransport` с записанными ответами, сети нет ни байта). Расчёты -
ДРР, цена заказа, расхождение статистики и счёта - чистые и проверяются
напрямую.

Ожидаемые числа посчитаны руками и записаны рядом в комментариях: считать их
тем же способом, что и код, значит не проверить ничего.

Транспорт отвечает не «что попало на любой запрос», а ровно тем, что попало
в запрошенное окно дат и в запрошенный список кампаний. Иначе проверка
ограничений Wildberries (50 кампаний и 31 день за запрос) доказывала бы
только то, что мок покладистый.
"""

from __future__ import annotations

import base64
import copy
import functools
import json
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from agents import ads, finance
from bot.handlers import ads as handlers_ads
from bot.handlers import settings as settings_handler
from bot.handlers import tariffs
from core import access, clients, config, crypto, db, queue, wbapi, xlsx
from core.wbapi import client as wb_client

TODAY = date(2026, 9, 30)

# Неделя отчёта о реализации: она даёт вторую цифру ДРР. Взята той же формы,
# что и у соседних агентов, чтобы «выручка артикула» значила ровно то же.
FIN_ROWS = [
    {
        "reportId": 1,
        "rrdId": 1,
        "dateFrom": "2026-09-01",
        "dateTo": "2026-09-07",
        "nmId": 111,
        "vendorCode": "A-1",
        "subjectName": "Кружка",
        "docTypeName": "Продажа",
        "quantity": 2,
        "retailAmount": 4000,
        "retailPriceWithDisc": 2000,
        "forPay": 3000,
        "vw": 560,
    },
    {
        "reportId": 1,
        "rrdId": 2,
        "dateFrom": "2026-09-01",
        "dateTo": "2026-09-07",
        "nmId": 222,
        "vendorCode": "B-2",
        "subjectName": "Ложка",
        "docTypeName": "Продажа",
        "quantity": 1,
        "retailAmount": 1000,
        "retailPriceWithDisc": 1000,
        "forPay": 700,
        "vw": 170,
    },
    {
        "reportId": 1,
        "rrdId": 3,
        "dateFrom": "2026-09-01",
        "dateTo": "2026-09-07",
        "nmId": 111,
        "vendorCode": "A-1",
        "subjectName": "Кружка",
        "docTypeName": "Возврат",
        "quantity": 1,
        "retailAmount": 800,
        "retailPriceWithDisc": 800,
        "vw": -100,
    },
]

# Выручка за вычетом возвратов: 111 это 4000 - 800 = 3200, 222 это 1000.
REVENUE = {111: Decimal("3200"), 222: Decimal("1000")}

# Две кампании. Расход и выручка от рекламы лежат в одной строке ответа: это
# и есть первая цифра ДРР, сшивать для неё ничего не надо.
FULLSTATS = [
    {
        "advertId": 777,
        "currency": "RUB",
        "days": [
            {
                "date": "2026-09-01T00:00:00+03:00",
                "views": 1000,
                "clicks": 50,
                "atbs": 10,
                "orders": 4,
                "shks": 4,
                "canceled": 0,
                "sum": 600,
                "sum_price": 2000,
                "apps": [
                    {
                        "appType": 1,
                        "nms": [
                            {
                                "nmId": 111,
                                "views": 700,
                                "clicks": 35,
                                "orders": 3,
                                "sum": 400,
                                "sum_price": 1500,
                            }
                        ],
                    },
                    {
                        "appType": 32,
                        "nms": [
                            {
                                "nmId": 111,
                                "views": 300,
                                "clicks": 15,
                                "orders": 1,
                                "sum": 200,
                                "sum_price": 500,
                            }
                        ],
                    },
                ],
            },
            {
                "date": "2026-09-02T00:00:00+03:00",
                "views": 800,
                "clicks": 20,
                "atbs": 1,
                "orders": 0,
                "shks": 0,
                "canceled": 0,
                "sum": 400,
                "sum_price": 0,
                "apps": [
                    {
                        "appType": 1,
                        "nms": [
                            {
                                "nmId": 111,
                                "views": 800,
                                "clicks": 20,
                                "orders": 0,
                                "sum": 400,
                                "sum_price": 0,
                            }
                        ],
                    }
                ],
            },
        ],
    },
    {
        "advertId": 888,
        "currency": "RUB",
        "days": [
            {
                "date": "2026-09-01T00:00:00+03:00",
                "views": 200,
                "clicks": 10,
                "atbs": 2,
                "orders": 2,
                "shks": 2,
                "canceled": 0,
                "sum": 100,
                "sum_price": 1000,
                "apps": [
                    {
                        "appType": 1,
                        "nms": [
                            {
                                "nmId": 222,
                                "views": 200,
                                "clicks": 10,
                                "orders": 2,
                                "sum": 100,
                                "sum_price": 1000,
                            }
                        ],
                    }
                ],
            }
        ],
    },
]

ADVERTS = [
    {"id": 777, "type": 9, "status": 9, "settings": {"name": "Кружки осень"}},
    {"id": 888, "type": 9, "status": 11, "settings": {"name": "Ложки"}},
]

# Списали больше, чем показала статистика: 700 + 450 + 100 это 1250 против
# 1100. Часть ушла бонусами, и об этом говорит paymentType.
UPD = [
    {
        "updNum": 1,
        "updTime": "2026-09-01T10:00:00+03:00",
        "updSum": 700,
        "advertId": 777,
        "campName": "Кружки осень",
        "paymentType": "Счёт",
    },
    {
        "updNum": 2,
        "updTime": "2026-09-02T10:00:00+03:00",
        "updSum": 450,
        "advertId": 777,
        "paymentType": "Бонусы",
    },
    {
        "updNum": 3,
        "updTime": "2026-09-01T10:00:00+03:00",
        "updSum": 100,
        "advertId": 888,
        "paymentType": "Счёт",
    },
]

COUNT_PATH = "/adv/v1/promotion/count"
ADVERTS_PATH = "/api/advert/v2/adverts"
STATS_PATH = "/adv/v3/fullstats"
UPD_PATH = "/adv/v1/upd"

MASK_FIVE = 8294
EXP = 1789000000


def make_token() -> str:
    """JWT с нужным payload. Подпись не проверяется, она тут не нужна."""

    def part(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    body = {"id": "cd" * 8, "sid": "s" * 8, "exp": EXP, "s": MASK_FIVE, "acc": 3}
    return f"{part({'alg': 'HS256', 'typ': 'JWT'})}.{part(body)}.c2lnbmF0dXJl"


class FakeTime:
    """Часы и пауза под контролем теста: бюджет запросов к WB не ждём вживую."""

    def __init__(self) -> None:
        self.now = 1000.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += float(seconds)


class Recorder:
    """Записанные ответы WB плюс журнал запросов. Сети в тестах нет.

    Статистика и история затрат отвечают только тем, что попало в окно дат и
    в список кампаний запроса: так ведёт себя Wildberries, и только так
    проверка нарезки что-то доказывает.
    """

    def __init__(self, stats=FULLSTATS, upd=UPD, adverts=ADVERTS, codes=None) -> None:
        self.stats = stats
        self.upd = upd
        self.adverts = adverts
        self.codes = codes or {}
        self.calls: list[tuple[str, dict]] = []

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def paths(self) -> list[str]:
        return [path for path, _ in self.calls]

    def params(self, path: str) -> list[dict]:
        return [query for spot, query in self.calls if spot == path]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        query = dict(request.url.params)
        self.calls.append((path, query))
        code = self.codes.get(path)
        if code:
            return httpx.Response(code, json={"title": "нет"})
        if path == COUNT_PATH:
            ids = sorted({item["advertId"] for item in self.stats})
            return httpx.Response(
                200,
                json={"adverts": [{"advert_list": [{"advertId": value} for value in ids]}]},
            )
        if path == ADVERTS_PATH:
            wanted = {int(value) for value in query.get("ids", "").split(",") if value}
            return httpx.Response(
                200, json=[item for item in self.adverts if item["id"] in wanted]
            )
        if path == STATS_PATH:
            return httpx.Response(200, json=self._stats(query))
        if path == UPD_PATH:
            return httpx.Response(200, json=self._charges(query))
        return httpx.Response(200, json={})

    def _stats(self, query: dict) -> list[dict]:
        wanted = {int(value) for value in query.get("ids", "").split(",") if value}
        begin, end = query.get("beginDate", ""), query.get("endDate", "")
        found = []
        for campaign in self.stats:
            if campaign["advertId"] not in wanted:
                continue
            days = [
                day for day in campaign["days"] if begin <= str(day["date"])[:10] <= end
            ]
            if days:
                found.append({**campaign, "days": days})
        return found

    def _charges(self, query: dict) -> list[dict]:
        begin, end = query.get("from", ""), query.get("to", "")
        # Списание без времени Wildberries всё равно отдаёт: окно запроса
        # задали мы сами, и запись в него попала.
        return [
            item
            for item in self.upd
            if not item.get("updTime")
            or begin <= str(item["updTime"])[:10] <= end
        ]


@pytest.fixture
def cabinet(db_path, monkeypatch):
    """Подключённый кабинет с неделей финансового отчёта под второй цифрой."""
    monkeypatch.setenv("ENCRYPTION_KEY", crypto.generate_key())
    wbapi.reset_limits()
    queue.reset()
    client_id = db.admin_repo(db_path).ensure_client(8080)
    db.repo(client_id, db_path).insert(
        "wb_tokens", ciphertext=crypto.encrypt(make_token()), exp=str(EXP), scopes="promotion"
    )
    finance.save_rows(client_id, FIN_ROWS, path=db_path)
    for week in finance.aggregate(FIN_ROWS).values():
        finance.save_week(client_id, week, path=db_path)
    return client_id


async def collect_into(client_id, db_path, recorder, *, days=30):
    clock = FakeTime()
    return await ads.collect(
        client_id,
        TODAY - timedelta(days=days),
        TODAY,
        path=db_path,
        http=recorder.client(),
        clock=clock.clock,
        sleep=clock.sleep,
    )


def report_of(client_id, db_path, **kw):
    return ads.build(client_id, kw.pop("period", "month"), today=TODAY, path=db_path, **kw)


def campaign(report, advert_id):
    for item in report.campaigns:
        if item.advert_id == advert_id:
            return item
    raise AssertionError(f"кампании {advert_id} нет в отчёте")


# --- чистые расчёты ---


def test_drr_is_spend_over_revenue_in_percent():
    # 1000 расхода при 4000 выручки это 25 процентов.
    assert ads.drr(Decimal("1000"), Decimal("4000")) == Decimal("25.00")


def test_drr_without_revenue_is_unknown_not_zero_and_not_infinity():
    """Расход есть, выручки нет: процента не существует.

    Ноль прочитался бы как «реклама бесплатна», а прочерк как «всё хорошо».
    Это самый частый случай у новых кампаний, и молчать о нём нельзя.
    """
    assert ads.drr(Decimal("1000"), Decimal("0")) is None
    assert ads.drr(Decimal("1000"), Decimal("-5")) is None


def test_cpo_without_orders_does_not_exist():
    assert ads.cpo(Decimal("1000"), 4) == Decimal("250.00")
    assert ads.cpo(Decimal("1000"), 0) is None


# --- две цифры ДРР ---


@pytest.mark.asyncio
async def test_both_drr_numbers_are_counted_the_way_the_owner_asked(cabinet, db_path):
    """Первая цифра из одного источника, вторая сшита из двух.

    Числа руками. Кампания 777: расход 600 + 400 = 1000, выручка от рекламы
    2000 + 0 = 2000, заказов 4. ДРР рекламы 1000 / 2000 = 50 процентов, цена
    заказа 1000 / 4 = 250 рублей. Вся выручка её товара (111) по финансовому
    отчёту 4000 - 800 = 3200, значит ДРР кабинета 1000 / 3200 = 31,25
    процента.

    Кампания 888: расход 100, выручка от рекламы 1000, ДРР рекламы 10
    процентов; выручка товара 222 это 1000, ДРР кабинета тоже 10 процентов.
    """
    recorder = Recorder()
    await collect_into(cabinet, db_path, recorder)
    report = report_of(cabinet, db_path)

    first = campaign(report, 777)
    assert first.spend == Decimal("1000")
    assert first.ad_revenue == Decimal("2000")
    assert first.orders == 4
    assert first.cpo == Decimal("250.00")
    assert first.drr_ads == Decimal("50.00")
    assert first.revenue == REVENUE[111]
    assert first.drr_cabinet == Decimal("31.25")

    second = campaign(report, 888)
    assert second.drr_ads == Decimal("10.00")
    assert second.drr_cabinet == Decimal("10.00")

    # По кабинету: расход 1100, выручка от рекламы 3000, вся выручка
    # рекламируемых товаров 3200 + 1000 = 4200.
    assert report.spend == Decimal("1100")
    assert report.ad_revenue == Decimal("3000")
    assert report.revenue == Decimal("4200")
    # 1100 / 3000 = 36,67 процента и 1100 / 4200 = 26,19 процента.
    assert report.drr_ads == Decimal("36.67")
    assert report.drr_cabinet == Decimal("26.19")


@pytest.mark.asyncio
async def test_the_second_number_is_called_our_own_calculation(cabinet, db_path):
    """Сшитое из двух источников число обязано быть названо нашим расчётом.

    Первая цифра это отношение двух полей одного ответа Wildberries, и её
    можно называть его цифрой. Вторая собрана из рекламы и финансового
    отчёта, и показать её молча значит соврать уверенным голосом.
    """
    recorder = Recorder()
    await collect_into(cabinet, db_path, recorder)
    text = handlers_ads.summary_text(report_of(cabinet, db_path))

    first, _, second = text.partition("ДРР кабинета")
    assert "ДРР рекламы" in first
    # У первой цифры сказано, что оба числа Wildberries.
    assert "Оба числа его" in first
    # У второй сказано, что это наш расчёт.
    assert "Наш расчёт" in second


@pytest.mark.asyncio
async def test_the_gap_between_two_drr_numbers_is_named_out_loud(cabinet, db_path):
    """Ценность в расхождении, и сказать о нём должен бот, а не селлер сам.

    С рекламы пришло 3000 из 4200 выручки, это 71,43 процента: выше границы
    из конфига, значит товары живут на рекламе. Об этом и написано.
    """
    recorder = Recorder()
    await collect_into(cabinet, db_path, recorder)
    report = report_of(cabinet, db_path)

    assert report.ad_share == Decimal("71.43")
    assert report.verdict == "ad_driven"
    text = handlers_ads.summary_text(report)
    assert "почти не продаются" in text
    assert "71,43%" in text


@pytest.mark.asyncio
async def test_a_self_selling_cabinet_gets_the_opposite_verdict(cabinet, db_path):
    """Те же две цифры, разошедшиеся сильно, значат ровно обратное."""
    quiet = copy.deepcopy(FULLSTATS)
    # С рекламы пришло 200 рублей из 4200 выручки, это меньше пяти процентов.
    quiet[0]["days"][0]["sum_price"] = 200
    quiet[0]["days"][0]["apps"][0]["nms"][0]["sum_price"] = 200
    quiet[0]["days"][0]["apps"][1]["nms"][0]["sum_price"] = 0
    quiet[1]["days"][0]["sum_price"] = 0
    quiet[1]["days"][0]["apps"][0]["nms"][0]["sum_price"] = 0

    await collect_into(cabinet, db_path, Recorder(stats=quiet))
    report = report_of(cabinet, db_path)

    assert report.verdict == "self_selling"
    assert "продают сами" in handlers_ads.summary_text(report)


@pytest.mark.asyncio
async def test_without_the_finance_module_the_second_number_is_not_invented(
    db_path, monkeypatch
):
    """Реклама куплена, финансы нет: честный ответ вместо выдуманного числа.

    Выручку товара брать неоткуда: её собирает модуль «Финансы». Показать
    вместо неё выручку от рекламы значило бы нарисовать вторую цифру, равную
    первой, и селлер прочитал бы её как факт.
    """
    monkeypatch.setenv("ENCRYPTION_KEY", crypto.generate_key())
    wbapi.reset_limits()
    client_id = db.admin_repo(db_path).ensure_client(9090)
    db.repo(client_id, db_path).insert(
        "wb_tokens", ciphertext=crypto.encrypt(make_token()), exp=str(EXP), scopes="promotion"
    )
    await collect_into(client_id, db_path, Recorder())
    report = report_of(client_id, db_path)

    assert report.revenue_known is False
    assert report.revenue is None
    assert report.drr_cabinet is None
    assert campaign(report, 777).drr_cabinet is None
    # Первая цифра при этом на месте и посчитана полностью.
    assert report.drr_ads == Decimal("36.67")

    text = handlers_ads.summary_text(report)
    assert "посчитать не из чего" in text
    assert "Финансы" in text


# --- статистика против счёта ---


@pytest.mark.asyncio
async def test_the_gap_between_statistics_and_the_invoice_is_shown_not_hidden(
    cabinet, db_path
):
    """Статистический расход 1100, списали 1250: разница 150 рублей.

    Прятать её нельзя: селлер сверяет отчёт с разделом «Финансы» рекламного
    кабинета, а там стоит вторая цифра. То же правило, что в сверке
    финансового отчёта: расхождение больше рубля показывается.
    """
    await collect_into(cabinet, db_path, Recorder())
    report = report_of(cabinet, db_path)

    assert report.fact_known is True
    assert report.spend == Decimal("1100")
    assert report.fact_spend == Decimal("1250")
    assert report.gap == Decimal("150")
    assert report.gap_matters is True
    # По кампании обе цифры тоже стоят рядом: 600 + 400 против 700 + 450.
    assert campaign(report, 777).fact_spend == Decimal("1150")

    text = handlers_ads.summary_text(report)
    assert "150" in text
    assert "бонусами" in text


@pytest.mark.asyncio
async def test_a_kopeck_of_difference_is_not_worth_a_warning(cabinet, db_path):
    """Рубль это порог, а не ноль: округление не повод пугать селлера."""
    same = [dict(item) for item in UPD]
    same[0]["updSum"] = 700
    same[1]["updSum"] = 300
    same[2]["updSum"] = 100  # ровно 1100, как в статистике

    await collect_into(cabinet, db_path, Recorder(upd=same))
    report = report_of(cabinet, db_path)

    assert report.gap == Decimal("0")
    assert report.gap_matters is False
    assert "Статистика и счёт разошлись" not in handlers_ads.summary_text(report)


@pytest.mark.asyncio
async def test_without_the_history_of_charges_there_is_nothing_to_check_against(
    cabinet, db_path
):
    """История затрат не ответила: остаётся статистика, и это не ноль."""
    await collect_into(cabinet, db_path, Recorder(codes={UPD_PATH: 500}))
    report = report_of(cabinet, db_path)

    assert report.fact_known is False
    assert report.fact_spend is None
    assert report.gap is None
    assert report.gap_matters is False
    # Сама статистика при этом сохранена: терять весь сбор из-за
    # необязательной части нельзя.
    assert report.spend == Decimal("1100")


@pytest.mark.asyncio
async def test_a_charge_without_a_time_is_not_thrown_away(cabinet, db_path):
    """Списание без времени всё равно внутри окна, которое мы сами и задали.

    Выбросить его значило бы занизить фактический расход и нарисовать
    расхождение, которого нет.
    """
    nameless = [dict(item) for item in UPD]
    nameless[1]["updTime"] = None

    await collect_into(cabinet, db_path, Recorder(upd=nameless))
    report = report_of(cabinet, db_path)

    assert report.fact_spend == Decimal("1250")


async def collect_window(client_id, db_path, recorder, last, *, days=7):
    """Один заход сбора окном, которое кончается днём `last`."""
    clock = FakeTime()
    return await ads.collect(
        client_id,
        last - timedelta(days=days - 1),
        last,
        path=db_path,
        http=recorder.client(),
        clock=clock.clock,
        sleep=clock.sleep,
    )


def charges_of(client_id, db_path):
    """Состояние таблицы списаний: сколько строк и на какую сумму."""
    rows = db.repo(client_id, db_path).rows("ad_upd")
    return len(rows), sum(int(row["sum_kop"]) for row in rows)


@pytest.mark.asyncio
async def test_a_charge_without_a_time_is_counted_once_however_many_collections(
    cabinet, db_path
):
    """Три захода подряд разными окнами: сумма списаний не выросла.

    Дату списанию без времени придумывает бот, а не Wildberries: это
    последний день запрошенного окна. Окно суточного сбора скользящее, сегодня
    оно кончается сегодня, завтра завтра, поэтому одно и то же списание
    приезжало бы каждый день под новой датой и ложилось бы рядом с прежним: за
    неделю до семи раз. Проверяется именно повторение, одним заходом такую
    поломку не увидеть.
    """
    nameless = [dict(item) for item in UPD]
    nameless[1]["updTime"] = None      # 450 рублей без времени

    seen = []
    for shift in range(3):
        # 3, 4 и 5 сентября: оба датированных списания (1 и 2 сентября)
        # остаются внутри окна, а безвременное каждый раз получает новую дату.
        await collect_window(
            cabinet, db_path, Recorder(upd=nameless), date(2026, 9, 3 + shift)
        )
        seen.append(charges_of(cabinet, db_path))

    # 700 + 450 + 100 это 1250 рублей, и после каждого захода их всё столько же.
    assert seen == [(3, 125000)] * 3


@pytest.mark.asyncio
async def test_two_charges_of_one_day_without_a_number_are_both_kept(cabinet, db_path):
    """Номер документа у Wildberries тоже бывает пустым.

    Два списания по одной кампании за одни сутки без номера ничем не
    различаются, и раньше оба получали номер ноль, то есть схлопывались в одну
    строку: факт занижался. Ошибка обратная к задвоению и из того же места.
    """
    twins = [
        {
            "updTime": "2026-09-01T10:00:00+03:00",
            "updSum": 300,
            "advertId": 777,
            "paymentType": "Счёт",
        },
        {
            "updTime": "2026-09-01T18:00:00+03:00",
            "updSum": 200,
            "advertId": 777,
            "paymentType": "Бонусы",
        },
    ]

    await collect_into(cabinet, db_path, Recorder(upd=twins))
    assert charges_of(cabinet, db_path) == (2, 50000)

    # И повтор сбора их не удваивает: окно переписывается целиком.
    await collect_into(cabinet, db_path, Recorder(upd=twins))
    assert charges_of(cabinet, db_path) == (2, 50000)
    assert report_of(cabinet, db_path).fact_spend == Decimal("500")


@pytest.mark.asyncio
async def test_a_failed_request_for_charges_does_not_erase_the_window(cabinet, db_path):
    """Пустой ответ это «не знаем», а не «списаний не было».

    Окно списаний переписывается целиком, и это правило обязано кончаться там,
    где Wildberries не ответил: иначе одна пятисотка стёрла бы собранное.
    """
    await collect_into(cabinet, db_path, Recorder())
    assert charges_of(cabinet, db_path) == (3, 125000)

    await collect_into(cabinet, db_path, Recorder(codes={UPD_PATH: 500}))

    assert charges_of(cabinet, db_path) == (3, 125000)
    assert report_of(cabinet, db_path).fact_spend == Decimal("1250")


# --- отчёт читает только свой период ---


@pytest.mark.asyncio
async def test_the_report_reads_only_the_period_it_builds(cabinet, db_path, monkeypatch):
    """Отчёт за месяц не поднимает в память всю историю кабинета.

    `sqlite3` тут синхронный и живёт в одном процессе с ботом: долгое чтение
    это пауза у всех клиентов сразу, а не медленный отчёт у одного. Индексы по
    дате в схеме стоят, значит границы периода обязаны быть в запросе.
    """
    await collect_into(cabinet, db_path, Recorder())
    repo = db.repo(cabinet, db_path)

    # Прошлогодние строки: в период они не входят и в отчёт попасть не должны.
    old = "2025-09-01"
    repo.upsert("ad_daily", {"date": old, "advert_id": 777}, spend_kop=9_900_000)
    repo.upsert(
        "ad_nm_daily", {"date": old, "advert_id": 777, "nm_id": 111}, spend_kop=9_900_000
    )
    repo.upsert(
        "ad_upd", {"date": old, "advert_id": 777, "upd_num": 99}, sum_kop=9_900_000
    )

    whole = db.ClientRepo.rows

    def guard(self, table, *args, **kwargs):
        assert table not in ads.CLEANED_TABLES, f"таблица {table} прочитана целиком"
        return whole(self, table, *args, **kwargs)

    monkeypatch.setattr(db.ClientRepo, "rows", guard)

    report = report_of(cabinet, db_path)

    assert report.spend == Decimal("1100")
    assert report.fact_spend == Decimal("1250")
    assert {item.date for item in report.days} == {"2026-09-01", "2026-09-02"}


def test_the_old_days_are_cleaned_up_and_the_term_lives_in_the_config(
    cabinet, db_path, monkeypatch
):
    """Срок хранения суточной истории это настройка владельца, а не число в коде."""
    repo = db.repo(cabinet, db_path)
    fresh, ancient = TODAY.isoformat(), (TODAY - timedelta(days=500)).isoformat()
    for stamp in (fresh, ancient):
        repo.upsert("ad_daily", {"date": stamp, "advert_id": 777}, spend_kop=100)
        repo.upsert(
            "ad_nm_daily", {"date": stamp, "advert_id": 777, "nm_id": 111}, spend_kop=100
        )
        repo.upsert("ad_upd", {"date": stamp, "advert_id": 777, "upd_num": 1}, sum_kop=100)

    patched = copy.deepcopy(config.settings())
    patched["storage"]["daily_history_days"] = 30
    monkeypatch.setattr(config, "settings", lambda: patched)
    assert ads.history_days() == 30

    task = SimpleNamespace(client_id=None, payload={"date": TODAY.isoformat()})
    assert ads.cleanup(task, path=db_path) == 3

    for table in ads.CLEANED_TABLES:
        assert {str(row["date"]) for row in repo.rows(table)} == {fresh}, table
    # Справочник кампаний не чистится: в нём строка на кампанию, а не на сутки.
    assert repo.count("ad_campaigns") == 0


# --- ограничения Wildberries ---


@pytest.mark.asyncio
async def test_sixty_campaigns_and_a_long_period_fit_into_the_wb_limits(
    cabinet, db_path
):
    """Кабинет крупнее одного запроса: 60 кампаний и 45 дней.

    Wildberries берёт максимум 50 кампаний и 31 день за запрос. Нарезку
    делает `core.wbapi`, и проверяется здесь именно она: ни один запрос не
    должен выйти за оба предела, а все кампании и все дни обязаны быть
    спрошены ровно по одному разу.
    """
    many = [
        {
            "advertId": 1000 + number,
            "days": [
                {
                    "date": f"2026-09-{day:02d}T00:00:00+03:00",
                    "orders": 1,
                    "sum": 10,
                    "sum_price": 100,
                    "apps": [
                        {"appType": 1, "nms": [{"nmId": 111, "orders": 1, "sum": 10, "sum_price": 100}]}
                    ],
                }
                for day in (1, 2)
            ],
        }
        for number in range(60)
    ]
    recorder = Recorder(stats=many, upd=[])
    await collect_into(cabinet, db_path, recorder, days=45)

    queries = recorder.params(STATS_PATH)
    assert queries, "статистику вообще не спросили"
    asked: list[int] = []
    for query in queries:
        ids = [int(value) for value in query["ids"].split(",")]
        begin = date.fromisoformat(query["beginDate"])
        end = date.fromisoformat(query["endDate"])
        assert len(ids) <= wb_client.FULLSTATS_MAX_IDS, "больше 50 кампаний в одном запросе"
        assert (end - begin).days + 1 <= wb_client.FULLSTATS_MAX_DAYS, "окно длиннее 31 дня"
        asked += ids
    # 60 кампаний на два окна дат: каждая спрошена по разу в каждом окне.
    assert sorted(set(asked)) == sorted(item["advertId"] for item in many)
    assert len(asked) == len(set(asked)) * 2

    # История затрат режется по тем же 31 дню, и это другой предел.
    for query in recorder.params(UPD_PATH):
        begin = date.fromisoformat(query["from"])
        end = date.fromisoformat(query["to"])
        assert (end - begin).days + 1 <= wb_client.UPD_MAX_DAYS

    # Расход при этом сложился без потерь и без задвоения: 60 кампаний по
    # 10 рублей за два дня это 1200 рублей.
    assert report_of(cabinet, db_path).spend == Decimal("1200")


@pytest.mark.asyncio
async def test_campaign_names_are_asked_in_batches_of_fifty(cabinet, db_path):
    """Названия тоже режутся: у метода информации о кампаниях предел 50."""
    many = [{"advertId": 1000 + number, "days": []} for number in range(60)]
    recorder = Recorder(stats=many, upd=[], adverts=[])
    await collect_into(cabinet, db_path, recorder, days=10)

    for query in recorder.params(ADVERTS_PATH):
        ids = [value for value in query["ids"].split(",") if value]
        assert len(ids) <= wb_client.ADVERTS_MAX_IDS


# --- чего нет, о том говорится вслух ---


@pytest.mark.asyncio
async def test_without_the_promotion_category_the_client_gets_an_explanation(
    cabinet, db_path
):
    """403 это не сбой: сказать, какой категории нет, и не упасть.

    Модули на паузу при этом не ставятся: токен рабочий, просто без одной
    категории. Решает это `core.clients.on_wb_error`, и здесь важно, что
    наружу не летит ни исключение, ни пустой отчёт без объяснения.
    """
    collected = await collect_into(cabinet, db_path, Recorder(codes={COUNT_PATH: 403}))

    assert collected.reason == ads.NO_CATEGORY
    assert collected.ok is False

    report = report_of(cabinet, db_path, trouble=collected.reason)
    text = handlers_ads.summary_text(report)
    assert report.empty is True
    assert "Продвижение" in text
    assert "/connect" in text


@pytest.mark.asyncio
async def test_unavailable_wildberries_keeps_what_was_collected_before(cabinet, db_path):
    """WB не ответил: показываем собранное раньше и говорим об этом."""
    await collect_into(cabinet, db_path, Recorder())
    collected = await collect_into(cabinet, db_path, Recorder(codes={STATS_PATH: 503}))

    assert collected.reason == ads.UNAVAILABLE
    report = report_of(cabinet, db_path, trouble=collected.reason)
    assert report.spend == Decimal("1100")
    assert "не отдал статистику" in handlers_ads.summary_text(report)


@pytest.mark.asyncio
async def test_a_cabinet_without_campaigns_is_not_a_failure(cabinet, db_path):
    collected = await collect_into(cabinet, db_path, Recorder(stats=[], upd=[]))

    assert collected.reason == ads.OK
    report = report_of(cabinet, db_path)
    assert report.empty is True
    assert "не нашлось" in handlers_ads.summary_text(report)


@pytest.mark.asyncio
async def test_a_campaign_that_spends_without_orders_is_named_not_dashed(
    cabinet, db_path
):
    """Расход есть, заказов с рекламы нет: процента нет, и это худший случай.

    Прочерк на месте ДРР прочитался бы как «ноль», то есть как успех, а
    кампания при этом просто жжёт бюджет.
    """
    burning = copy.deepcopy(FULLSTATS)
    burning[0]["days"][0]["sum_price"] = 0
    burning[0]["days"][0]["orders"] = 0
    burning[0]["days"][0]["apps"][0]["nms"][0]["sum_price"] = 0
    burning[0]["days"][0]["apps"][1]["nms"][0]["sum_price"] = 0

    await collect_into(cabinet, db_path, Recorder(stats=burning))
    report = report_of(cabinet, db_path)
    item = campaign(report, 777)

    assert item.spend == Decimal("1000")
    assert item.drr_ads is None
    assert item.cpo is None
    # Такая кампания обязана попасть в список «выше цели»: расход есть.
    assert item in report.over_target
    assert "заказов с рекламы нет" in handlers_ads.summary_text(report)


# --- цель селлера ---


def test_the_target_drr_comes_from_the_settings_and_not_from_the_code(db_path, monkeypatch):
    """Цель это настройка, а не число в коде.

    Доказывается двумя способами сразу: умолчание меняется правкой конфига, а
    личная цель клиента перебивает умолчание.
    """
    client_id = db.admin_repo(db_path).ensure_client(4242)
    assert ads.target_drr(client_id, path=db_path) == Decimal(
        str(config.settings()["ads"]["target_drr"])
    )

    patched = copy.deepcopy(config.settings())
    patched["ads"]["target_drr"] = 8
    monkeypatch.setattr(config, "settings", lambda: patched)
    assert ads.target_drr(client_id, path=db_path) == Decimal("8")

    # Личная цель селлера сильнее умолчания и переживает его правку.
    assert ads.set_target_drr(client_id, 25, path=db_path) == Decimal("25")
    assert ads.target_drr(client_id, path=db_path) == Decimal("25")
    patched["ads"]["target_drr"] = 40
    assert ads.target_drr(client_id, path=db_path) == Decimal("25")


def test_the_target_lives_next_to_the_other_client_settings(db_path):
    """Своя цель не затирает чужие ключи в настройках клиента."""
    client_id = db.admin_repo(db_path).ensure_client(4343)
    data = clients.settings_of(client_id, path=db_path)
    data["token_reminders"] = [14]
    clients.save_settings(client_id, data, path=db_path)

    ads.set_target_drr(client_id, 20, path=db_path)

    assert clients.reminded(client_id, path=db_path) == (14,)
    assert ads.target_drr(client_id, path=db_path) == Decimal("20")


@pytest.mark.asyncio
async def test_the_report_compares_campaigns_with_the_target_of_this_seller(
    cabinet, db_path
):
    """Список «выше цели» зависит от цели клиента, а не от числа в коде."""
    await collect_into(cabinet, db_path, Recorder())

    # Цель по умолчанию 15 процентов: выше неё только кампания 777 с 50.
    assert [item.advert_id for item in report_of(cabinet, db_path).over_target] == [777]

    ads.set_target_drr(cabinet, 5, path=db_path)
    report = report_of(cabinet, db_path)
    assert report.target == Decimal("5")
    assert {item.advert_id for item in report.over_target} == {777, 888}
    assert "Ваша цель: 5%" in handlers_ads.summary_text(report)


class FakeMessage:
    def __init__(self):
        self.sent = []

    async def reply_text(self, text, **kwargs):
        self.sent.append((text, kwargs))
        return self


class FakeQuery:
    def __init__(self, data, message):
        self.data = data
        self.message = message
        self.answered = None

    async def answer(self, text=None, **kwargs):
        self.answered = text or ""


class FakeUpdate:
    def __init__(self, telegram_id, message, query=None):
        self.effective_user = SimpleNamespace(id=telegram_id)
        self.effective_message = message
        self.callback_query = query


@pytest.mark.asyncio
async def test_the_settings_button_saves_the_target_the_seller_picked(db_path):
    client_id = db.admin_repo(db_path).ensure_client(4444)
    message = FakeMessage()
    query = FakeQuery(f"{settings_handler.DRR_PREFIX}20", message)

    await settings_handler.toggle_callback(
        FakeUpdate(4444, message, query), None, path=db_path
    )

    assert ads.target_drr(client_id, path=db_path) == Decimal("20")


@pytest.mark.asyncio
async def test_a_forged_target_does_not_get_into_the_settings(db_path):
    """`callback_data` приходит от клиента и подделывается свободно.

    Цель в ноль обнулила бы сравнение, а в тысячу отключила бы его вовсе.
    Проверка живёт в функции, а не в клавиатуре.
    """
    client_id = db.admin_repo(db_path).ensure_client(4545)
    for data in ("0", "-5", "1000", "мало"):
        message = FakeMessage()
        await settings_handler.toggle_callback(
            FakeUpdate(4545, message, FakeQuery(f"{settings_handler.DRR_PREFIX}{data}", message)),
            None,
            path=db_path,
        )
    assert ads.target_drr(client_id, path=db_path) == ads.default_target_drr()


# --- команда бота ---


@pytest.mark.asyncio
async def test_ads_offers_periods_and_only_queues_the_work(cabinet, db_path):
    """В WB из хендлера никто не ходит: команда ставит задачу и отвечает."""
    queue.reset()
    telegram_id = int(db.admin_repo(db_path).client(cabinet)["telegram_id"])

    message = FakeMessage()
    await handlers_ads.ads_command(FakeUpdate(telegram_id, message), None, path=db_path)
    buttons = message.sent[0][1]["reply_markup"].inline_keyboard
    assert [row[0].callback_data for row in buttons] == ["ads:week", "ads:month", "ads:quarter"]

    query = FakeQuery("ads:month", FakeMessage())
    await handlers_ads.period_chosen(
        FakeUpdate(telegram_id, query.message, query), None, path=db_path
    )
    assert query.answered == "Принято"

    tasks = db.admin_repo(db_path).tasks_by_kind(ads.TASK_KIND)
    assert len(tasks) == 1
    assert json.loads(tasks[0]["payload"])["period"] == "month"


@pytest.mark.asyncio
async def test_a_made_up_period_puts_nothing_in_the_queue(cabinet, db_path):
    queue.reset()
    telegram_id = int(db.admin_repo(db_path).client(cabinet)["telegram_id"])

    for data in ("ads:year", "ads:", "ads:всё"):
        query = FakeQuery(data, FakeMessage())
        await handlers_ads.period_chosen(
            FakeUpdate(telegram_id, query.message, query), None, path=db_path
        )
    assert db.admin_repo(db_path).tasks_by_kind(ads.TASK_KIND) == []


@pytest.mark.asyncio
async def test_the_command_without_the_module_offers_to_buy_it(db_path):
    """Нет доступа - предложение вместо отказа, и кнопка «Оформить»."""
    queue.reset()
    client_id = db.admin_repo(db_path).ensure_client(5151)
    guard = tariffs.require_module(ads.MODULE, path=db_path)

    message = FakeMessage()
    command = guard(functools.partial(handlers_ads.ads_command, path=db_path))
    await command(FakeUpdate(5151, message), None)

    text, kwargs = message.sent[-1]
    assert "Реклама" in text and "1" in text
    assert kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "buy:ads"
    assert access.has_access(client_id, ads.MODULE, path=db_path) is False


@pytest.mark.asyncio
async def test_a_forged_callback_does_not_run_the_paid_report_either(db_path):
    """Нарисованная кнопка прав не даёт, и выдуманная тоже.

    Клиент этой клавиатуры не видел: `callback_data` он сочинил сам. Проверка
    стоит в самой команде, а не в том, нарисовали мы кнопку или нет.
    """
    queue.reset()
    db.admin_repo(db_path).ensure_client(5252)
    guard = tariffs.require_module(ads.MODULE, path=db_path)

    message = FakeMessage()
    query = FakeQuery("ads:quarter", message)
    pressed = guard(functools.partial(handlers_ads.period_chosen, path=db_path))
    await pressed(FakeUpdate(5252, message, query), None)

    text, kwargs = message.sent[-1]
    assert "Реклама" in text
    assert kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "buy:ads"
    assert db.admin_repo(db_path).tasks_by_kind(ads.TASK_KIND) == [], (
        "платный отчёт встал в очередь по выдуманной кнопке"
    )


# --- чужой текст в сообщении ---

# Ловушка короткая нарочно: длинное название в строке топа режется по
# ширине экрана, и проверка «текст не потерян» проверяла бы срез, а не
# экранирование.
TRAP = '<a href="u">нажми</a>'


@pytest.mark.asyncio
async def test_a_campaign_name_from_the_cabinet_does_not_become_markup(cabinet, db_path):
    """Название кампании придумывает селлер, и это чужой текст.

    Сводка уходит с ParseMode.HTML: осмысленная угловая скобка стала бы
    ссылкой от имени бота, случайная ошибкой Telegram, и тогда отчёта клиент
    не увидит вообще.
    """
    named = [dict(item) for item in ADVERTS]
    named[0] = {**named[0], "settings": {"name": TRAP}}
    await collect_into(cabinet, db_path, Recorder(adverts=named))

    text = handlers_ads.summary_text(report_of(cabinet, db_path))

    assert "<a href" not in text
    assert "&lt;a href=&quot;" in text
    assert "нажми" in text  # текст не потерян, он просто не ссылка
    assert "<b>" in text  # а разметка самого бота на месте


# --- книга Excel ---


@pytest.mark.asyncio
async def test_excel_holds_campaigns_days_articles_and_the_method(cabinet, db_path):
    await collect_into(cabinet, db_path, Recorder())
    report = report_of(cabinet, db_path)
    book = xlsx.read_book(ads.excel_bytes(report))

    assert set(book.titles) == {
        ads.CAMPAIGNS_SHEET,
        ads.DAYS_SHEET,
        ads.ARTICLES_SHEET,
        ads.METHOD_SHEET,
    }

    rows = book[ads.CAMPAIGNS_SHEET].rows
    assert [row.get("Номер") for row in rows] == [777, 888]
    first = rows[0]
    assert first.get("Кампания") == "Кружки осень"
    assert first.get("Расход, ₽") == 1000
    assert first.get("Фактически списано, ₽") == 1150
    assert first.get("Цена заказа, ₽") == 250
    assert first.get("ДРР рекламы, %") == 50
    assert first.get("ДРР кабинета, %") == 31.25
    assert first.get("Цель, %") == 15

    # По дням: два дня, и во второй заказов не было.
    days = book[ads.DAYS_SHEET].rows
    assert [row.get("Дата") for row in days] == ["2026-09-01", "2026-09-02"]
    assert days[1].get("Цена заказа, ₽") == ads.NO_DATA

    # По артикулам: расход по товару сложен по всем площадкам, 400 + 200 + 400.
    articles = {row.get("Артикул WB"): row for row in book[ads.ARTICLES_SHEET].rows}
    assert articles[111].get("Расход, ₽") == 1000
    assert articles[111].get("Выручка всего, ₽") == 3200

    # Методология называет обе цифры и разницу между ними словами.
    method = "\n".join(
        " ".join(str(value) for value in row.values) for row in book[ads.METHOD_SHEET].rows
    )
    assert "sum / sum_price" in method
    assert "наш расчёт" in method
    assert "живёт на рекламе" in method


# --- сбор по расписанию и уход клиента ---


def test_the_daily_collect_goes_to_every_connected_cabinet(db_path, monkeypatch):
    """Сбор идёт независимо от подписки: удалённая кампания уносит историю.

    Подписку клиент оформит и через месяц, а не собранный расход не вернуть.
    """
    monkeypatch.setenv("ENCRYPTION_KEY", crypto.generate_key())
    queue.reset()
    admin = db.admin_repo(db_path)
    paid = admin.ensure_client(6161)
    unpaid = admin.ensure_client(6262)
    without_cabinet = admin.ensure_client(6363)
    for client_id in (paid, unpaid):
        db.repo(client_id, db_path).insert(
            "wb_tokens", ciphertext=crypto.encrypt(make_token()), exp=str(EXP)
        )
    access.grant_access(paid, "ads", 30, "WBR-2026-0700", "invoice", "owner", path=db_path)

    task = SimpleNamespace(client_id=None, payload={"date": TODAY.isoformat()})
    ads.fan_out_collect(task, path=db_path)

    queued = {
        int(row["client_id"])
        for row in admin.tasks_by_kind(ads.COLLECT_ONE)
    }
    assert queued == {paid, unpaid}
    assert without_cabinet not in queued


@pytest.mark.asyncio
async def test_disconnect_takes_the_ad_tables_with_it(cabinet, db_path):
    """Каскадное удаление: после /disconnect рекламы клиента не остаётся."""
    await collect_into(cabinet, db_path, Recorder())
    repo = db.repo(cabinet, db_path)
    for table in ("ad_campaigns", "ad_daily", "ad_nm_daily", "ad_upd"):
        assert repo.count(table) > 0, table

    removed = clients.disconnect(cabinet, path=db_path)

    for table in ("ad_campaigns", "ad_daily", "ad_nm_daily", "ad_upd"):
        assert table in removed, table
        assert repo.count(table) == 0, table
