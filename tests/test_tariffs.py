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
    # Цены из спецификации, история C1: finance 990, rnp 590.
    # Разряды в тексте бота разделяются неразрывным пробелом.
    assert "990" in text and "590" in text
    # Скидка 10 процентов за 3 месяца: 990 * 3 = 2970, минус 10 процентов.
    assert "2 673" in text
    # Реклама и воронка открыты: их цены на витрине есть.
    assert "Реклама" in text and "1 290" in text
    assert "Воронка" in text and "490" in text
    # Пакет владелец открыл: он стоит на витрине со своей ценой.
    assert "Всё сразу" in text and "2 490" in text


def test_the_shop_window_has_a_buy_button_for_every_module_it_sells():
    """Витрина обещает кнопку у нужного модуля, и кнопка там есть.

    Проверяется по конфигу, а не списком имён: откроется ads - кнопка появится
    сама, и тест об этом узнает.
    """
    rows = tariffs.tariffs_keyboard().inline_keyboard
    offered = {button.callback_data: button.text for row in rows for button in row}

    for name, info in config.visible_modules().items():
        assert f"buy:{name}" in offered, f"на витрине нет кнопки модуля {name}"
        # Надпись называет модуль: три «Оформить» подряд неразличимы.
        assert info.title in offered[f"buy:{name}"]

    # Ряд на модуль: на телефоне так читается.
    assert all(len(row) == 1 for row in rows)


def test_a_hidden_module_does_not_get_a_buy_button(monkeypatch):
    """Скрытых модулей в конфиге не осталось: владелец открыл все четыре и
    пакет. Модуль прячется здесь же, чтобы правило оставалось проверенным и в
    тот день, когда владелец снова что-нибудь скроет."""
    patched = copy.deepcopy(config.settings())
    patched["modules"]["funnel"]["visible"] = False
    monkeypatch.setattr(config, "settings", lambda: patched)

    offered = {
        button.callback_data
        for row in tariffs.tariffs_keyboard().inline_keyboard
        for button in row
    }
    assert "buy:funnel" not in offered
    assert offered == {f"buy:{name}" for name in config.visible_modules()}
    assert tariffs.for_sale("funnel") is False
    assert "Воронка" not in tariffs.tariffs_text()


@pytest.mark.asyncio
async def test_the_shop_window_arrives_with_those_buttons(db_path, client_id):
    message = FakeMessage()
    await tariffs.tariffs_command(FakeUpdate(300300, message), None, path=db_path)

    text, kwargs = message.sent[-1]
    assert "Оформить" in text, "витрина обещает кнопку словами"
    offered = {
        button.callback_data
        for row in kwargs["reply_markup"].inline_keyboard
        for button in row
    }
    assert offered == {f"buy:{name}" for name in config.visible_modules()}


@pytest.mark.asyncio
async def test_a_button_from_the_shop_window_goes_through_the_same_check(
    db_path, monkeypatch
):
    """Дорожка к оплате одна: кнопка витрины и кнопка отказа ведут в buy_callback.

    Поэтому подделанный callback_data со скрытым модулем покупку не открывает,
    хотя такой кнопки не нарисовано нигде. Скрытых модулей в конфиге не
    осталось, поэтому модуль прячется здесь же.
    """
    patched = copy.deepcopy(config.settings())
    patched["modules"]["funnel"]["visible"] = False
    monkeypatch.setattr(config, "settings", lambda: patched)

    reached = []

    async def fake_dialog(update_, context_, module):
        reached.append(module)

    tariffs.set_buy_dialog(fake_dialog)
    try:
        for row in tariffs.tariffs_keyboard().inline_keyboard:
            for button in row:
                message = FakeMessage()
                update = FakeUpdate(300301, message, FakeQuery(button.callback_data, message))
                await tariffs.buy_callback(update, None, path=db_path)
        assert reached == list(config.visible_modules())

        # Тот же обработчик, но данные сочинил клиент.
        message = FakeMessage()
        update = FakeUpdate(300301, message, FakeQuery("buy:funnel", message))
        await tariffs.buy_callback(update, None, path=db_path)
    finally:
        tariffs.set_buy_dialog(None)

    assert reached == list(config.visible_modules()), "модуль скрыт, а счёт открылся"
    assert message.sent, "отказ должен быть понятным, а не тишиной"


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


def test_hidden_modules_cannot_be_taken_even_for_a_trial(monkeypatch):
    """Скрытых модулей в конфиге не осталось, поэтому модуль прячется здесь же:
    на пробу скрытое не выдаётся и кнопки не имеет."""
    patched = copy.deepcopy(config.settings())
    patched["modules"]["funnel"]["visible"] = False
    monkeypatch.setattr(config, "settings", lambda: patched)

    offered = {
        button.callback_data
        for row in tariffs.trial_keyboard().inline_keyboard
        for button in row
    }
    assert "trial:funnel" not in offered
    assert "trial:finance" in offered
    assert "trial:ads" in offered


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


# --- одна цена, несколько разных отчётов ---
#
# Модуль «Финансы» за 990 это три отчёта, и так было задумано с самого начала.
# Одним слипшимся предложением это читается как одна функция, и цена выглядит
# дорого. Витрина обязана называть каждый отчёт отдельно.


def test_the_shop_window_names_every_report_of_the_finance_module():
    reports = config.modules()["finance"].reports
    assert len(reports) == 3, "модуль «Финансы» это три отчёта"

    text = tariffs.tariffs_text()
    for report in reports:
        assert report.title in text, report.command
        assert f"/{report.command}" in text, report.command
        assert report.gives in text, report.command


def test_the_offer_instead_of_a_refusal_names_them_too():
    """Человек видит цену впервые здесь, и здесь же он должен видеть, за что."""
    text = tariffs.offer_text("finance")
    for report in config.modules()["finance"].reports:
        assert report.title in text, report.command
        assert f"/{report.command}" in text, report.command
    assert "990" in text


def test_a_module_with_a_single_report_keeps_its_old_short_description():
    """У «План-факта» отчёт один: список повторил бы «что входит» слово в слово."""
    rnp = config.modules()["rnp"]
    assert len(rnp.reports) == 1
    assert tariffs.report_lines("rnp") == []

    text = tariffs.tariffs_text()
    assert rnp.title in text
    assert tariffs.what_it_gives("rnp") in text
    assert "<b>План-факт</b>, команда" not in text

    # И модуль без списка отчётов вовсе тоже жив: у пакета его нет.
    assert tariffs.reports_of("all") == ()
    assert tariffs.report_lines("all") == []


def test_a_report_text_from_config_does_not_become_markup(monkeypatch):
    """Список отчётов пишет владелец, и он уходит в сообщение с разметкой."""
    patched = copy.deepcopy(config.settings())
    patched["modules"]["finance"]["reports"][0]["title"] = TRAP
    patched["modules"]["finance"]["reports"][1]["gives"] = TRAP
    monkeypatch.setattr(config, "settings", lambda: patched)

    for text in (tariffs.tariffs_text(), tariffs.offer_text("finance")):
        assert "<a href" not in text
        assert "&lt;a href=&quot;" in text


@pytest.mark.asyncio
async def test_buy_button_refuses_a_module_that_is_not_for_sale(monkeypatch):
    """Кнопки скрытого модуля нет ни на одной клавиатуре, но callback_data
    шлётся руками. Скрытых в конфиге не осталось, поэтому модуль прячется здесь.
    """
    patched = copy.deepcopy(config.settings())
    patched["modules"]["funnel"]["visible"] = False
    monkeypatch.setattr(config, "settings", lambda: patched)

    message = FakeMessage()
    update = FakeUpdate(400400, message, FakeQuery("buy:funnel", message))
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
    assert tariffs.for_sale("funnel") is False
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


# --- знаки препинания ---
#
# Строка «что даёт модуль» живёт в config.toml, и это законченная фраза. Шаблон
# дописывал свою точку сверху, и клиент видел две точки подряд. Правило «в
# конфиге без точки» продержалось бы до первой правки настроек, поэтому знать
# про точку обязан код.


@pytest.mark.parametrize("gives", ["дешевле, чем по отдельности.", "дешевле, чем по отдельности"])
def test_a_description_reads_the_same_with_a_final_dot_and_without_it(monkeypatch, gives):
    patched = copy.deepcopy(config.settings())
    patched["modules"]["finance"]["gives"] = gives
    monkeypatch.setattr(config, "settings", lambda: patched)

    for text in (tariffs.tariffs_text(), tariffs.offer_text("finance")):
        assert "дешевле, чем по отдельности." in text
        assert ".." not in text


def test_a_description_that_ends_with_a_question_keeps_its_sign(monkeypatch):
    patched = copy.deepcopy(config.settings())
    patched["modules"]["finance"]["gives"] = "Куда утекают деньги?"
    monkeypatch.setattr(config, "settings", lambda: patched)

    text = tariffs.offer_text("finance")
    assert "Куда утекают деньги?" in text
    assert "?." not in text


# --- пакет «Всё сразу» открыт ---
#
# Владелец открыл пакет и назначил цену. По отдельности четыре модуля это
# 990 + 590 + 1290 + 490 = 3360, пакет стоит 2490: дешевле на 870, то есть на
# 26 процентов. Продаётся он как обычный модуль, но пробно не выдаётся
# никогда: пробуют по одному модулю, а не всё сразу.


def test_the_package_is_sold_but_never_given_for_a_trial(db_path):
    assert config.modules()["all"].visible is True
    assert tariffs.for_sale("all") is True
    assert "all" in config.visible_modules()

    text = tariffs.tariffs_text()
    assert "Всё сразу" in text
    assert "2 490" in text

    offered = {
        button.callback_data
        for row in tariffs.tariffs_keyboard().inline_keyboard
        for button in row
    }
    assert offered == {"buy:finance", "buy:rnp", "buy:ads", "buy:funnel", "buy:all"}
    assert "Финансы" in text and "План-факт" in text and "Реклама" in text
    assert "Воронка" in text

    # Кнопки пробного периода у пакета нет, и держится это не клавиатурой:
    # правило живёт в start_trial, поэтому и подделанный trial:all пуст.
    trial_offered = {
        button.callback_data
        for row in tariffs.trial_keyboard().inline_keyboard
        for button in row
    }
    assert "trial:all" not in trial_offered

    admin = db.admin_repo(db_path)
    client = admin.ensure_client(300302)
    admin.set_client_fields(client, seller_id="WB-302")
    with pytest.raises(access.TrialDenied) as denied:
        access.start_trial(client, "all", path=db_path)
    assert denied.value.reason == "package"
    assert access.has_access(client, "all", path=db_path) is False
    assert admin.trials("WB-302") == []


@pytest.mark.asyncio
async def test_a_forged_trial_callback_does_not_hand_out_the_package(db_path):
    """Кнопки trial:all нет ни на одной клавиатуре, но callback_data сочиняется.

    Правило «пакеты пробно не отдаём» жило в клавиатуре, и подделанный trial:all
    отдавал пакет бесплатно на неделю. Теперь оно живёт в start_trial, и дорожка
    от кнопки до выдачи проверяется целиком: отказ доказан состоянием.
    """
    admin = db.admin_repo(db_path)
    client = admin.ensure_client(300303)
    admin.set_client_fields(client, seller_id="WB-303")

    message = FakeMessage()
    update = FakeUpdate(300303, message, FakeQuery("trial:all", message))
    await tariffs.trial_callback(update, None, path=db_path)

    assert access.has_access(client, "all", path=db_path) is False
    assert access.access_of(client, "all", path=db_path).state == "off"
    # Попытка не потрачена: пробный период на модуль клиенту ещё доступен.
    assert admin.trials("WB-303") == []
    assert access.start_trial(client, "finance", path=db_path).state == "active"
    assert message.sent, "отказ должен быть понятным, а не тишиной"


def test_the_package_is_gone_from_the_trial_and_the_open_modules_are_not():
    offered = {
        button.callback_data
        for row in tariffs.trial_keyboard().inline_keyboard
        for button in row
    }
    assert offered == {"trial:finance", "trial:rnp", "trial:ads", "trial:funnel"}
