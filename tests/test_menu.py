"""Меню команд Telegram и кнопки главных действий.

Два свойства проверяются здесь строже остальных.

Первое: админских команд нет в общем меню. Для постороннего их в этом боте
как бы не существует, они не отвечают ничем, и общее меню с ними отменило бы
всю эту работу. Проверяется списком того, что бот реально заявил Telegram, а
не текстом.

Второе: кнопка это та же команда. Она не повторяет её логику, а зовёт ту же
функцию, и прав не даёт никаких: платный модуль без доступа остаётся
недоступным и по кнопке тоже.

Швы те же два, что и везде: путь к базе и транспорт WB. Сети нет.
"""

from __future__ import annotations

import ast
import asyncio
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from agents import diagnostic as diagnostic_agent
from bot import app as bot_app
from bot import texts
from bot.handlers import (
    connect,
    diagnostic,
    dynamics,
    finance,
    menu,
    profit,
    settings,
    tariffs,
)
from bot.handlers import costs as costs_handler
from core import access, audit, config, db, queue

ROOT = Path(__file__).resolve().parent.parent
HANDLERS = ROOT / "bot" / "handlers"

# Команды владельца по спецификации. Список записан руками нарочно: сторож
# должен падать, когда админскую команду тихо пустят в общее меню.
OWNER_ONLY = {"stats", "acts", "grant", "revoke", "tasks", "diag"}


# --- приспособления ---


class FakeBot:
    """Запоминает, какой список команд и в какую область видимости уехал."""

    def __init__(self, broken: bool = False):
        self.declared: list[tuple[str, int | None, list[str]]] = []
        self.broken = broken

    async def set_my_commands(self, commands, scope=None, **kwargs):
        if self.broken:
            raise RuntimeError("Telegram недоступен")
        self.declared.append(
            (
                type(scope).__name__,
                getattr(scope, "chat_id", None),
                [command.command for command in commands],
            )
        )

    async def send_message(self, chat_id, text, **kwargs):
        return None

    def scope(self, name: str, chat_id: int | None = None) -> list[str]:
        for kind, chat, commands in self.declared:
            if kind == name and chat == chat_id:
                return commands
        return []


class FakeMessage:
    def __init__(self):
        self.sent: list[tuple[str, dict]] = []

    async def reply_text(self, text, **kwargs):
        self.sent.append((text, kwargs))
        return self

    @property
    def last(self) -> str:
        return self.sent[-1][0] if self.sent else ""


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
        self.message = message
        self.callback_query = query


def press(telegram_id: int, token: str) -> tuple[FakeUpdate, FakeMessage, FakeQuery]:
    """Нажатие кнопки меню: данные приходят от клиента, как в жизни."""
    message = FakeMessage()
    query = FakeQuery(f"{menu.PREFIX}{token}", message)
    return FakeUpdate(telegram_id, message, query), message, query


@pytest.fixture
def owner_data_dir(tmp_path, monkeypatch):
    """Временная папка данных: журнал и база не должны уехать в боевые."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "777, 888")
    db.migrate()
    yield tmp_path
    db.close_all()


@pytest.fixture
def cabinet(db_path):
    """Клиент с подключённым кабинетом: seller_id и категория «Финансы».

    Токен здесь не расшифровывается ни разу: до Wildberries дело не доходит,
    хендлер только ставит задачу в очередь.
    """
    queue.reset()
    client_id = db.admin_repo(db_path).ensure_client(555001)
    db.admin_repo(db_path).set_client_fields(client_id, seller_id="sid-menu")
    db.repo(client_id, db_path).insert(
        "wb_tokens", ciphertext=b"x", exp="1789000000", scopes="finance,analytics"
    )
    yield client_id
    queue.reset()


# --- меню команд: главное требование ---


@pytest.mark.asyncio
async def test_general_menu_has_no_admin_commands_and_the_owner_menu_has_them(
    owner_data_dir,
):
    """Списком, а не текстом: что уехало всем и что уехало владельцу."""
    bot = FakeBot()
    done = await menu.publish(bot)

    # Три заявки: всем и по одной каждому владельцу из ADMIN_TELEGRAM_IDS.
    assert done == 3

    everyone = bot.scope("BotCommandScopeAllPrivateChats")
    assert everyone, "общее меню не заявлено"
    assert OWNER_ONLY & set(everyone) == set(), (
        "админская команда попала в общее меню: " + ", ".join(sorted(OWNER_ONLY & set(everyone)))
    )
    assert "finance" in everyone and "connect" in everyone

    for admin_id in (777, 888):
        owner = bot.scope("BotCommandScopeChat", admin_id)
        assert OWNER_ONLY <= set(owner), f"владельцу {admin_id} не хватает админских команд"
        # Клиентские команды владелец тоже видит: он ими пользуется сам.
        assert set(everyone) <= set(owner)


def test_admin_commands_of_the_menu_are_exactly_the_ones_the_bot_hides():
    assert {name for name, _ in menu.ADMIN_COMMANDS} == OWNER_ONLY
    assert OWNER_ONLY & {name for name, _ in menu.CLIENT_COMMANDS} == set()


def test_descriptions_are_taken_from_help_and_fit_telegram():
    """Описания не выдуманы заново: это те же формулировки, что в /help."""
    parsed = dict(menu.from_help())
    assert parsed["finance"] == "недельная раскладка: сколько удержал Wildberries и за что"
    assert parsed["connect"] == "подключить кабинет Wildberries"

    for name, description in menu.CLIENT_COMMANDS:
        assert description, f"у /{name} пустое описание"
        assert len(description) <= menu.DESCRIPTION_LIMIT
        assert "<" not in description, f"в описании /{name} осталась разметка"
        if name not in ("start", "help"):
            assert f"<code>/{name}</code>" in texts.HELP
            assert description in texts.HELP

    # /start и /help в списке HELP не перечислены, но в меню они нужны.
    names = [name for name, _ in menu.CLIENT_COMMANDS]
    assert names[0] == "start" and names[-1] == "help"
    assert len(names) == len(set(names))


# --- Telegram молчит ---


@pytest.mark.asyncio
async def test_unavailable_telegram_does_not_break_the_menu_call(owner_data_dir):
    bot = FakeBot(broken=True)
    assert await menu.publish(bot) == 0

    events = audit.recent()
    warnings = [row for row in events if row["kind"] == "menu" and row["level"] == "warning"]
    assert warnings, "отказ Telegram не попал в журнал"


@pytest.mark.asyncio
async def test_unavailable_telegram_does_not_break_the_startup(owner_data_dir, monkeypatch):
    """Заявка меню это поход в сеть, и уронить запуск он не имеет права."""
    monkeypatch.setattr(bot_app, "WORKER_POLL_SEC", 0.05)

    app = SimpleNamespace(bot=FakeBot(broken=True), job_queue=None, bot_data={})
    await bot_app.start_background(app)
    try:
        worker = app.bot_data["queue_worker"]
        assert not worker.done(), "воркер очереди не поднялся"
    finally:
        await bot_app.stop_background(app)
        queue.set_notifier(None)


# --- кнопки ведут туда же, куда команды ---


def test_every_button_points_at_a_real_command_handler():
    for token, action in menu.ACTIONS.items():
        command = getattr(menu.module_of(action), action.attr, None)
        assert callable(command), f"кнопка {token} никуда не ведёт"
    assert menu.ACTIONS[menu.DIAGNOSTIC].module is diagnostic
    assert menu.ACTIONS[menu.CONNECT].module is connect
    assert menu.ACTIONS[menu.FINANCE].module is finance
    assert menu.ACTIONS[menu.SETTINGS].module is settings
    assert menu.ACTIONS[menu.TARIFFS].module is tariffs
    # /help лежит в bot/app.py, а тот импортирует меню: круг разрывает ленивый
    # импорт по имени, но приходит кнопка ровно в ту же функцию.
    assert menu.module_of(menu.ACTIONS[menu.HELP]) is bot_app


def test_every_button_hands_the_database_path_over():
    """Шов один на всех: хендлер без path в тестах пошёл бы в боевую базу."""
    for token, action in menu.ACTIONS.items():
        command = getattr(menu.module_of(action), action.attr)
        assert "path" in command.__code__.co_varnames, f"кнопка {token} не берёт path"


@pytest.mark.asyncio
async def test_button_calls_the_very_function_of_the_command(db_path, monkeypatch):
    """Подмена: заменили хендлер команды - кнопка попала в замену.

    Значит, кнопка не копия команды, а она сама: правка одна на двоих.
    """
    called: list[dict] = []

    async def spy(update, context, *, path=None, **rest):
        called.append({"update": update, "context": context, "path": path})

    monkeypatch.setattr(diagnostic, "diagnostic_command", spy)

    update, _, query = press(555002, menu.DIAGNOSTIC)
    context = object()
    await menu.pressed(update, context, path=db_path)

    assert query.answered, "кнопка осталась крутиться у клиента"
    assert len(called) == 1
    assert called[0]["update"] is update and called[0]["context"] is context
    assert called[0]["path"] == db_path


@pytest.mark.asyncio
async def test_button_puts_the_same_work_in_the_queue_as_the_command(db_path, cabinet):
    """Состоянием: после кнопки в очереди та же задача, что и после команды."""
    admin = db.admin_repo(db_path)
    assert admin.tasks_by_kind(diagnostic_agent.TASK_KIND) == []

    update, _, _ = press(555001, menu.DIAGNOSTIC)
    await menu.pressed(update, None, path=db_path)

    after_button = admin.tasks_by_kind(diagnostic_agent.TASK_KIND)
    assert len(after_button) == 1
    assert int(after_button[0]["client_id"]) == cabinet

    # Та же команда руками не добавляет второй задачи: очередь узнаёт в ней
    # ту же самую работу и возвращает ту же строку. Это и есть доказательство,
    # что кнопка попала не в похожий код, а в тот же самый.
    message = FakeMessage()
    await diagnostic.diagnostic_command(
        FakeUpdate(555001, message), None, path=db_path
    )
    after_command = admin.tasks_by_kind(diagnostic_agent.TASK_KIND)
    assert len(after_command) == 1
    assert int(after_command[0]["id"]) == int(after_button[0]["id"])


# --- кнопка не даёт прав ---


@pytest.mark.asyncio
async def test_paid_button_offers_instead_of_running_without_access(db_path, monkeypatch):
    """Нарисованная кнопка ничего не открывает: проверка живёт в функции."""
    ran: list[int] = []

    async def spy(update, context, *, path=None, **rest):
        ran.append(1)

    monkeypatch.setattr(finance, "finance_command", spy)

    update, message, query = press(555003, menu.FINANCE)
    await menu.pressed(update, None, path=db_path)

    assert query.answered
    assert ran == [], "платная команда отработала без доступа"
    text, kwargs = message.sent[-1]
    assert "Финансы" in text and "990" in text
    buttons = kwargs["reply_markup"].inline_keyboard[0]
    assert buttons[0].callback_data == "buy:finance"


@pytest.mark.asyncio
async def test_paid_button_works_when_the_module_is_paid_for(db_path, monkeypatch):
    ran: list[int] = []

    async def spy(update, context, *, path=None, **rest):
        ran.append(1)

    monkeypatch.setattr(finance, "finance_command", spy)

    client_id = db.admin_repo(db_path).ensure_client(555004)
    access.grant_access(
        client_id, "finance", 30, "WBR-2026-0042", "invoice", "owner", path=db_path
    )

    update, _, _ = press(555004, menu.FINANCE)
    await menu.pressed(update, None, path=db_path)
    assert ran == [1]


@pytest.mark.asyncio
async def test_made_up_callback_data_does_nothing_and_still_answers(db_path):
    update, message, query = press(555005, "grant")
    await menu.pressed(update, None, path=db_path)

    assert query.answered
    assert message.last == menu.UNKNOWN


# --- набор кнопок зависит от состояния в базе ---


def _tokens(update, db_path) -> list[str]:
    return [
        button.callback_data.removeprefix(menu.PREFIX)
        for row in menu.main_keyboard(update, path=db_path).inline_keyboard
        for button in row
    ]


def test_keyboard_depends_on_the_cabinet_read_from_the_database(db_path, cabinet):
    message = FakeMessage()

    # Без кабинета: с чего начать. Бесплатный разбор тут намеренно: он обещан в
    # приветствии, а без кабинета честно объясняет, что нужен /connect.
    stranger = _tokens(FakeUpdate(555006, message), db_path)
    assert stranger == [menu.CONNECT, menu.DIAGNOSTIC, menu.TARIFFS, menu.HELP]

    # С кабинетом: то, что селлер делает часто. Подключать нечего, а отчёты
    # собраны за одной кнопкой «Мои отчёты».
    seller = _tokens(FakeUpdate(555001, message), db_path)
    assert menu.CONNECT not in seller
    assert menu.DIAGNOSTIC in seller and menu.REPORTS in seller
    assert menu.SETTINGS in seller


def test_the_greeting_is_not_a_second_list_of_commands(db_path, cabinet):
    """Полный список живёт в меню Telegram, кнопки это «с чего начать»."""
    message = FakeMessage()
    for update in (FakeUpdate(555006, message), FakeUpdate(555001, message)):
        tokens = _tokens(update, db_path)
        assert len(tokens) == len(set(tokens))
        assert len(tokens) < len(menu.CLIENT_COMMANDS) / 2
        # Отчёты в приветствие не возвращаются поштучно: их четыре, и запас
        # сторожа они съедают целиком. Им отведён свой экран.
        assert not set(menu.report_tokens()) & set(tokens), (
            "отчёты снова разъехались по приветствию"
        )
        # На телефоне больше двух кнопок в ряду не читается.
        assert all(
            len(row) <= 2
            for row in menu.main_keyboard(update, path=db_path).inline_keyboard
        )


@pytest.mark.asyncio
async def test_the_what_i_can_do_button_calls_the_help_command(db_path, monkeypatch):
    """Кнопка «Что я умею» это /help, а не его копия рядом."""
    called: list[dict] = []

    async def spy(update, context, *, path=None, **rest):
        called.append({"path": path})

    monkeypatch.setattr(bot_app, "help_command", spy)

    update, _, query = press(555008, menu.HELP)
    await menu.pressed(update, None, path=db_path)

    assert query.answered
    assert called == [{"path": db_path}]


@pytest.mark.asyncio
async def test_the_tariffs_button_shows_the_shop_window_with_its_buy_buttons(db_path):
    update, message, _ = press(555009, menu.TARIFFS)
    await menu.pressed(update, None, path=db_path)

    text, kwargs = message.sent[-1]
    assert "Тарифы" in text
    offered = {
        button.callback_data
        for row in kwargs["reply_markup"].inline_keyboard
        for button in row
    }
    assert offered == {f"buy:{name}" for name in config.visible_modules()}


@pytest.mark.asyncio
async def test_start_and_help_come_with_buttons(owner_data_dir):
    message = FakeMessage()
    update = FakeUpdate(555007, message)

    await bot_app.start(update, None)
    await bot_app.help_command(update, None)

    for text, kwargs in message.sent:
        assert text
        markup = kwargs.get("reply_markup")
        assert markup is not None, "приветствие ушло без кнопок"
        assert markup.inline_keyboard[0][0].callback_data.startswith(menu.PREFIX)


# --- экран «мои отчёты»: за 990 куплен не один отчёт, а три ---

# Отчёты в том порядке, в каком их обещает конфиг. Список записан руками
# нарочно: сторож должен падать, когда отчёт тихо пропадёт из витрины.
REPORTS = ("finance", "dynamics", "profit", "rnp", "ads", "funnel")


def _keyboard_tokens(markup) -> list[str]:
    return [
        button.callback_data.removeprefix(menu.PREFIX)
        for row in markup.inline_keyboard
        for button in row
    ]


def test_the_reports_screen_is_built_from_the_config_and_misses_nobody():
    """Второго списка отчётов в коде нет: кнопки собраны по конфигу."""
    promised = [
        report.command
        for info in config.visible_modules().values()
        for report in info.reports
    ]
    assert promised == list(REPORTS)
    assert menu.report_tokens() == REPORTS

    markup = menu.reports_keyboard()
    assert _keyboard_tokens(markup) == list(REPORTS)
    # На телефоне больше двух кнопок в ряду не читается.
    assert all(len(row) <= 2 for row in markup.inline_keyboard)


def test_the_reports_screen_names_each_report_and_what_it_is_for():
    text = menu.reports_text()
    for info in config.visible_modules().values():
        for report in info.reports:
            assert report.title in text, report.command
            assert f"/{report.command}" in text
            assert report.gives in text, report.command
    # Видно, что за одни деньги куплено несколько разных отчётов.
    assert "три отчёта" in text
    assert "990" in text and "590" in text


def test_every_report_of_the_config_is_a_real_command_of_the_bot():
    """Кнопка отчёта ведёт в команду, которая у бота действительно есть."""
    known = dict(menu.CLIENT_COMMANDS)
    for token in menu.report_tokens():
        assert token in menu.ACTIONS, token
        assert token in known, token
        assert f"<code>/{token}</code>" in texts.HELP, token


@pytest.mark.asyncio
async def test_every_report_is_two_taps_from_the_greeting_and_calls_its_command(
    db_path, cabinet, monkeypatch
):
    """Приветствие -> «Мои отчёты» -> сам отчёт, и это та же его функция."""
    access.grant_access(
        cabinet, "finance", 30, "WBR-2026-0100", "invoice", "owner", path=db_path
    )
    access.grant_access(
        cabinet, "rnp", 30, "WBR-2026-0101", "invoice", "owner", path=db_path
    )

    seen: list[str] = []

    def spy(name: str):
        async def handler(update, context, *, path=None, **rest):
            seen.append(name)

        return handler

    monkeypatch.setattr(finance, "finance_command", spy("finance"))
    monkeypatch.setattr(dynamics, "dynamics_command", spy("dynamics"))
    monkeypatch.setattr(profit, "profit_command", spy("profit"))

    # Шаг первый: в приветствии есть дорога к отчётам.
    assert menu.REPORTS in _tokens(FakeUpdate(555001, FakeMessage()), db_path)

    # Шаг второй: экран отчётов, и на нём кнопка у каждого.
    update, screen, query = press(555001, menu.REPORTS)
    await menu.pressed(update, None, path=db_path)
    assert query.answered
    text, kwargs = screen.sent[-1]
    assert set(REPORTS) <= set(_keyboard_tokens(kwargs["reply_markup"]))

    # Шаг третий: кнопка отчёта попадает в ту самую функцию команды.
    for token in ("finance", "dynamics", "profit"):
        report, _, report_query = press(555001, token)
        await menu.pressed(report, None, path=db_path)
        assert report_query.answered, token
    assert seen == ["finance", "dynamics", "profit"]


@pytest.mark.asyncio
async def test_a_report_button_without_the_module_offers_instead_of_running(
    db_path, monkeypatch
):
    """Кнопки этот клиент не видел: `callback_data` он сочинил сам.

    Нарисованная кнопка прав не даёт, и выдуманная тоже: проверка стоит в
    самой команде, а не в клавиатуре.
    """
    ran: list[str] = []

    def spy(name: str):
        async def handler(update, context, *, path=None, **rest):
            ran.append(name)

        return handler

    monkeypatch.setattr(finance, "finance_command", spy("finance"))
    monkeypatch.setattr(dynamics, "dynamics_command", spy("dynamics"))
    monkeypatch.setattr(profit, "profit_command", spy("profit"))

    for token in ("finance", "dynamics", "profit"):
        update, message, query = press(555012, token)
        await menu.pressed(update, None, path=db_path)

        assert query.answered, token
        text, kwargs = message.sent[-1]
        assert "Финансы" in text and "990" in text, token
        buttons = kwargs["reply_markup"].inline_keyboard[0]
        assert buttons[0].callback_data == "buy:finance", token
    assert ran == [], "платный отчёт отработал по выдуманной кнопке"


@pytest.mark.asyncio
async def test_the_reports_screen_itself_is_free_and_promises_nothing(db_path):
    """Экран открывается и без подписки: он про то, что даёт модуль."""
    update, message, query = press(555013, menu.REPORTS)
    await menu.pressed(update, None, path=db_path)

    assert query.answered
    text, kwargs = message.sent[-1]
    assert "Деньги за неделю" in text
    assert _keyboard_tokens(kwargs["reply_markup"]) == list(REPORTS)


# --- префикс кнопок ни с чем не пересекается ---


def _prefixes() -> dict[str, str]:
    """Строковые константы-префиксы всех хендлеров, прямо из файлов."""
    found: dict[str, str] = {}
    for path in sorted(HANDLERS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if not isinstance(node.value, ast.Constant) or not isinstance(
                node.value.value, str
            ):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and "PREFIX" in target.id:
                    found[f"{path.stem}.{target.id}"] = node.value.value
    return found


def test_menu_prefix_collides_with_nobody():
    prefixes = _prefixes()
    assert prefixes["menu.PREFIX"] == menu.PREFIX
    for name, value in prefixes.items():
        if name == "menu.PREFIX":
            continue
        assert not value.startswith(menu.PREFIX), f"{name} перехватит кнопки меню"
        assert not menu.PREFIX.startswith(value), f"меню перехватит кнопки {name}"


def test_menu_group_is_the_default_one_and_frees_the_busy_ones():
    """Занятые группы: -100 частота, -10 диалог счёта, 40 документы, 50 «Оформить»."""
    added: list[tuple[object, int]] = []

    class FakeApp:
        def add_handler(self, handler, group=0):
            added.append((handler, group))

    menu.register(FakeApp())
    assert [group for _, group in added] == [0]


# --- кнопка себестоимости: она же и всё повторное напоминание ---


@pytest.mark.asyncio
async def test_a_connected_cabinet_without_costs_is_offered_to_enter_them(
    db_path, cabinet
):
    """Кабинет есть, себестоимости нет: кнопка стоит первой и ведёт в /costs."""
    from core import costs as costs_core

    message = FakeMessage()
    update = FakeUpdate(555001, message)

    assert _tokens(update, db_path)[0] == menu.COSTS

    # Напоминание останавливает сама себестоимость: ни таймера, ни тумблера,
    # выключать нечего. Первая же внесённая строка убирает кнопку.
    costs_core.save_costs(cabinet, {101: Decimal("10")}, path=db_path)
    assert menu.COSTS not in _tokens(update, db_path)


@pytest.mark.asyncio
async def test_the_costs_button_calls_the_very_command_of_costs(db_path, monkeypatch):
    """Кнопка это та же команда, а не вторая дорога рядом с ней."""
    called: list[dict] = []

    async def spy(update, context, *, path=None, **rest):
        called.append({"path": path})

    monkeypatch.setattr(costs_handler, "costs_command", spy)

    update, _, query = press(555010, menu.COSTS)
    await menu.pressed(update, None, path=db_path)

    assert query.answered
    assert called == [{"path": db_path}]
