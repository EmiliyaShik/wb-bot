"""Витрина тарифов и заготовка для платных команд. Шов один: путь к базе."""

import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from core import access, config, db
from bot.handlers import tariffs

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def client_id(db_path):
    return db.admin_repo(db_path).ensure_client(300300)


def test_tariffs_shows_only_visible_modules_with_prices_from_config(db_path, client_id):
    text = tariffs.tariffs_text(client_id, now=T0, path=db_path)
    # Цены из спецификации, история C1: finance 990, rnp 590, all 2 290.
    # Разряды в тексте бота разделяются неразрывным пробелом.
    assert "990" in text and "590" in text and "2 290" in text
    # Скидка 10 процентов за 3 месяца: 990 * 3 = 2970, минус 10 процентов.
    assert "2 673" in text
    # C11: ads и funnel описаны в конфиге, но их не видно и не купить.
    assert "Реклама" not in text and "Воронка" not in text
    assert "1 290" not in text


def test_offer_names_price_and_gives_a_buy_button():
    text = tariffs.offer_text("finance")
    assert "990" in text
    assert "Финансы" in text
    buttons = tariffs.offer_keyboard("finance").inline_keyboard[0]
    assert buttons[0].callback_data == "buy:finance"
    assert "Оформить" in buttons[0].text


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
        self.answered = False

    async def answer(self, *args, **kwargs):
        self.answered = True


class FakeUpdate:
    def __init__(self, telegram_id, message, query=None):
        self.effective_user = SimpleNamespace(id=telegram_id)
        self.effective_message = message
        self.callback_query = query


@pytest.mark.asyncio
async def test_require_module_offers_instead_of_refusing_and_then_lets_through(db_path):
    message = FakeMessage()
    update = FakeUpdate(400400, message)
    passed = []

    @tariffs.require_module("finance", path=db_path)
    async def handler(update_, context_):
        passed.append(update_)

    await handler(update, None)
    assert passed == []
    text, kwargs = message.sent[0]
    assert "990" in text
    button = kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.callback_data == "buy:finance"

    granted_to = db.admin_repo(db_path).ensure_client(400400)
    access.grant_access(
        granted_to, "finance", 30, "WBR-2026-0006", "invoice", "owner", path=db_path
    )
    await handler(update, None)
    assert len(passed) == 1


@pytest.mark.asyncio
async def test_buy_button_answers_before_the_invoice_dialog_exists_and_yields_to_it():
    message = FakeMessage()
    query = FakeQuery("buy:finance", message)
    update = FakeUpdate(400400, message, query)

    tariffs.set_buy_dialog(None)
    try:
        await tariffs.buy_callback(update, None)
        assert query.answered is True
        assert message.sent, "кнопка не должна отвечать тишиной"

        seen = []

        async def fake_dialog(update_, context_, module):
            seen.append(module)

        tariffs.set_buy_dialog(fake_dialog)
        await tariffs.buy_callback(update, None)
        assert seen == ["finance"]
        assert len(message.sent) == 1
    finally:
        tariffs.set_buy_dialog(None)


def test_switch_on_message_names_the_end_date_in_moscow_time(db_path, client_id):
    # 22:30 UTC это уже следующие сутки в Москве: тест разойдётся, если дату
    # посчитать в UTC.
    late = datetime(2026, 3, 1, 22, 30, tzinfo=timezone.utc)
    granted = access.grant_access(
        client_id, "finance", 30, "WBR-2026-0007", "invoice", "owner",
        now=late, path=db_path,
    )
    assert granted.until == datetime(2026, 3, 31, 22, 30, tzinfo=timezone.utc)
    assert "01.04.2026" in tariffs.granted_text(granted)
    assert tariffs.local_date(granted.until) == "01.04.2026"


def test_price_line_shows_every_period_from_config(monkeypatch):
    patched = copy.deepcopy(config.settings())
    patched["periods"]["months_6"] = 15
    monkeypatch.setattr(config, "settings", lambda: patched)
    line = tariffs.price_line("rnp")
    # 590 * 6 = 3540, минус 15 процентов = 3009.
    assert "6 мес." in line
    assert "3 009" in line and "скидка 15%" in line


def test_rubles_rounds_the_decimal_instead_of_cutting_it():
    assert tariffs.rubles(Decimal("2672.50")) == "2 673 ₽"
    assert tariffs.rubles(Decimal("2672.49")) == "2 672 ₽"


def test_hidden_modules_cannot_be_taken_even_for_a_trial():
    offered = {
        button.callback_data
        for row in tariffs.trial_keyboard().inline_keyboard
        for button in row
    }
    assert "trial:ads" not in offered and "trial:funnel" not in offered
    assert "trial:finance" in offered


def test_handler_registers_itself():
    added = []

    class FakeApp:
        def add_handler(self, handler, group=0):
            added.append((type(handler).__name__, group))

    tariffs.register(FakeApp())
    assert ("CommandHandler", 0) in added
    assert any(name == "CallbackQueryHandler" for name, _ in added)


def test_module_description_comes_from_config_not_from_the_code(monkeypatch):
    patched = copy.deepcopy(config.settings())
    patched["modules"]["finance"]["gives"] = "строка только из конфига"
    monkeypatch.setattr(config, "settings", lambda: patched)
    assert tariffs.what_it_gives("finance") == "строка только из конфига"
    assert "строка только из конфига" in tariffs.offer_text("finance")
    assert "строка только из конфига" in tariffs.tariffs_text()


def test_descriptions_of_visible_modules_are_filled_in_config():
    for name in config.visible_modules():
        assert tariffs.what_it_gives(name), f"в конфиге нет gives у модуля {name}"


@pytest.mark.asyncio
async def test_buy_button_refuses_a_module_that_is_not_for_sale():
    """Кнопки ads нет ни на одной клавиатуре, но callback_data можно прислать руками."""
    message = FakeMessage()
    update = FakeUpdate(400400, message, FakeQuery("buy:ads", message))
    reached = []

    async def fake_dialog(update_, context_, module):
        reached.append(module)

    tariffs.set_buy_dialog(fake_dialog)
    try:
        await tariffs.buy_callback(update, None)
    finally:
        tariffs.set_buy_dialog(None)

    assert reached == [], "счёт не должен выставляться на скрытый модуль"
    assert message.sent, "отказ должен быть понятным, а не тишиной"
    assert tariffs.for_sale("ads") is False
    assert tariffs.for_sale("finance") is True


# --- чужой текст на витрине ---
#
# Название модуля и строка «что входит» лежат в конфиге, а витрина уходит с
# ParseMode.HTML. Конфиг пишет владелец, но угловая скобка в нём не должна
# ни превращаться в разметку, ни ронять отправку: тогда витрины не увидит
# ни один клиент.

TRAP = '<a href="http://zlo.example">нажми</a>'


def _with_trap(monkeypatch):
    patched = copy.deepcopy(config.settings())
    patched["modules"]["finance"]["title"] = TRAP
    patched["modules"]["finance"]["gives"] = TRAP
    monkeypatch.setattr(config, "settings", lambda: patched)


def test_a_module_title_from_config_does_not_become_markup(monkeypatch):
    _with_trap(monkeypatch)

    text = tariffs.tariffs_text()

    assert "<a href" not in text
    assert "&lt;a href=&quot;" in text
    assert "<b>" in text  # а разметка самого бота на месте


def test_the_same_holds_for_the_offer_shown_instead_of_a_refusal(monkeypatch):
    _with_trap(monkeypatch)

    text = tariffs.offer_text("finance")

    assert "<a href" not in text
    assert "&lt;a href=&quot;" in text
