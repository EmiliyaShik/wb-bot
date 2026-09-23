"""Диалог счёта в боте: выбор периода, ИНН, PDF, уведомления, кнопка «Оплачен».

Шов тот же, что у всего проекта, - путь к базе. Телеграм заменён простыми
двойниками: нас интересует, что бот говорит и что он от этого записывает,
а не как python-telegram-bot доставляет сообщения.
"""

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from bot.handlers import billing as handler
from bot.handlers import tariffs
from core import access, billing, config, db

FRIDAY = datetime(2026, 9, 18, 10, 0, tzinfo=timezone.utc)
OWNER_ID = 999001
CLIENT_TG = 777001

SELLER_ENV = {
    "SELLER_NAME": "ИП Тестовый Тест Тестович",
    "SELLER_INN": "123456789047",
    "SELLER_OGRNIP": "300000000000000",
    "SELLER_ADDRESS": "г Тест, ул Тестовая, д 1",
    "SELLER_ACCOUNT": "40802810000000000000",
    "SELLER_BANK": "Тестовый банк",
    "SELLER_BIK": "040000000",
    "SELLER_CORR_ACCOUNT": "30101810000000000000",
}


# --- двойники телеграма ---


class FakeMessage:
    def __init__(self):
        self.sent = []
        self.documents = []

    async def reply_text(self, text, **kwargs):
        self.sent.append((text, kwargs))
        return self

    async def reply_document(self, document, **kwargs):
        self.documents.append((document, kwargs))
        return self

    @property
    def texts(self):
        return [text for text, _ in self.sent]

    @property
    def last(self):
        return self.sent[-1][0]


class FakeQuery:
    def __init__(self, data, message):
        self.data = data
        self.message = message
        self.answered = False

    async def answer(self, *args, **kwargs):
        self.answered = True


class FakeUpdate:
    def __init__(self, telegram_id, message, query=None, text=""):
        self.effective_user = SimpleNamespace(id=telegram_id)
        self.effective_message = message
        self.message = message
        self.callback_query = query
        message.text = text


class FakeBot:
    def __init__(self):
        self.messages = []
        self.documents = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text, kwargs))

    async def send_document(self, chat_id, document, **kwargs):
        self.documents.append((chat_id, document, kwargs))

    def to(self, chat_id):
        return [text for cid, text, _ in self.messages if cid == chat_id]


class FakeContext:
    def __init__(self):
        self.user_data = {}
        self.bot = FakeBot()


class FakeApp:
    """Собирает то, что хендлер себе зарегистрировал."""

    def __init__(self):
        self.handlers = []
        self.bot = FakeBot()

    def add_handler(self, item, group=0):
        self.handlers.append((item, group))


@pytest.fixture
def wired(db_path, monkeypatch):
    """База, владелец и заполненные реквизиты: рабочее состояние бота."""
    monkeypatch.setattr(config, "db_path", lambda: db_path)
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", str(OWNER_ID))
    monkeypatch.setenv("OWNER_CONTACT", "@vladelec_testovyi")
    monkeypatch.setenv("DADATA_API_KEY", "")
    for name, value in SELLER_ENV.items():
        monkeypatch.setenv(name, value)
    return db_path


@pytest.fixture
def client_id(wired):
    return db.admin_repo(wired).ensure_client(CLIENT_TG)


async def walk_to_invoice(context, module="finance", months=1, inn="1234567894"):
    """Проводит клиента по всему диалогу до готового счёта."""
    message = FakeMessage()
    update = FakeUpdate(CLIENT_TG, message)
    await handler.start_dialog(update, context, module)

    pick = FakeUpdate(CLIENT_TG, message, FakeQuery(f"inv:p:{module}:{months}", message))
    await handler.period_callback(pick, context)

    for answer in (inn, "ООО Ромашка", "г Пример, ул Тестовая, д 2"):
        step = FakeUpdate(CLIENT_TG, message, text=answer)
        try:
            await handler.text_step(step, context)
        except handler.ApplicationHandlerStop:
            pass
        if not context.user_data.get(handler.STATE):
            break
    return message


# --- оплата картой и поддержка ---


def test_paysupport_shows_owner_contact(wired):
    assert "@vladelec_testovyi" in handler.paysupport_text()


def test_paysupport_without_contact_invents_nothing(wired, monkeypatch):
    monkeypatch.setenv("OWNER_CONTACT", "")
    text = handler.paysupport_text()
    assert "@" not in text
    assert "готовится" in text or "скоро" in text


def test_card_button_leads_to_the_same_contact(wired):
    assert "@vladelec_testovyi" in handler.card_text()
    # Бот про кассу ничего не знает: ни платёжных ссылок, ни провайдеров.
    for word in ("касса", "эквайринг", "ЮKassa", "Tinkoff"):
        assert word.lower() not in handler.card_text().lower()


# --- подключение к витрине ---


def test_register_takes_over_the_buy_button(wired):
    tariffs.set_buy_dialog(None)
    try:
        handler.register(FakeApp())
        assert tariffs.buy_dialog() is handler.start_dialog
    finally:
        tariffs.set_buy_dialog(None)


def test_period_keyboard_shows_every_period_with_its_discounted_price(wired):
    rows = handler.period_keyboard("finance").inline_keyboard
    labels = [button.text for row in rows for button in row]
    joined = " ".join(labels)
    for months in config.periods():
        assert f"{months}" in joined
    # 990 за месяц, три месяца со скидкой 10 процентов: 2 673.
    # Разряды бот разделяет неразрывным пробелом, поэтому он тут и записан.
    assert "2 673" in joined
    datas = [button.callback_data for row in rows for button in row]
    assert "inv:p:finance:3" in datas
    assert any(item.startswith("inv:card") for item in datas)


# --- диалог целиком ---


@pytest.mark.asyncio
async def test_dialog_asks_inn_and_makes_an_invoice(wired, client_id):
    context = FakeContext()
    message = await walk_to_invoice(context, "finance", 3)

    joined = " ".join(message.texts)
    assert "ИНН" in joined
    assert "2 673" in joined

    invoices = billing.invoices_of(client_id, path=wired)
    assert len(invoices) == 1
    made = invoices[0]
    assert made.module == "finance"
    assert made.period_months == 3
    assert made.org_name == "ООО Ромашка"
    assert made.inn == "1234567894"

    # Клиент получил файл счёта, а не обещание файла.
    assert message.documents, "PDF не отправлен"
    document, kwargs = message.documents[0]
    assert kwargs.get("filename") == f"{made.number}.pdf"
    assert bytes(document).startswith(b"%PDF")


@pytest.mark.asyncio
async def test_bad_inn_is_refused_before_any_request(wired, client_id):
    context = FakeContext()
    message = FakeMessage()
    await handler.start_dialog(FakeUpdate(CLIENT_TG, message), context, "finance")
    pick = FakeUpdate(CLIENT_TG, message, FakeQuery("inv:p:finance:1", message))
    await handler.period_callback(pick, context)

    step = FakeUpdate(CLIENT_TG, message, text="1234567890")
    try:
        await handler.text_step(step, context)
    except handler.ApplicationHandlerStop:
        pass

    assert "цифры" in message.last
    # Счёт не выставлен, диалог остался на том же шаге.
    assert billing.invoices_of(client_id, path=wired) == []
    assert context.user_data[handler.STATE]["step"] == "inn"


@pytest.mark.asyncio
async def test_owner_is_told_about_every_new_invoice(wired, client_id):
    context = FakeContext()
    await walk_to_invoice(context, "finance", 1)
    made = billing.invoices_of(client_id, path=wired)[0]

    to_owner = " ".join(context.bot.to(OWNER_ID))
    assert made.number in to_owner                 # номер
    assert "Финансы" in to_owner                   # что
    assert "990" in to_owner                       # сколько
    assert f"клиент #{client_id}" in to_owner      # кто, внутренним id
    # R159: Telegram-аккаунт клиента в админку не уезжает даже сюда.
    assert str(CLIENT_TG) not in to_owner


@pytest.mark.asyncio
async def test_empty_vat_note_reminds_the_owner(wired, client_id):
    context = FakeContext()
    await walk_to_invoice(context, "finance", 1)
    to_owner = " ".join(context.bot.to(OWNER_ID))
    assert "vat_note" in to_owner
    # Счёт при этом выставлен: пустой налоговый режим его не останавливает.
    assert billing.invoices_of(client_id, path=wired)


@pytest.mark.asyncio
async def test_text_outside_the_dialog_is_left_to_other_handlers(wired, client_id):
    context = FakeContext()
    message = FakeMessage()
    step = FakeUpdate(CLIENT_TG, message, text="123456789")
    # Ни ответа, ни остановки цепочки: артикул должен дойти до старого бота.
    await handler.text_step(step, context)
    assert message.sent == []


# --- чужой текст в разметке ---

# Название организации клиент пишет руками, адрес приезжает из DaData.
# Внутри и то и другое лежит как есть, а вот в сообщении разметки быть не
# должно: она уходит владельцу в личку от имени его же бота.
EVIL_NAME = '<a href="http://zlo.example/dai-token">ООО Ромашка</a>'
EVIL_ADDRESS = "г <i>Пример</i>, ул Тестовая, д 2"

# Теги, которых бот не пишет нигде: если такой встретился, его подставил не
# бот. Свои <b> и <code> в списке отсутствуют намеренно, они тут законны.
ALIEN_TAGS = ("<a ", "<a href", "</a>", "<i>", "</i>", "<script")


def assert_no_markup(text, *needles):
    """Ни одного чужого тега в сообщении, но сам текст на месте."""
    for tag in ALIEN_TAGS:
        assert tag not in text, f"чужая разметка уехала в сообщение: {tag}"
    for needle in needles:
        assert needle in text


@pytest.mark.asyncio
async def test_name_typed_by_the_client_is_escaped_everywhere(wired, client_id):
    context = FakeContext()
    message = FakeMessage()
    update = FakeUpdate(CLIENT_TG, message)
    await handler.start_dialog(update, context, "finance")
    await handler.period_callback(
        FakeUpdate(CLIENT_TG, message, FakeQuery("inv:p:finance:1", message)), context
    )
    for answer in ("1234567894", EVIL_NAME, EVIL_ADDRESS):
        try:
            await handler.text_step(FakeUpdate(CLIENT_TG, message, text=answer), context)
        except handler.ApplicationHandlerStop:
            pass

    # Счёт выставлен, и в базе имя лежит как его написали: экранирование это
    # про разметку сообщения, а не про данные.
    made = billing.invoices_of(client_id, path=wired)[0]
    assert made.org_name == EVIL_NAME

    to_owner = " ".join(context.bot.to(OWNER_ID))
    assert_no_markup(to_owner, "&lt;a href=", "ООО Ромашка")
    assert "zlo.example" in to_owner  # текст не потерян, он просто не ссылка
    # И ни в одном сообщении клиенту чужого тега тоже нет.
    for text in message.texts:
        assert_no_markup(text)


@pytest.mark.asyncio
async def test_answer_of_dadata_is_escaped_too(wired, client_id, monkeypatch):
    from core.billing import counterparty

    async def fake_lookup(inn, **kwargs):
        return counterparty.Counterparty(
            inn=str(inn), name=EVIL_NAME, address=EVIL_ADDRESS
        )

    monkeypatch.setattr(counterparty, "lookup", fake_lookup)

    context = FakeContext()
    message = FakeMessage()
    await handler.start_dialog(FakeUpdate(CLIENT_TG, message), context, "finance")
    await handler.period_callback(
        FakeUpdate(CLIENT_TG, message, FakeQuery("inv:p:finance:1", message)), context
    )
    try:
        await handler.text_step(FakeUpdate(CLIENT_TG, message, text="1234567894"), context)
    except handler.ApplicationHandlerStop:
        pass

    assert_no_markup(message.last, "ООО Ромашка", "Пример")
    # В состоянии диалога, а значит и в счёте, лежит исходная строка.
    assert context.user_data[handler.STATE]["name"] == EVIL_NAME


def test_owner_contact_is_data_not_markup(wired, monkeypatch):
    monkeypatch.setenv("OWNER_CONTACT", '<a href="http://zlo.example">@kontakt</a>')
    assert_no_markup(handler.card_text(), "@kontakt")
    assert_no_markup(handler.paysupport_text(), "@kontakt")


# --- callback_data это не доверенный канал ---


@pytest.mark.asyncio
async def test_forged_callback_cannot_buy_a_hidden_module(wired, client_id):
    context = FakeContext()
    message = FakeMessage()
    # Модуль ads в конфиге скрыт, кнопки с ним бот не рисует.
    await handler.start_dialog(FakeUpdate(CLIENT_TG, message), context, "ads")
    assert handler.STATE not in context.user_data

    await handler.period_callback(
        FakeUpdate(CLIENT_TG, message, FakeQuery("inv:p:ads:1", message)), context
    )
    assert handler.STATE not in context.user_data
    assert billing.invoices_of(client_id, path=wired) == []


@pytest.mark.asyncio
async def test_forged_callback_cannot_invent_a_period(wired, client_id):
    context = FakeContext()
    message = FakeMessage()
    # Срока в 7 месяцев в тарифах нет, но цену за него config.price посчитает.
    assert 7 not in config.periods()
    for data in ("inv:p:finance:7", "inv:p:finance:0", "inv:p:finance:-3"):
        await handler.period_callback(
            FakeUpdate(CLIENT_TG, message, FakeQuery(data, message)), context
        )
        assert handler.STATE not in context.user_data
    assert billing.invoices_of(client_id, path=wired) == []


@pytest.mark.asyncio
async def test_garbage_callback_does_not_break_the_bot(wired, client_id):
    context = FakeContext()
    message = FakeMessage()
    for data in ("inv:p:", "inv:p:finance", "inv:p:finance:мес", "inv:p:a:b:c"):
        await handler.period_callback(
            FakeUpdate(CLIENT_TG, message, FakeQuery(data, message)), context
        )
    assert handler.STATE not in context.user_data
    assert billing.invoices_of(client_id, path=wired) == []


@pytest.mark.asyncio
async def test_hidden_module_in_the_dialog_state_makes_no_invoice(wired, client_id):
    """Последняя проверка перед деньгами: состояние тоже сверяется с витриной."""
    context = FakeContext()
    context.user_data[handler.STATE] = {"module": "ads", "months": 1, "step": "address"}
    message = FakeMessage()
    try:
        await handler.text_step(
            FakeUpdate(CLIENT_TG, message, text="г Пример, ул Тестовая, д 2"), context
        )
    except handler.ApplicationHandlerStop:
        pass
    assert billing.invoices_of(client_id, path=wired) == []


# --- предел обращений к справочнику ---


@pytest.mark.asyncio
async def test_dadata_limit_leaves_the_manual_path_open(wired, client_id, monkeypatch):
    """Выбранная квота меняет слова, а не дорогу: счёт всё равно выставляется."""
    from core.billing import counterparty

    counterparty.forget()
    monkeypatch.setenv("DADATA_API_KEY", "ключ-для-теста")
    monkeypatch.setattr(counterparty, "per_day", lambda: 1)

    class Silent:
        """Справочник молчит: обращение потрачено, ответ не запомнен."""

        async def post(self, url, **kwargs):
            raise OSError("справочник недоступен")

    try:
        # Единственное на сегодня обращение клиент потратил раньше. Дальше в
        # сеть уже не ходят: своего http диалог не подставляет, и живой
        # запрос из теста означал бы, что предел не сработал.
        await counterparty.lookup("1000000002", client_id=client_id, http=Silent())
        assert counterparty.throttled(client_id) is True

        context = FakeContext()
        message = FakeMessage()
        await handler.start_dialog(FakeUpdate(CLIENT_TG, message), context, "finance")
        await handler.period_callback(
            FakeUpdate(CLIENT_TG, message, FakeQuery("inv:p:finance:1", message)),
            context,
        )
        # Верный ИНН, но в сеть за ним уже не идут: квота выбрана.
        try:
            await handler.text_step(
                FakeUpdate(CLIENT_TG, message, text="1234567894"), context
            )
        except handler.ApplicationHandlerStop:
            pass

        assert "исчерпан" in message.last
        assert "руками" in message.last or "Пришлите наименование" in message.last
        assert context.user_data[handler.STATE]["step"] == "name"

        # И ручная дорога рабочая: счёт выставлен.
        for answer in ("ООО Ромашка", "г Пример, ул Тестовая, д 2"):
            try:
                await handler.text_step(
                    FakeUpdate(CLIENT_TG, message, text=answer), context
                )
            except handler.ApplicationHandlerStop:
                pass
        assert len(billing.invoices_of(client_id, path=wired)) == 1
    finally:
        counterparty.forget()


# --- пустые реквизиты ---


@pytest.mark.asyncio
async def test_without_seller_details_client_waits_and_owner_gets_the_list(
    wired, client_id, monkeypatch
):
    for name in SELLER_ENV:
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("SELLER_NAME", "ИП Тестовый Тест Тестович")

    context = FakeContext()
    message = await walk_to_invoice(context, "finance", 1)

    assert "свяж" in message.last.lower()
    assert not message.documents
    assert billing.invoices_of(client_id, path=wired) == []

    to_owner = " ".join(context.bot.to(OWNER_ID))
    assert "SELLER_BIK" in to_owner
    assert "SELLER_ACCOUNT" in to_owner
    # Заполненную переменную владельцу не называют.
    assert "SELLER_NAME" not in to_owner
    # И здесь клиент назван внутренним id, а не Telegram-аккаунтом.
    assert f"клиент #{client_id}" in to_owner
    assert str(CLIENT_TG) not in to_owner


# --- кнопка владельца «Оплачен» ---


@pytest.mark.asyncio
async def test_paid_button_turns_access_on_and_second_press_does_not_extend(
    wired, client_id
):
    made = billing.create_invoice(client_id, "finance", 1, now=FRIDAY, path=wired)

    owner_message = FakeMessage()
    context = FakeContext()
    press = FakeUpdate(
        OWNER_ID, owner_message, FakeQuery(f"invpay:{made.number}", owner_message)
    )

    await handler.paid_callback(press, context)
    first = access.access_of(client_id, "finance", path=wired)
    assert first.works is True
    assert billing.invoice(made.number, path=wired).status == billing.PAID
    # Клиенту сказали, что модуль включён, и назвали дату окончания.
    to_client = " ".join(context.bot.to(CLIENT_TG))
    assert "Финансы" in to_client

    await handler.paid_callback(press, context)
    second = access.access_of(client_id, "finance", path=wired)
    assert second.until == first.until, "повторное нажатие продлило доступ"
    assert "уже" in owner_message.last.lower()


@pytest.mark.asyncio
async def test_paid_button_is_for_the_owner_only(wired, client_id):
    made = billing.create_invoice(client_id, "finance", 1, now=FRIDAY, path=wired)
    message = FakeMessage()
    context = FakeContext()
    press = FakeUpdate(CLIENT_TG, message, FakeQuery(f"invpay:{made.number}", message))

    await handler.paid_callback(press, context)
    assert access.has_access(client_id, "finance", path=wired) is False


# --- ежедневная проверка просроченных ---


@pytest.mark.asyncio
async def test_daily_job_moves_overdue_invoices_and_reminds_the_client(wired, client_id):
    made = billing.create_invoice(client_id, "finance", 1, now=FRIDAY, path=wired)
    assert made.due_at == date(2026, 9, 25)

    app = FakeApp()
    job = handler.make_overdue_job(app)
    await job(SimpleNamespace(payload={"date": "2026-09-26"}, client_id=None))

    assert billing.invoice(made.number, path=wired).status == billing.OVERDUE
    reminder = " ".join(app.bot.to(CLIENT_TG))
    assert made.number in reminder

    # Повтор на следующий день второго напоминания не шлёт.
    app2 = FakeApp()
    await handler.make_overdue_job(app2)(
        SimpleNamespace(payload={"date": "2026-09-27"}, client_id=None)
    )
    assert app2.bot.messages == []


def test_register_wires_commands_and_the_daily_check(wired):
    from core import queue, scheduler

    scheduler.reset()
    queue.reset()
    tariffs.set_buy_dialog(None)
    app = FakeApp()
    try:
        handler.register(app)
        # Просроченные счета проверяются ежедневно, своего расписания нет.
        assert handler.DAILY_NAME in scheduler.daily_names()
        assert handler.DAILY_NAME in queue.handlers()
        # Текстовый шаг стоит в более ранней группе, чем запасной обработчик
        # артикула, иначе ИНН до диалога не дошёл бы.
        groups = {group for _, group in app.handlers}
        assert handler.DIALOG_GROUP in groups
        assert handler.DIALOG_GROUP < 0
        commands = [
            item
            for item, _ in app.handlers
            if "paysupport" in getattr(item, "commands", set())
        ]
        assert commands, "команда /paysupport не зарегистрирована"
    finally:
        scheduler.reset()
        queue.reset()
        tariffs.set_buy_dialog(None)
