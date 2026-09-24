"""Клиент WB API: разбор токена, лимиты, ошибки, проверка связи.

Шов один, транспорт: httpx.MockTransport отдаёт записанные ответы,
сети в тестах нет ни одного байта.
"""

from __future__ import annotations

import base64
import json
from datetime import date, datetime, timezone

import pytest

from core import wbapi

# Маска посчитана руками, а не кодом под тестом:
# Контент 1, Аналитика 2, Статистика 5, Продвижение 6, Финансы 13.
# 2 + 4 + 32 + 64 + 8192 = 8294.
MASK_FIVE = 8294
READ_ONLY = 1 << 30  # 1073741824
EXP = 1789000000
SID = "3d0f5b41-2c8a-4f4e-9a6b-1f2e3d4c5b6a"


def make_token(**payload) -> str:
    """Собирает JWT с нужным payload. Подпись не проверяется, она не нужна."""
    def part(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    body = {"id": "ab" * 8, "sid": SID, "exp": EXP, "s": MASK_FIVE, "acc": 3}
    body.update(payload)
    return f"{part({'alg': 'HS256', 'typ': 'JWT'})}.{part(body)}.c2lnbmF0dXJl"


def test_verify_token_reads_payload_without_network():
    info = wbapi.verify_token(make_token())

    assert info.sid == SID
    assert info.exp == EXP
    assert info.expires_at == datetime.fromtimestamp(EXP, tz=timezone.utc)
    assert info.acc == 3
    assert info.read_only is False


def test_category_bits_match_documentation():
    """Биты ровно как в документации, каждый проверяется отдельно."""
    cases = {1: "content", 2: "analytics", 5: "statistics", 6: "promotion", 13: "finance"}
    for bit, name in cases.items():
        info = wbapi.verify_token(make_token(s=1 << bit))
        assert info.categories == (name,), f"бит {bit} должен давать {name}"
        assert info.has(name) is True


def test_five_categories_from_one_mask():
    info = wbapi.verify_token(make_token(s=MASK_FIVE))
    assert set(info.categories) == {"content", "analytics", "statistics", "promotion", "finance"}


def test_read_only_is_bit_30():
    info = wbapi.verify_token(make_token(s=MASK_FIVE | READ_ONLY))
    assert info.read_only is True
    # бит 30 не должен превратиться в категорию
    assert set(info.categories) == {"content", "analytics", "statistics", "promotion", "finance"}


def test_broken_token_is_rejected_and_never_echoed():
    raw = "не-джей-дабл-ю-ти-совсем"
    with pytest.raises(wbapi.WBTokenFormatError) as exc:
        wbapi.verify_token(raw)
    assert raw not in str(exc.value)


def test_expired_token_is_visible_without_network():
    info = wbapi.verify_token(make_token(exp=1600000000))
    assert info.is_expired(now=datetime(2026, 9, 15, tzinfo=timezone.utc)) is True
    assert info.days_left(now=datetime(2026, 9, 15, tzinfo=timezone.utc)) < 0


# --- шов «транспорт»: подставной httpx.AsyncClient с записанными ответами ---

import httpx  # noqa: E402

from core import crypto, db  # noqa: E402

TOKEN = make_token()


class FakeTime:
    """Часы и пауза под контролем теста: ожидания видны, а время не идёт."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(round(float(seconds), 3))
        self.now += float(seconds)


def make_http(responses, record=None):
    """AsyncClient с записанными ответами. Список коротится, последний повторяется."""
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(request)
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, type) and issubclass(item, Exception):
            raise item("нет ответа", request=request)
        return item

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def ok(payload=None, **headers) -> httpx.Response:
    return httpx.Response(200, json=payload if payload is not None else {}, headers=headers)


@pytest.fixture
def cabinet(tmp_path, monkeypatch):
    """Подключённый кабинет: клиент в базе и зашифрованный токен рядом."""
    monkeypatch.setenv("ENCRYPTION_KEY", crypto.generate_key())
    path = tmp_path / "wb.db"
    db.migrate(path)
    client_id = db.admin_repo(path).ensure_client(555)
    db.repo(client_id, path).insert(
        "wb_tokens", ciphertext=crypto.encrypt(TOKEN), exp=str(EXP), scopes="finance"
    )
    yield client_id, path
    db.close_all()


def build(cabinet, responses, record=None, time=None):
    """Клиент на подставном транспорте.

    Часы и сон обязательно одного объекта: с разными ограничитель частоты
    оказался бы выключен, потому что время не двигалось бы после паузы.
    """
    client_id, path = cabinet
    time = time or FakeTime()
    return wbapi.get_wb_client(
        client_id,
        http=make_http(responses, record),
        path=path,
        clock=time.clock,
        sleep=time.sleep,
    )


@pytest.mark.asyncio
async def test_authorization_header_goes_without_bearer(cabinet):
    seen: list[httpx.Request] = []
    client = build(cabinet, [ok({"name": "ИП"})], seen)

    await client.seller_info()

    sent = seen[0].headers["Authorization"]
    assert sent == TOKEN
    assert not sent.lower().startswith("bearer")


@pytest.mark.asyncio
async def test_401_and_403_are_different_classes(cabinet):
    client = build(cabinet, [httpx.Response(401, json={"title": "Unauthorized"})])
    with pytest.raises(wbapi.WBAuthError):
        await client.seller_info()

    client = build(cabinet, [httpx.Response(403, json={"title": "Forbidden"})])
    with pytest.raises(wbapi.WBForbiddenError) as exc:
        await client.seller_info()
    assert not isinstance(exc.value, wbapi.WBAuthError)


@pytest.mark.asyncio
async def test_403_names_the_missing_category(cabinet):
    client = build(cabinet, [httpx.Response(403, json={"detail": "no access"})])
    with pytest.raises(wbapi.WBForbiddenError) as exc:
        await client.promotion_count()
    assert exc.value.category == "promotion"
    assert "Продвижение" in str(exc.value)


@pytest.mark.asyncio
async def test_429_waits_exactly_what_the_header_says(cabinet):
    time = FakeTime()
    seen: list[httpx.Request] = []
    client = build(
        cabinet,
        [httpx.Response(429, headers={"X-Ratelimit-Retry": "7"}, json={}), ok({"name": "ИП"})],
        seen,
        time,
    )

    await client.seller_info()

    assert 7.0 in time.slept
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_429_without_header_pauses_all_longer(cabinet):
    time = FakeTime()
    client = build(
        cabinet,
        [httpx.Response(429, json={}), httpx.Response(429, json={}), ok({"name": "ИП"})],
        None,
        time,
    )

    await client.seller_info()

    waits = [value for value in time.slept if value > 0]
    assert len(waits) >= 2
    assert waits[1] > waits[0], "пауза должна расти, а не повторяться"


@pytest.mark.asyncio
async def test_429_that_never_ends_becomes_rate_limited(cabinet):
    client = build(cabinet, [httpx.Response(429, headers={"X-Ratelimit-Retry": "3"}, json={})])
    with pytest.raises(wbapi.WBRateLimited) as exc:
        await client.seller_info()
    assert exc.value.retry_after == 3.0


@pytest.mark.asyncio
async def test_500_does_not_retry_here(cabinet):
    """Повтор после 5xx делает очередь, и второго слоя поверх неё быть не должно.

    Иначе попытки перемножаются: одна пятисотка превращается в дюжину
    обращений к WB, а требование ТЗ прямо обратное.
    """
    seen: list[httpx.Request] = []
    client = build(cabinet, [httpx.Response(500, text="oops")], seen)

    with pytest.raises(wbapi.WBUnavailable):
        await client.seller_info()
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_retries_come_from_the_queue_section_of_config(cabinet):
    """Число ожиданий после 429 то же, что у очереди: источник один."""
    from core import config

    attempts = int(config.settings()["queue"]["attempts"])
    seen: list[httpx.Request] = []
    client = build(cabinet, [httpx.Response(429, headers={"X-Ratelimit-Retry": "5"}, json={})], seen)

    with pytest.raises(wbapi.WBRateLimited):
        await client.seller_info()
    assert len(seen) == attempts


@pytest.mark.asyncio
async def test_timeout_is_unavailable_too(cabinet):
    client = build(cabinet, [httpx.ReadTimeout])
    with pytest.raises(wbapi.WBUnavailable):
        await client.seller_info()


@pytest.mark.asyncio
async def test_remaining_zero_makes_the_next_call_wait(cabinet):
    time = FakeTime()
    client = build(cabinet, [ok({"name": "ИП"}, **{"X-Ratelimit-Remaining": "0"})], None, time)

    await client.seller_info()
    assert time.slept == []
    await client.seller_info()
    assert time.slept and time.slept[0] > 0, "после Remaining: 0 вслепую не ходим"


@pytest.mark.asyncio
async def test_every_call_is_written_to_api_calls(cabinet):
    client_id, path = cabinet
    client = build(cabinet, [ok({"name": "ИП"})])

    await client.seller_info()

    rows = db.repo(client_id, path).rows("api_calls")
    assert len(rows) == 1
    assert rows[0]["host"] == "common-api.wildberries.ru"
    assert "seller-info" in rows[0]["method"]
    assert rows[0]["status"] == 200
    assert rows[0]["duration_ms"] is not None


@pytest.mark.asyncio
async def test_failed_call_is_written_too(cabinet):
    client_id, path = cabinet
    client = build(cabinet, [httpx.Response(401, json={})])

    with pytest.raises(wbapi.WBAuthError):
        await client.seller_info()

    rows = db.repo(client_id, path).rows("api_calls")
    assert [row["status"] for row in rows] == [401]


@pytest.mark.asyncio
async def test_token_never_leaves_the_client(cabinet):
    client = build(cabinet, [ok({"name": "ИП"})])

    assert TOKEN not in repr(client)
    public = {name: getattr(client, name) for name in dir(client) if not name.startswith("_")}
    assert TOKEN not in str(public.values())

    with pytest.raises(wbapi.WBAuthError) as exc:
        client = build(cabinet, [httpx.Response(401, json={"detail": TOKEN})])
        await client.seller_info()
    assert TOKEN not in str(exc.value), "токен не должен вернуться даже из тела ошибки"


def test_client_for_cabinet_without_token(tmp_path, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", crypto.generate_key())
    path = tmp_path / "empty.db"
    db.migrate(path)
    client_id = db.admin_repo(path).ensure_client(999)
    with pytest.raises(wbapi.WBTokenMissing):
        wbapi.get_wb_client(client_id, path=path)
    db.close_all()


# --- пагинация, разбиение по ограничениям WB, медленная дорожка ---


@pytest.mark.asyncio
async def test_report_walks_pages_by_rrd_id(cabinet):
    seen: list[httpx.Request] = []
    client = build(
        cabinet,
        [
            ok({"data": [{"rrdId": 11, "forPay": "100"}, {"rrdId": 22, "forPay": "200"}]}),
            ok({"data": [{"rrdId": 33, "forPay": "300"}]}),
        ],
        seen,
    )

    rows = await client.sales_report_detailed("2026-09-01", "2026-09-07", limit=2)

    assert [row["rrdId"] for row in rows] == [11, 22, 33]
    assert json.loads(seen[0].content)["rrdId"] == 0
    assert json.loads(seen[1].content)["rrdId"] == 22, "курсор это rrdId последней строки"


@pytest.mark.asyncio
async def test_report_has_its_own_slow_lane(cabinet):
    time = FakeTime()
    client = build(cabinet, [ok({"data": []}), ok({"data": []})], None, time)

    await client.sales_report_detailed("2026-09-01", "2026-09-07", limit=100)
    await client.sales_reports_list("2026-09-01", "2026-09-07")

    assert time.slept, "второй запрос отчёта не может уйти сразу: лимит 1 в минуту"
    assert max(time.slept) >= 59.0


@pytest.mark.asyncio
async def test_cards_walk_by_cursor(cabinet):
    seen: list[httpx.Request] = []
    client = build(
        cabinet,
        [
            ok(
                {
                    "cards": [{"nmID": 1}, {"nmID": 2}],
                    "cursor": {"updatedAt": "2026-09-01T10:00:00Z", "nmID": 2, "total": 2},
                }
            ),
            ok({"cards": [{"nmID": 3}], "cursor": {"updatedAt": "", "nmID": 3, "total": 1}}),
        ],
        seen,
    )

    cards = await client.cards_list(limit=2)

    assert [card["nmID"] for card in cards] == [1, 2, 3]
    # Первая страница просит только limit: пустой updatedAt Wildberries
    # разбирает как дату и отвечает 400, то есть каталог не приходит вовсе.
    first = json.loads(seen[0].content)["settings"]["cursor"]
    assert first == {"limit": 2}
    assert "updatedAt" not in first and "nmID" not in first
    # Вторая несёт курсор из ответа на первую, а не выдуманные значения.
    second = json.loads(seen[1].content)["settings"]["cursor"]
    assert second["updatedAt"] == "2026-09-01T10:00:00Z"
    assert second["nmID"] == 2


@pytest.mark.asyncio
async def test_cards_stop_when_the_answer_has_no_cursor(cabinet):
    """Полная страница без курсора это не повод просить её же ещё раз."""
    seen: list[httpx.Request] = []
    client = build(
        cabinet,
        [ok({"cards": [{"nmID": 1}, {"nmID": 2}], "cursor": {"total": 2}})]
        + [ok({"cards": [], "cursor": {"total": 0}})] * 3,
        seen,
    )

    cards = await client.cards_list(limit=2)

    assert [card["nmID"] for card in cards] == [1, 2]
    assert len(seen) == 1, "без курсора второй запрос повторил бы первую страницу"


@pytest.mark.asyncio
async def test_fullstats_respects_50_campaigns_and_31_days(cabinet):
    seen: list[httpx.Request] = []
    client = build(cabinet, [ok([])], seen)

    await client.fullstats(range(1, 121), "2026-06-01", "2026-07-10")

    # 120 кампаний это 3 куска по 50, 40 дней это 2 окна по 31 и меньше.
    assert len(seen) == 6
    for request in seen:
        ids = request.url.params["ids"].split(",")
        assert len(ids) <= 50
        begin = date.fromisoformat(request.url.params["beginDate"])
        end = date.fromisoformat(request.url.params["endDate"])
        assert (end - begin).days < 31


@pytest.mark.asyncio
async def test_stocks_walk_by_offset(cabinet):
    seen: list[httpx.Request] = []
    client = build(
        cabinet,
        [
            ok({"data": {"items": [{"nmId": 1, "quantity": 5}, {"nmId": 2, "quantity": 6}]}}),
            ok({"data": {"items": [{"nmId": 3, "quantity": 7}]}}),
        ],
        seen,
    )

    rows = await client.stocks_wb_warehouses(limit=2)

    assert [row["nmId"] for row in rows] == [1, 2, 3]
    assert json.loads(seen[1].content)["offset"] == 2


@pytest.mark.asyncio
async def test_funnel_history_asks_by_day(cabinet):
    seen: list[httpx.Request] = []
    client = build(cabinet, [ok({"data": [{"nmID": 1, "history": [{"date": "2026-09-01"}]}]})], seen)

    rows = await client.sales_funnel_history("2026-09-01", "2026-09-07", nm_ids=[1])

    assert rows and rows[0]["nmID"] == 1
    body = json.loads(seen[0].content)
    assert body["aggregationLevel"] == "day"
    assert body["selectedPeriod"] == {"start": "2026-09-01", "end": "2026-09-07"}
    assert body["nmIds"] == [1]
    # Поля skipDeletedNm в схеме этого метода нет, и мы его не шлём.
    assert "skipDeletedNm" not in body


@pytest.mark.asyncio
async def test_funnel_history_without_articles_never_goes_to_wb(cabinet):
    """nmIds у метода обязателен: пустой список это ошибка в коде, а не запрос."""
    seen: list[httpx.Request] = []
    client = build(cabinet, [ok({"data": []})], seen)

    for empty in ([], None):
        with pytest.raises(ValueError):
            await client.sales_funnel_history("2026-09-01", "2026-09-07", empty)

    assert seen == []


@pytest.mark.asyncio
async def test_funnel_history_splits_45_articles_into_three_batches(cabinet):
    """WB берёт максимум 20 артикулов за запрос: 45 это 20 + 20 + 5."""
    seen: list[httpx.Request] = []
    client = build(cabinet, [ok({"data": []})], seen)

    await client.sales_funnel_history("2026-09-01", "2026-09-07", list(range(1, 46)))

    assert len(seen) == 3
    batches = [json.loads(request.content)["nmIds"] for request in seen]
    assert [len(batch) for batch in batches] == [20, 20, 5]
    # Ни один артикул не потерялся и ни один не спрошен дважды.
    assert sorted(nm_id for batch in batches for nm_id in batch) == list(range(1, 46))


@pytest.mark.asyncio
async def test_funnel_history_waits_between_batches(cabinet):
    """Лимит дорожки 3 запроса в минуту: пачки ждут, а не летят подряд."""
    time = FakeTime()
    client = build(cabinet, [ok({"data": []})], None, time=time)

    # 100 артикулов это 5 пачек: три уходят всплеском, две ждут по 20 секунд.
    await client.sales_funnel_history("2026-09-01", "2026-09-07", list(range(1, 101)))

    assert time.slept == [20.0, 20.0]


@pytest.mark.asyncio
async def test_adverts_info_cuts_by_50_campaigns(cabinet):
    """Названия кампаний: WB берёт максимум 50 номеров за запрос."""
    seen: list[httpx.Request] = []
    client = build(
        cabinet, [ok([{"id": 777, "settings": {"name": "Осень"}}])], seen
    )

    rows = await client.adverts_info(range(1, 121))

    assert len(seen) == 3  # 120 кампаний это 50 + 50 + 20
    for request in seen:
        assert request.method == "GET"
        assert len(request.url.params["ids"].split(",")) <= 50
    assert rows[0]["settings"]["name"] == "Осень"


@pytest.mark.asyncio
async def test_adverts_info_without_campaigns_does_not_ask_wb(cabinet):
    """Кабинет без кампаний это нормальное состояние, а не повод для запроса."""
    seen: list[httpx.Request] = []
    client = build(cabinet, [ok([])], seen)

    assert await client.adverts_info([]) == []
    assert seen == []


@pytest.mark.asyncio
async def test_advert_upd_cuts_the_period_by_31_days(cabinet):
    """История затрат: WB берёт максимум 31 день за запрос."""
    seen: list[httpx.Request] = []
    client = build(cabinet, [ok([{"updNum": 1, "updSum": 500, "campName": "Осень"}])], seen)

    rows = await client.advert_upd("2026-06-01", "2026-07-10")

    assert len(seen) == 2  # 40 дней это два окна: 31 и 9
    for request in seen:
        assert request.method == "GET"
        begin = date.fromisoformat(request.url.params["from"])
        end = date.fromisoformat(request.url.params["to"])
        assert (end - begin).days < 31
    assert rows[0]["campName"] == "Осень"


def test_new_advert_lanes_match_the_documented_limits():
    """Дорожки новых методов заведены по числам из разведки, а не на глаз."""
    from core.wbapi import limits

    assert limits.LANES["adv-adverts"] == wbapi.Limit(5, 1.0, 5)
    assert limits.LANES["adv-upd"] == wbapi.Limit(1, 1.0, 5)
    assert wbapi.ENDPOINTS["adverts_info"].lane == "adv-adverts"
    assert wbapi.ENDPOINTS["advert_upd"].lane == "adv-upd"


def test_every_endpoint_has_a_lane_of_its_own_in_the_table():
    """Дорожка без записи в LANES получает самый осторожный лимит молча.

    Один запрос в минуту это правильная страховка от опечатки, но узнать о ней
    было бы неоткуда: отчёт просто стал бы идти в двадцать раз дольше.
    """
    from core.wbapi import limits

    missing = sorted(
        name for name, spot in wbapi.ENDPOINTS.items() if spot.lane not in limits.LANES
    )
    assert not missing, "дорожки этих методов нет в LANES: " + ", ".join(missing)


def test_the_search_report_lane_matches_the_documented_limit():
    """Видимость это отдельный метод, и лимит у него свой: 3 в минуту."""
    from core.wbapi import limits

    assert limits.LANES["analytics-search"] == wbapi.Limit(3, 60.0, 3)
    assert wbapi.ENDPOINTS["search_report"].lane == "analytics-search"
    # Дорожка не общая с воронкой: иначе необязательный пятый этап тормозил бы
    # суточный сбор, который терять нельзя.
    assert wbapi.ENDPOINTS["sales_funnel_history"].lane != "analytics-search"


def test_disabled_wb_methods_are_absent_from_the_code():
    """Отключённые WB методы не должны встречаться в коде вообще."""
    from pathlib import Path

    gone = (
        "reportDetailByPeriod",
        "/api/v1/supplier/stocks",
        "/api/v2/nm-report/detail",
        "/adv/v2/fullstats",
    )
    root = Path(wbapi.__file__).resolve().parent.parent.parent
    watched = list((root / "core").rglob("*.py")) + list((root / "bot").rglob("*.py"))
    for path in watched:
        text = path.read_text(encoding="utf-8")
        for dead in gone:
            assert dead not in text, f"{path.name}: отключённый метод {dead}"


def test_only_reading_methods_have_wrappers():
    """Обёрток на запись нет: токен только на чтение."""
    expected = {
        "sales_report_detailed",
        "sales_reports_list",
        "promotion_count",
        "adverts_info",
        "fullstats",
        "advert_upd",
        "sales_funnel_products",
        "sales_funnel_history",
        "search_report",
        "stocks_wb_warehouses",
        "cards_list",
        "seller_info",
    }
    assert set(wbapi.ENDPOINTS) == expected
    for name, endpoint in wbapi.ENDPOINTS.items():
        assert endpoint.verb in {"GET", "POST"}, name
        assert endpoint.host.endswith(".wildberries.ru"), name

    # Глаголы записи: их нет ни у одной обёртки и появиться не должно.
    assert not {endpoint.verb for endpoint in wbapi.ENDPOINTS.values()} & {
        "PUT",
        "PATCH",
        "DELETE",
    }
    # Пути, которые меняют кабинет, в коде тоже не встречаются. Соседи по
    # разделу продвижения названы поимённо: рядом с рекламными обёртками
    # живут методы назначения минус-фраз и управления кампаниями.
    from pathlib import Path

    writing = (
        "/normquery/set-minus",
        "/adv/v0/rename",
        "/adv/v1/pause",
        "/adv/v1/start",
        "/adv/v1/stop",
        "/adv/v1/budget/deposit",
    )
    root = Path(wbapi.__file__).resolve().parent.parent.parent
    for path in list((root / "core").rglob("*.py")) + list((root / "agents").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for method in writing:
            assert method not in text, f"{path.name}: метод на запись {method}"


# --- проверка связи: probe_hosts и команда /diag ---


def http_by_host(codes: dict[str, object], record=None):
    """Ответ зависит от домена: так проверяются объяснения по каждому коду."""

    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(request)
        item = codes.get(request.url.host, 200)
        if isinstance(item, type) and issubclass(item, Exception):
            raise item("нет ответа", request=request)
        return httpx.Response(int(item), json={"TS": "2026-09-15", "Status": "OK"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_probe_visits_all_six_hosts_with_pauses(tmp_path):
    path = tmp_path / "diag.db"
    db.migrate(path)
    time = FakeTime()
    seen: list[httpx.Request] = []

    probes = await wbapi.probe_hosts(
        http=http_by_host({}, seen), path=path, sleep=time.sleep, clock=time.clock
    )

    assert {probe.host for probe in probes} == set(wbapi.HOSTS.values())
    assert len(seen) == 6
    assert all(request.url.path == "/ping" for request in seen)
    assert all(probe.ok for probe in probes)
    assert len(time.slept) >= 5, "между доменами нужна пауза: лимит 3 запроса за 30 секунд"
    db.close_all()


@pytest.mark.asyncio
async def test_probe_explains_every_answer(tmp_path):
    path = tmp_path / "diag.db"
    db.migrate(path)
    codes = {
        wbapi.HOSTS["finance"]: 401,
        wbapi.HOSTS["analytics"]: 403,
        wbapi.HOSTS["advert"]: 500,
        wbapi.HOSTS["content"]: httpx.ConnectTimeout,
    }
    probes = {
        probe.host: probe
        for probe in await wbapi.probe_hosts(
            http=http_by_host(codes), path=path, sleep=FakeTime().sleep
        )
    }

    assert probes[wbapi.HOSTS["common"]].status == 200
    assert "связь есть" in probes[wbapi.HOSTS["common"]].verdict.lower()
    assert "токен" in probes[wbapi.HOSTS["finance"]].verdict.lower()
    assert "категор" in probes[wbapi.HOSTS["analytics"]].verdict.lower()
    assert probes[wbapi.HOSTS["advert"]].status == 500
    timed_out = probes[wbapi.HOSTS["content"]]
    assert timed_out.status is None
    assert "адрес" in timed_out.verdict.lower(), "таймаут похож на блокировку по адресу"
    assert not timed_out.ok
    db.close_all()


@pytest.mark.asyncio
async def test_probe_is_written_to_api_calls(tmp_path):
    path = tmp_path / "diag.db"
    db.migrate(path)

    await wbapi.probe_hosts(http=http_by_host({}), path=path, sleep=FakeTime().sleep)

    rows = db.admin_repo(path).api_calls()
    assert len(rows) == 6
    assert {row["method"] for row in rows} == {"GET /ping"}
    db.close_all()


# --- команда /diag: только владельцу, только вручную ---


class FakeMessage:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def reply_text(self, text, **kwargs):
        self.sent.append(str(text))
        return self


class FakeUpdate:
    def __init__(self, user_id: int) -> None:
        from types import SimpleNamespace

        self.effective_user = SimpleNamespace(id=user_id)
        self.effective_chat = SimpleNamespace(id=user_id)
        self.message = FakeMessage()


class FakeApp:
    """Приложение, которое ловит попытку поставить задачу в расписание."""

    def __init__(self) -> None:
        self.handlers: list = []

    def add_handler(self, handler, *args, **kwargs):
        self.handlers.append(handler)

    @property
    def job_queue(self):
        raise AssertionError("проверка связи не ставится в расписание, только вручную")


@pytest.fixture
def owner(tmp_path, monkeypatch):
    """Владелец бота и временная база на месте боевой."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "500")
    monkeypatch.setenv("ENCRYPTION_KEY", crypto.generate_key())
    db.migrate()
    yield 500
    db.close_all()


def test_diag_registers_itself_without_touching_the_schedule(owner):
    from bot.handlers import diag as handler

    app = FakeApp()
    handler.register(app)

    assert len(app.handlers) == 1


def test_diag_report_explains_every_host_in_plain_russian(owner):
    """Отчёт называет домен, код и причину. Собирается без сети и без бота."""
    from bot.handlers import diag as handler

    report = handler.report_text(
        [
            wbapi.HostProbe("finance", wbapi.HOSTS["finance"], 200, True, wbapi.verdict_for(200)),
            wbapi.HostProbe("advert", wbapi.HOSTS["advert"], 401, False, wbapi.verdict_for(401)),
            wbapi.HostProbe("content", wbapi.HOSTS["content"], None, False, wbapi.verdict_for(None)),
        ]
    )

    assert wbapi.HOSTS["finance"] in report
    assert "401" in report and "токен" in report.lower()
    assert "адрес" in report.lower()


def test_diag_texts_are_russian_without_long_dashes(owner):
    from bot.handlers import diag as handler

    probes = [wbapi.HostProbe("common", wbapi.HOSTS["common"], 200, True, wbapi.verdict_for(200))]
    report = handler.report_text(probes)
    assert chr(0x2014) not in report and chr(0x2013) not in report
    assert "WB" in report or "Wildberries" in report


# --- подключение кабинета: разбор плюс один пробный запрос (история A5) ---


@pytest.mark.asyncio
async def test_check_token_parses_and_probes_once(tmp_path):
    path = tmp_path / "check.db"
    db.migrate(path)
    seen: list[httpx.Request] = []

    info = await wbapi.check_token(TOKEN, http=make_http([ok({"Status": "OK"})], seen), path=path)

    assert info.sid == SID
    assert len(seen) == 1, "проверка токена это ровно один запрос"
    assert seen[0].url.path == "/ping"
    assert seen[0].url.host == wbapi.HOSTS["finance"]
    db.close_all()


@pytest.mark.asyncio
async def test_check_token_tells_revoked_from_wrong_category(tmp_path):
    path = tmp_path / "check.db"
    db.migrate(path)

    with pytest.raises(wbapi.WBAuthError):
        await wbapi.check_token(TOKEN, http=make_http([httpx.Response(401, json={})]), path=path)

    # 403 означает, что токен живой, просто не той категории: это не отказ
    info = await wbapi.check_token(
        TOKEN, http=make_http([httpx.Response(403, json={})]), path=path
    )
    assert info.sid == SID
    db.close_all()


@pytest.mark.asyncio
async def test_check_token_rejects_garbage_without_any_request(tmp_path):
    seen: list[httpx.Request] = []
    with pytest.raises(wbapi.WBTokenFormatError):
        await wbapi.check_token("не токен", http=make_http([ok()], seen))
    assert seen == []


# --- соединение, прошлый период воронки, публичные имена ---


def test_client_reuses_one_connection(cabinet):
    client_id, path = cabinet

    first = wbapi.get_wb_client(client_id, path=path)
    second = wbapi.get_wb_client(client_id, path=path)

    assert first.session is second.session, "новый AsyncClient на каждый вызов это утечка"
    assert first is not second


@pytest.mark.asyncio
async def test_funnel_can_ask_for_the_past_period(cabinet):
    seen: list[httpx.Request] = []
    client = build(cabinet, [ok({"data": {"products": []}})], seen)

    await client.sales_funnel_products(
        "2026-09-08", "2026-09-14", past=("2026-09-01", "2026-09-07"), limit=100
    )

    body = json.loads(seen[0].content)
    assert body["pastPeriod"] == {"start": "2026-09-01", "end": "2026-09-07"}


@pytest.mark.asyncio
async def test_probe_reports_the_code_the_host_answered(tmp_path):
    """В отчёт попадает настоящий код, а не подставленная константа."""
    path = tmp_path / "diag.db"
    db.migrate(path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204, json=None)

    probes = await wbapi.probe_hosts(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        path=path,
        sleep=FakeTime().sleep,
    )

    assert {probe.status for probe in probes} == {204}
    assert all(probe.ok for probe in probes)
    db.close_all()


# --- обрезана выгрузка отчёта или нет: это должно быть фактом, а не догадкой ---


@pytest.mark.asyncio
async def test_full_last_page_with_spent_cursor_is_not_truncated(cabinet):
    """Ровно полная последняя страница это не обрезка.

    Арифметика по длинам тут ошибается: 4 строки при limit=2 выглядят как
    «упёрлись», хотя выгрузка полная. Ложная тревога на честной неделе стоит
    доверия ровно столько же, сколько пропущенная настоящая.
    """
    client = build(
        cabinet,
        [
            ok({"data": [{"rrdId": 1}, {"rrdId": 2}]}),
            ok({"data": [{"rrdId": 3}, {"rrdId": 4}]}),
            ok({"data": []}),
        ],
    )

    result = await client.sales_report_detailed_paged(
        "2026-09-01", "2026-09-07", limit=2, max_pages=3
    )

    assert len(result.rows) == 4
    assert result.truncated is False
    assert result.pages == 3


@pytest.mark.asyncio
async def test_page_ceiling_with_live_cursor_is_truncated(cabinet):
    """Страниц больше потолка: курсор живой, значит часть строк не прочитана."""
    client = build(
        cabinet,
        [
            ok({"data": [{"rrdId": 1}, {"rrdId": 2}]}),
            ok({"data": [{"rrdId": 3}, {"rrdId": 4}]}),
        ],
    )

    result = await client.sales_report_detailed_paged(
        "2026-09-01", "2026-09-07", limit=2, max_pages=2
    )

    assert len(result.rows) == 4
    assert result.truncated is True
    assert result.last_rrd_id == 4


@pytest.mark.asyncio
async def test_old_call_still_returns_plain_rows(cabinet):
    client = build(cabinet, [ok({"data": [{"rrdId": 1}]})])

    rows = await client.sales_report_detailed("2026-09-01", "2026-09-07", limit=2)

    assert rows == [{"rrdId": 1}]


# --- /diag молчит для чужого и не ждёт лимит внутри хендлера ---


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, str(text)))


def fake_app() -> FakeApp:
    app = FakeApp()
    app.bot = FakeBot()
    return app


@pytest.mark.asyncio
async def test_diag_says_nothing_at_all_to_a_stranger(owner):
    """Для постороннего команды не существует: иначе перебор найдёт админку."""
    from bot.handlers import diag as handler

    update = FakeUpdate(4242)
    await handler.diag(update, None)

    assert update.message.sent == []
    assert db.admin_repo().tasks() == []


@pytest.mark.asyncio
async def test_diag_answers_at_once_and_walks_hosts_later(owner, monkeypatch):
    """Обход с паузами уходит в очередь: бот не молчит минуту для всех."""
    from bot.handlers import diag as handler

    async def never(**kwargs):
        raise AssertionError("обход хостов не должен идти внутри хендлера")

    monkeypatch.setattr(handler.wbapi, "probe_hosts", never)
    update = FakeUpdate(owner)

    await handler.diag(update, None)

    assert update.message.sent == [handler.STARTED]
    queued = db.admin_repo().tasks()
    assert [row["kind"] for row in queued] == [handler.TASK_KIND]


@pytest.mark.asyncio
async def test_diag_task_sends_the_report_when_it_runs(owner, monkeypatch):
    from core import queue
    from bot.handlers import diag as handler

    async def fake_probe(**kwargs):
        return [wbapi.HostProbe("common", wbapi.HOSTS["common"], 200, True, wbapi.verdict_for(200))]

    monkeypatch.setattr(handler.wbapi, "probe_hosts", fake_probe)
    app = fake_app()
    run = handler.make_runner(app)

    await run(queue.Task(id=1, client_id=None, kind=handler.TASK_KIND,
                         payload={"chat_id": owner, "telegram_id": owner}, attempts=0))

    assert len(app.bot.sent) == 1
    chat_id, text = app.bot.sent[0]
    assert chat_id == owner
    assert wbapi.HOSTS["common"] in text


# --- проверка токена не морозит бота ---


@pytest.mark.asyncio
async def test_check_token_does_not_wait_for_the_limit(tmp_path):
    """Бюджет /ping исчерпан: проверка возвращается сразу, без запроса и паузы.

    Ждать тут нельзя: бот обрабатывает одно сообщение за раз, и десять секунд
    ожидания это десять секунд молчания для всех, включая владельца.
    """
    path = tmp_path / "check.db"
    db.migrate(path)
    time = FakeTime()
    budget = wbapi.Budget(time.clock, time.sleep)
    seen: list[httpx.Request] = []
    http = make_http([ok({"Status": "OK"})], seen)

    # всплеск у /ping это три запроса на домен, четвёртый уже ждал бы
    for _ in range(3):
        await wbapi.check_token(TOKEN, http=http, path=path, budget=budget)
    assert len(seen) == 3

    result = await wbapi.check_token_live(TOKEN, http=http, path=path, budget=budget)

    assert len(seen) == 3, "четвёртый запрос ушёл бы только после паузы"
    assert result.probed is False
    assert result.info.sid == SID
    assert time.slept == []
    db.close_all()


@pytest.mark.asyncio
async def test_check_token_probes_when_the_budget_is_free(tmp_path):
    path = tmp_path / "check.db"
    db.migrate(path)
    time = FakeTime()
    seen: list[httpx.Request] = []

    result = await wbapi.check_token_live(
        TOKEN,
        http=make_http([ok({"Status": "OK"})], seen),
        path=path,
        budget=wbapi.Budget(time.clock, time.sleep),
    )

    assert len(seen) == 1
    assert result.probed is True
    assert result.status == 200
    assert result.alive is True
    db.close_all()
