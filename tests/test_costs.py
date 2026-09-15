"""Себестоимость: шаблон, приём файла, хранение.

Швов два и оба чужие: путь к базе и транспорт WB. Сети тут нет ни байта,
файлы собираются и разбираются в памяти.
"""

from __future__ import annotations

import base64
import json
from decimal import Decimal

import httpx
import pytest

from core import costs, crypto, db, xlsx

EXP = 1789000000
# Маска токена с категорией «Контент»: бит 1, посчитано по документации.
MASK = 1 << 1


def make_token() -> str:
    def part(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    body = {"id": "ab" * 8, "sid": "sid-1", "exp": EXP, "s": MASK, "acc": 3}
    return f"{part({'alg': 'HS256', 'typ': 'JWT'})}.{part(body)}.c2ln"


@pytest.fixture
def cabinet(tmp_path, monkeypatch):
    """Подключённый кабинет: клиент в базе и зашифрованный токен рядом."""
    monkeypatch.setenv("ENCRYPTION_KEY", crypto.generate_key())
    path = tmp_path / "costs.db"
    db.migrate(path)
    client_id = db.admin_repo(path).ensure_client(777)
    db.repo(client_id, path).insert(
        "wb_tokens", ciphertext=crypto.encrypt(make_token()), exp=str(EXP)
    )
    yield client_id, path
    db.close_all()


def make_http(pages: list[dict], record=None) -> httpx.AsyncClient:
    """Транспорт с записанными страницами карточек."""
    queue = list(pages)

    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(json.loads(request.content.decode()))
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        return httpx.Response(200, json=item)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def card(nm_id: int, vendor: str, title: str) -> dict:
    return {"nmID": nm_id, "vendorCode": vendor, "title": title, "subjectName": "Носки"}


def page(cards: list[dict], total: int | None = None) -> dict:
    last = cards[-1] if cards else {"nmID": 0}
    return {
        "cards": cards,
        "cursor": {
            "total": len(cards) if total is None else total,
            "updatedAt": "2026-09-01T00:00:00Z",
            "nmID": last["nmID"],
        },
    }


def upload(rows, headers=None) -> bytes:
    """Книга, какую пришлёт селлер."""
    return xlsx.write_book(
        xlsx.Sheet(costs.TEMPLATE_SHEET, headers or list(costs.TEMPLATE_HEADERS), rows)
    )


# --- шаблон ---


@pytest.mark.asyncio
async def test_template_has_required_columns_and_all_pages_of_cards(cabinet):
    client_id, path = cabinet
    sent: list[dict] = []
    http = make_http(
        [
            page([card(1, "art-1", "Носки"), card(2, "art-2", "Кепка")], total=2),
            page([card(3, "art-3", "Шарф")], total=1),
        ],
        sent,
    )

    data = await costs.build_template(client_id, path=path, http=http, limit=2)
    sheet = xlsx.read_sheet(data)

    assert sheet.headers == costs.TEMPLATE_HEADERS
    assert [row.cells[costs.COL_NM] for row in sheet.rows] == [1, 2, 3]
    assert sheet.rows[0].cells[costs.COL_VENDOR] == "art-1"
    assert sheet.rows[0].cells[costs.COL_TITLE] == "Носки"
    # Колонка себестоимости пустая, её заполняет селлер.
    assert sheet.rows[0].cells[costs.COL_COST] in (None, "")
    # Вторая страница запрошена курсором с последнего артикула первой.
    assert len(sent) == 2
    assert sent[1]["settings"]["cursor"]["nmID"] == 2


@pytest.mark.asyncio
async def test_template_brings_back_costs_the_seller_already_gave(cabinet):
    client_id, path = cabinet
    costs.save_costs(client_id, {1: Decimal("199.50")}, path=path)
    http = make_http([page([card(1, "art-1", "Носки"), card(2, "art-2", "Кепка")])])

    data = await costs.build_template(client_id, path=path, http=http, limit=10)
    sheet = xlsx.read_sheet(data)

    assert Decimal(str(sheet.rows[0].cells[costs.COL_COST])) == Decimal("199.50")
    assert sheet.rows[1].cells[costs.COL_COST] in (None, "")


# --- приём файла ---


def test_good_file_is_saved_in_kopecks_and_read_back_as_decimal(cabinet):
    client_id, path = cabinet
    data = upload([[1, "art-1", "Носки", "199,50"], [2, "art-2", "Кепка", 1000]])

    result = costs.save_upload(client_id, data, path=path)

    assert result.saved == 2
    assert result.problems == ()
    assert costs.costs_for(client_id, path=path) == {
        1: Decimal("199.50"),
        2: Decimal("1000.00"),
    }
    # В базе целые копейки, ни одного float.
    row = db.repo(client_id, path).one("costs", nm_id=1)
    assert row["cost_per_unit_kop"] == 19950


def test_second_upload_updates_the_cost_instead_of_doubling_the_row(cabinet):
    client_id, path = cabinet
    costs.save_upload(client_id, upload([[1, "art-1", "Носки", 100]]), path=path)

    costs.save_upload(client_id, upload([[1, "art-1", "Носки", 250]]), path=path)

    assert db.repo(client_id, path).count("costs") == 1
    assert costs.costs_for(client_id, path=path)[1] == Decimal("250.00")


def test_file_without_the_needed_column_is_refused_with_a_reason(cabinet):
    client_id, path = cabinet
    data = upload(
        [[1, "art-1", "Носки"]], headers=["nmID", "Артикул продавца", "Название"]
    )

    with pytest.raises(costs.BadFile) as exc:
        costs.save_upload(client_id, data, path=path)

    assert "себестоимость" in str(exc.value).lower()
    assert db.repo(client_id, path).count("costs") == 0


def test_one_broken_row_does_not_cancel_the_whole_file(cabinet):
    client_id, path = cabinet
    rows = [
        [1, "art-1", "Носки", 100],
        [2, "art-2", "Кепка", "сто рублей"],
        [3, "art-3", "Шарф", -5],
        [4, "art-4", "Плед", ""],
        [5, "art-5", "Плед", 300],
    ]

    result = costs.save_upload(client_id, upload(rows), path=path)

    assert result.saved == 2
    assert sorted(costs.costs_for(client_id, path=path)) == [1, 5]
    numbers = [problem.row for problem in result.problems]
    # Заголовок это строка 1, значит «сто рублей» лежит в строке 3.
    assert numbers == [3, 4]
    assert "не число" in result.problems[0].reason
    assert "отрицательная" in result.problems[1].reason
    # Пустая себестоимость это не ошибка, селлер её просто не заполнил.
    assert result.blank == 1
    assert result.skipped == 3


def test_row_without_nm_id_is_reported_and_the_rest_survives(cabinet):
    client_id, path = cabinet
    data = upload([["", "art-1", "Носки", 100], [2, "art-2", "Кепка", 100]])

    result = costs.save_upload(client_id, data, path=path)

    assert result.saved == 1
    assert [problem.row for problem in result.problems] == [2]
    assert "nmid" in result.problems[0].reason.lower()


def test_alien_format_renamed_to_xlsx_is_refused_politely(cabinet):
    client_id, path = cabinet

    with pytest.raises(costs.BadFile) as exc:
        costs.save_upload(client_id, b"%PDF-1.4 not a workbook at all", path=path)

    assert "xlsx" in str(exc.value).lower()


def test_file_above_the_limit_is_refused_before_parsing(cabinet):
    client_id, path = cabinet
    data = upload([[1, "art-1", "Носки", 100]])

    with pytest.raises(costs.BadFile) as exc:
        costs.save_upload(client_id, data, max_bytes=16, path=path)

    assert "размер" in str(exc.value).lower() or "больше" in str(exc.value).lower()


def test_file_with_too_many_rows_is_refused_with_a_reason(cabinet):
    client_id, path = cabinet
    data = upload([[i, f"art-{i}", "Носки", 100] for i in range(1, 5)])

    with pytest.raises(costs.BadFile) as exc:
        costs.save_upload(client_id, data, max_rows=2, path=path)

    assert "строк" in str(exc.value).lower()
    assert db.repo(client_id, path).count("costs") == 0


def test_fraction_of_a_kopeck_is_a_problem_not_a_silent_rounding(cabinet):
    client_id, path = cabinet
    rows = [
        [1, "art-1", "Носки", "12,345"],
        [2, "art-2", "Кепка", "0,004"],
        [3, "art-3", "Шарф", "12,34"],
    ]

    result = costs.save_upload(client_id, upload(rows), path=path)

    assert result.saved == 1
    assert costs.costs_for(client_id, path=path) == {3: Decimal("12.34")}
    assert [problem.row for problem in result.problems] == [2, 3]
    assert "копе" in result.problems[0].reason.lower()


def test_a_huge_but_honest_cost_is_accepted(cabinet):
    """Порога сверху спецификация не решала, значит его нет."""
    client_id, path = cabinet

    result = costs.save_upload(
        client_id, upload([[1, "art-1", "Слиток", "999999999.99"]]), path=path
    )

    assert result.saved == 1
    assert costs.costs_for(client_id, path=path)[1] == Decimal("999999999.99")


def test_updated_at_is_stored_in_the_same_zone_as_the_rest_of_the_base(cabinet):
    """В базе UTC. Московское время это то, в чём показывают, а не хранят."""
    from datetime import datetime, timezone

    client_id, path = cabinet
    costs.save_costs(client_id, {1: Decimal("10")}, path=path)

    stored = db.repo(client_id, path).one("costs", nm_id=1)["updated_at"]
    seen = datetime.strptime(str(stored)[:19], "%Y-%m-%d %H:%M:%S")
    drift = abs((datetime.now(timezone.utc).replace(tzinfo=None) - seen).total_seconds())
    assert drift < 120, f"отметка {stored} не похожа на UTC"


def test_empty_file_is_refused_rather_than_silently_accepted(cabinet):
    client_id, path = cabinet

    with pytest.raises(costs.BadFile):
        costs.save_upload(client_id, upload([]), path=path)


def test_columns_are_found_even_if_the_seller_moved_or_renamed_them(cabinet):
    client_id, path = cabinet
    data = xlsx.write_book(
        xlsx.Sheet(
            "Мой лист",
            ["Себестоимость за единицу, ₽", "название", "  NMID  "],
            [[150, "Носки", 42]],
        )
    )

    result = costs.save_upload(client_id, data, path=path)

    assert result.saved == 1
    assert costs.costs_for(client_id, path=path) == {42: Decimal("150.00")}


# --- чтение для отчётов ---


def test_missing_costs_lists_only_articles_without_a_cost(cabinet):
    client_id, path = cabinet
    costs.save_costs(client_id, {1: Decimal("10"), 2: Decimal("20")}, path=path)

    assert costs.missing_costs(client_id, [1, 2, 3, 4], path=path) == [3, 4]
    assert costs.costs_for(client_id, nm_ids=[2, 3], path=path) == {2: Decimal("20.00")}


def test_one_client_never_sees_the_costs_of_another(cabinet, tmp_path):
    client_id, path = cabinet
    other = db.admin_repo(path).ensure_client(778)
    costs.save_costs(client_id, {1: Decimal("10")}, path=path)

    assert costs.costs_for(other, path=path) == {}
    assert costs.missing_costs(other, [1], path=path) == [1]


# --- хендлер ---

from types import SimpleNamespace  # noqa: E402

from core import queue  # noqa: E402


class FakeMessage:
    def __init__(self, document=None):
        self.document = document
        self.sent: list[str] = []

    async def reply_text(self, text, **kwargs):
        self.sent.append(text)
        return self

    @property
    def last(self) -> str:
        return self.sent[-1] if self.sent else ""


class FakeUpdate:
    def __init__(self, telegram_id, message):
        self.effective_user = SimpleNamespace(id=telegram_id)
        self.effective_message = message
        self.message = message


class FakeBot:
    """Телеграм на верёвочках: что скачали и что отправили."""

    def __init__(self, payload=b""):
        self.payload = payload
        self.asked: list[str] = []
        self.documents: list[tuple[int, str, bytes]] = []

    async def get_file(self, file_id):
        self.asked.append(file_id)
        payload = self.payload

        class Downloaded:
            async def download_as_bytearray(self_inner):
                return bytearray(payload)

        return Downloaded()

    async def send_document(self, chat_id, document, filename=None, caption=None):
        self.documents.append((chat_id, filename, bytes(document)))


def document(name="себестоимость.xlsx", size=1000, file_id="f1"):
    return SimpleNamespace(file_name=name, file_size=size, file_id=file_id)


@pytest.fixture
def quiet_queue():
    said: list[tuple[int | None, str]] = []
    queue.set_notifier(lambda client_id, text: said.append((client_id, text)))
    yield said
    queue.set_notifier(None)


@pytest.mark.asyncio
async def test_costs_command_queues_the_work_and_lets_the_queue_promise(
    cabinet, quiet_queue
):
    from bot.handlers import costs as handler

    client_id, path = cabinet
    message = FakeMessage()

    await handler.costs_command(FakeUpdate(777, message), None, path=path)

    tasks = db.repo(client_id, path).rows("tasks")
    assert [row["kind"] for row in tasks] == [costs.TASK_KIND]
    # Обещание «принято, пришлю» даёт очередь, а не хендлер.
    assert quiet_queue == [(client_id, queue.ACCEPTED)]
    assert message.sent == []


@pytest.mark.asyncio
async def test_costs_command_without_a_cabinet_asks_to_connect_first(
    db_path, quiet_queue
):
    from bot.handlers import costs as handler

    client_id = db.admin_repo(db_path).ensure_client(999)
    message = FakeMessage()

    await handler.costs_command(FakeUpdate(999, message), None, path=db_path)

    assert db.repo(client_id, db_path).count("tasks") == 0
    assert "подключ" in message.last.lower()


@pytest.mark.asyncio
async def test_file_of_another_format_is_refused_without_downloading_it(cabinet):
    from bot.handlers import costs as handler

    client_id, path = cabinet
    message = FakeMessage(document(name="таблица.csv"))
    bot = FakeBot()

    await handler.costs_document(
        FakeUpdate(777, message), SimpleNamespace(bot=bot), path=path
    )

    assert bot.asked == []
    assert "xlsx" in message.last.lower()


@pytest.mark.asyncio
async def test_too_big_file_is_refused_before_it_is_downloaded(cabinet):
    from bot.handlers import costs as handler

    client_id, path = cabinet
    message = FakeMessage(document(size=999_000_000))
    bot = FakeBot()

    await handler.costs_document(
        FakeUpdate(777, message), SimpleNamespace(bot=bot), path=path
    )

    assert bot.asked == []
    assert "размер" in message.last.lower()


@pytest.mark.asyncio
async def test_good_file_answers_how_many_rows_were_taken(cabinet):
    from bot.handlers import costs as handler

    client_id, path = cabinet
    data = upload([[1, "art-1", "Носки", 100], [2, "art-2", "Кепка", "250,40"]])
    message = FakeMessage(document())
    bot = FakeBot(data)

    await handler.costs_document(
        FakeUpdate(777, message), SimpleNamespace(bot=bot), path=path
    )

    assert costs.costs_for(client_id, path=path)[2] == Decimal("250.40")
    assert "2" in message.last and "принят" in message.last.lower()


@pytest.mark.asyncio
async def test_broken_rows_come_back_as_a_list_with_row_numbers(cabinet):
    from bot.handlers import costs as handler

    client_id, path = cabinet
    data = upload([[1, "art-1", "Носки", 100], [2, "art-2", "Кепка", "сто"]])
    message = FakeMessage(document())

    await handler.costs_document(
        FakeUpdate(777, message), SimpleNamespace(bot=FakeBot(data)), path=path
    )

    assert "строка 3" in message.last
    assert "не число" in message.last


@pytest.mark.asyncio
async def test_alien_content_in_a_file_named_xlsx_is_explained_not_crashed(cabinet):
    from bot.handlers import costs as handler

    client_id, path = cabinet
    message = FakeMessage(document())

    await handler.costs_document(
        FakeUpdate(777, message),
        SimpleNamespace(bot=FakeBot(b"%PDF-1.4 nothing to see here")),
        path=path,
    )

    assert "xlsx" in message.last.lower()


@pytest.mark.asyncio
async def test_registered_task_sends_the_workbook_to_the_client(cabinet, monkeypatch):
    from bot.handlers import costs as handler

    client_id, path = cabinet
    bot = FakeBot()
    handler.register(
        SimpleNamespace(add_handler=lambda *a, **kw: None, bot=bot), path=path
    )
    monkeypatch.setattr(costs, "build_template", _fake_template)

    task = SimpleNamespace(id=1, client_id=client_id, kind=costs.TASK_KIND, payload={})
    await queue.handlers()[costs.TASK_KIND](task)

    chat_id, filename, payload = bot.documents[0]
    assert chat_id == 777
    assert filename.endswith(".xlsx")
    assert xlsx.looks_like_xlsx(payload)


async def _fake_template(client_id, **kwargs):
    return costs.template_bytes([card(1, "art-1", "Носки")])
