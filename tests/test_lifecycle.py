"""Жизненный цикл: рассылки, настройки, продление, удаление данных.

Шов один и тот же - путь к базе, плюс подменённая «сегодня»: все сроки
считаются от переданного момента, а не от часов машины. Сами сроки берутся
из конфига (секция [access]), в тестах они названы числами из спецификации:
5 дней до конца, 3 дня льготных, 30 дней хранения, 3 дня до удаления.
"""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone

import pytest

from agents import lifecycle
from core import access, db


def utc(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)


@pytest.fixture
def client(db_path):
    return db.admin_repo(db_path).ensure_client(555001)


# --- тумблеры и время ---------------------------------------------------------


def test_by_default_both_mailings_are_on_and_time_is_from_config(db_path, client):
    prefs = lifecycle.prefs(client, path=db_path)
    assert prefs.daily is True
    assert prefs.weekly is True
    # 09:00 Europe/Moscow - значение из config.toml, не из кода.
    assert prefs.daily_at == time(9, 0)


def test_toggles_and_time_survive_and_keep_other_settings_keys(db_path, client):
    repo = db.admin_repo(db_path)
    repo.set_client_fields(
        client, settings=json.dumps({"token_reminders": [14]}, ensure_ascii=False)
    )

    lifecycle.set_daily(client, False, path=db_path)
    lifecycle.set_weekly(client, False, path=db_path)
    lifecycle.set_daily_time(client, "07:30", path=db_path)

    prefs = lifecycle.prefs(client, path=db_path)
    assert (prefs.daily, prefs.weekly) == (False, False)
    assert prefs.daily_at == time(7, 30)
    # Чужой ключ на месте: настройки пишутся поверх прочитанного словаря.
    stored = json.loads(repo.client(client)["settings"])
    assert stored["token_reminders"] == [14]


def test_unknown_time_is_refused(db_path, client):
    with pytest.raises(ValueError):
        lifecycle.set_daily_time(client, "четверть восьмого", path=db_path)


# --- рассылки -----------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_registry():
    from core import queue, scheduler

    from agents import finance

    queue.reset()
    scheduler.reset()
    lifecycle.set_notifier(None)
    finance.set_sender(None)
    yield
    queue.reset()
    scheduler.reset()
    lifecycle.set_notifier(None)
    finance.set_sender(None)


def connect(db_path, telegram_id: int) -> int:
    """Клиент с подключённым кабинетом. Токен тут нужен только как признак."""
    client_id = db.admin_repo(db_path).ensure_client(telegram_id)
    db.repo(client_id, db_path).insert("wb_tokens", ciphertext=b"x", exp=None)
    return client_id


def tasks(db_path, client_id, kind):
    return db.repo(client_id, db_path).rows("tasks", kind=kind)


class Task:
    def __init__(self, payload):
        self.client_id = None
        self.payload = payload
        self.kind = ""


def test_daily_report_goes_only_to_those_with_access_and_toggle_on(db_path):
    from agents import rnp

    on = connect(db_path, 777001)
    off = connect(db_path, 777002)
    no_access = connect(db_path, 777003)
    for client_id in (on, off, no_access):
        if client_id != no_access:
            access.grant_access(client_id, "rnp", 30, f"ref-{client_id}", path=db_path)
    lifecycle.set_daily(off, False, path=db_path)

    lifecycle.fan_out_daily(Task({"date": "2026-09-16"}), path=db_path)

    assert len(tasks(db_path, on, rnp.REPORT_ONE)) == 1
    assert tasks(db_path, off, rnp.REPORT_ONE) == []
    assert tasks(db_path, no_access, rnp.REPORT_ONE) == []


def test_switched_off_mailing_does_not_stop_collection(db_path):
    from agents import rnp

    client_id = connect(db_path, 777010)
    lifecycle.set_daily(client_id, False, path=db_path)
    lifecycle.set_weekly(client_id, False, path=db_path)

    # Сбор суточных данных остаётся за агентом и тумблеров не спрашивает.
    rnp.fan_out_collect(Task({"date": "2026-09-16"}), path=db_path)

    assert len(tasks(db_path, client_id, rnp.COLLECT_ONE)) == 1


def test_daily_report_is_queued_for_the_hour_the_client_picked(db_path):
    from agents import rnp

    client_id = connect(db_path, 777020)
    access.grant_access(client_id, "rnp", 30, "ref-time", path=db_path)
    lifecycle.set_daily_time(client_id, "11:00", path=db_path)

    lifecycle.fan_out_daily(Task({"date": "2026-09-16"}), path=db_path)

    row = tasks(db_path, client_id, rnp.REPORT_ONE)[0]
    # 11:00 по Москве это 08:00 UTC, а в очереди время хранится в UTC.
    assert str(row["next_run_at"]).startswith("2026-09-16 08:00")


@pytest.mark.asyncio
async def test_weekly_report_waits_for_a_new_wb_report_not_for_a_weekday(db_path):
    from agents import finance
    from core import scheduler

    client_id = connect(db_path, 777030)
    access.grant_access(client_id, "finance", 30, "ref-week", path=db_path)
    lifecycle.register_jobs(path=db_path)
    sent = []
    finance.set_sender(lambda cid, report, xlsx: sent.append((cid, report)))

    # Нового финотчёта нет - и недельная задача не ставится.
    assert scheduler.check_weekly(db_path) == []

    db.repo(client_id, db_path).insert(
        "fin_weeks", report_id=4242, date_from="2026-09-07", date_to="2026-09-13"
    )
    assert scheduler.check_weekly(db_path) != []

    assert len(tasks(db_path, client_id, lifecycle.WEEKLY_JOB)) == 1
    await lifecycle.weekly_job(Task({"report_id": 4242}), client_id=client_id, path=db_path)
    # Отдаётся уже собранное: повторной выгрузки из WB недельная рассылка
    # не заказывает, она самая дорогая в проекте.
    assert [cid for cid, _ in sent] == [client_id]
    assert tasks(db_path, client_id, finance.TASK_KIND) == []

    # Тот же отчёт второй раз не рассылается.
    assert scheduler.check_weekly(db_path) == []


@pytest.mark.asyncio
async def test_weekly_toggle_stops_delivery_but_not_the_report_itself(db_path):
    from agents import finance

    client_id = connect(db_path, 777040)
    access.grant_access(client_id, "finance", 30, "ref-week-off", path=db_path)
    lifecycle.set_weekly(client_id, False, path=db_path)
    sent = []
    finance.set_sender(lambda cid, report, xlsx: sent.append(cid))

    await lifecycle.weekly_job(Task({"report_id": 77}), client_id=client_id, path=db_path)

    assert sent == []


@pytest.mark.asyncio
async def test_finance_data_is_collected_by_schedule_only_for_those_with_the_module(db_path):
    from agents import finance

    paying = connect(db_path, 777050)
    silent = connect(db_path, 777051)
    access.grant_access(paying, "finance", 30, "ref-collect", path=db_path)
    # Тумблер гасит доставку, а не сбор: у платящего он выключен нарочно.
    lifecycle.set_weekly(paying, False, path=db_path)

    await lifecycle.fan_out_collect(Task({"date": "2026-09-16"}), path=db_path)

    assert tasks(db_path, paying, finance.COLLECT_KIND) != []
    assert tasks(db_path, silent, finance.COLLECT_KIND) == []


@pytest.mark.asyncio
async def test_history_for_the_baseline_is_raised_once_by_itself(db_path):
    import json as _json

    from agents import finance, watchdog

    bot = FakeBot()
    lifecycle.set_notifier(settings_handler.make_notifier(SimpleNamespace(bot=bot), path=db_path))

    client_id = connect(db_path, 777060)
    access.grant_access(client_id, "finance", 30, "ref-baseline", path=db_path)

    await lifecycle.fan_out_collect(Task({"date": "2026-09-16"}), path=db_path)

    periods = [
        _json.loads(row["payload"])["period"]
        for row in tasks(db_path, client_id, finance.COLLECT_KIND)
    ]
    # Неделя за сегодня и один подъём истории под базу сравнения сторожа.
    assert periods.count("week") == 1
    raised = [value for value in periods if value != "week"]
    assert len(raised) == 1
    # Период покрывает окно сравнения сторожа, а не выбран числом в коде.
    assert finance.PERIODS[raised[0]] >= watchdog.baseline_weeks() * 7
    # Клиенту сказали, что история подтягивается: пустой отчёт без объяснения
    # выглядит как сломанный сервис.
    assert len(bot.sent) == 1
    told = bot.sent[0][1].lower()
    assert "истор" in told and "займёт время" in told
    # Это сообщение про подъём истории, а не про удаление данных.
    assert "удал" not in told

    # Второй проход расписания истории не поднимает: лимит дорогой.
    await lifecycle.fan_out_collect(Task({"date": "2026-09-17"}), path=db_path)

    periods = [
        _json.loads(row["payload"])["period"]
        for row in tasks(db_path, client_id, finance.COLLECT_KIND)
    ]
    assert [value for value in periods if value != "week"] == raised
    assert len(bot.sent) == 1


@pytest.mark.asyncio
async def test_trial_access_raises_the_history_too(db_path):
    from agents import finance

    client_id = connect(db_path, 777070)
    db.admin_repo(db_path).set_client_fields(client_id, seller_id="seller-777070")
    access.start_trial(client_id, "finance", path=db_path)

    await lifecycle.fan_out_collect(Task({"date": "2026-09-16"}), path=db_path)

    assert lifecycle.needs_baseline(client_id, path=db_path) is False
    assert len(tasks(db_path, client_id, finance.COLLECT_KIND)) == 2


# --- продление, льготный период, выключение ------------------------------------


START = utc("2026-09-01 10:00")


def with_access(db_path, telegram_id: int, module: str = "rnp", days: int = 30) -> int:
    client_id = connect(db_path, telegram_id)
    access.grant_access(
        client_id, module, days, f"ref-{telegram_id}", now=START, path=db_path
    )
    return client_id


def kinds(events) -> list[str]:
    return [event.kind for event in events]


def test_renewal_offer_comes_five_days_before_the_end_and_only_once(db_path):
    client_id = with_access(db_path, 778001)
    # Доступ до 1 октября. За шесть дней молчим, за пять предлагаем продлить.
    assert lifecycle.check(client_id, now=utc("2026-09-25 09:00"), path=db_path) == []

    events = lifecycle.check(client_id, now=utc("2026-09-26 09:00"), path=db_path)
    assert kinds(events) == [lifecycle.RENEWAL]
    assert events[0].module == "rnp"

    lifecycle.remember(client_id, events[0], path=db_path)
    assert lifecycle.check(client_id, now=utc("2026-09-27 09:00"), path=db_path) == []


def test_renewal_offer_returns_after_the_access_is_prolonged(db_path):
    client_id = with_access(db_path, 778002)
    first = lifecycle.check(client_id, now=utc("2026-09-27 09:00"), path=db_path)[0]
    lifecycle.remember(client_id, first, path=db_path)

    access.grant_access(
        client_id, "rnp", 30, "ref-778002-again", now=utc("2026-09-27 10:00"), path=db_path
    )
    # Новый срок - новое предупреждение: отметка привязана к сроку, не к модулю.
    assert lifecycle.check(client_id, now=utc("2026-09-28 09:00"), path=db_path) == []
    later = lifecycle.check(client_id, now=utc("2026-10-27 09:00"), path=db_path)
    assert kinds(later) == [lifecycle.RENEWAL]


def test_grace_period_warns_but_keeps_the_module_working(db_path):
    client_id = with_access(db_path, 778003)
    moment = utc("2026-10-02 09:00")  # срок вышел 1 октября, идёт льготный день

    events = lifecycle.check(client_id, now=moment, path=db_path)
    assert kinds(events) == [lifecycle.GRACE]
    assert access.has_access(client_id, "rnp", now=moment, path=db_path) is True


def test_after_three_grace_days_the_module_is_switched_off_with_a_notice(db_path):
    client_id = with_access(db_path, 778004)
    moment = utc("2026-10-05 09:00")  # 1 октября + 3 льготных дня уже позади

    events = lifecycle.check(client_id, now=moment, path=db_path)
    assert lifecycle.SHUTDOWN in kinds(events)
    assert access.has_access(client_id, "rnp", now=moment, path=db_path) is False


def test_retention_warning_comes_three_days_before_deletion(db_path):
    client_id = with_access(db_path, 778005)
    # Модули выключились 4 октября (1 октября + 3 льготных). Данные живут
    # 30 дней, то есть до 3 ноября; предупреждение за 3 дня - 31 октября.
    for event in lifecycle.check(client_id, now=utc("2026-10-05 09:00"), path=db_path):
        lifecycle.remember(client_id, event, path=db_path)

    assert lifecycle.check(client_id, now=utc("2026-10-30 09:00"), path=db_path) == []

    warned = utc("2026-11-01 09:00")
    events = lifecycle.check(client_id, now=warned, path=db_path)
    assert kinds(events) == [lifecycle.RETENTION]

    # Предупредили в срок - удалили в срок, через retention_notice_days.
    lifecycle.remember(client_id, events[0], now=warned, path=db_path)
    assert lifecycle.check(client_id, now=utc("2026-11-03 09:00"), path=db_path) == []
    assert kinds(lifecycle.check(client_id, now=utc("2026-11-04 09:00"), path=db_path)) == [
        lifecycle.DELETED
    ]


def test_deletion_after_thirty_days_leaves_nothing_but_an_anonymous_log(db_path):
    from core import audit

    client_id = with_access(db_path, 778006)
    repo = db.repo(client_id, db_path)
    repo.insert("costs", nm_id=1, cost_per_unit_kop=1000)
    repo.insert("plans", year_month="2026-09", revenue_target_kop=100)
    repo.insert("nm_daily", date="2026-09-10", nm_id=1)
    repo.insert("fin_weeks", report_id=1, date_from="2026-09-01", date_to="2026-09-07")

    # Предупреждения не было: бот молчал, и на 35-й день данные не стираются,
    # а уходит предупреждение. Удаление ждёт retention_notice_days после него.
    late = utc("2026-11-05 09:00")
    events = lifecycle.check(client_id, now=late, path=db_path)
    # Про выключение модуля клиенту тоже ещё не говорили: бот молчал.
    assert kinds(events) == [lifecycle.SHUTDOWN, lifecycle.RETENTION]
    for event in events:
        lifecycle.remember(client_id, event, now=late, path=db_path)

    assert lifecycle.check(client_id, now=utc("2026-11-07 09:00"), path=db_path) == []

    events = lifecycle.check(client_id, now=utc("2026-11-08 09:00"), path=db_path)
    assert kinds(events) == [lifecycle.DELETED]

    lifecycle.erase(client_id, path=db_path)

    # Перебором по всем клиентским таблицам, а не по одной выбранной.
    left = {
        table: repo.count(table)
        for table in sorted(db.CLIENT_TABLES)
        if repo.count(table)
    }
    assert left == {}
    assert db.admin_repo(db_path).client(client_id) is None
    # Журнал остаётся, но обезличенный: клиента в нём нет.
    logged = [
        row
        for row in audit.recent(limit=50, path=db_path)
        if row["kind"] == lifecycle.DELETED
    ]
    assert logged and all(row["client_id"] is None for row in logged)


# --- /settings и сообщения клиенту --------------------------------------------


from types import SimpleNamespace  # noqa: E402

from bot.handlers import settings as settings_handler  # noqa: E402
from bot.handlers import tariffs  # noqa: E402


class FakeMessage:
    def __init__(self):
        self.sent = []

    async def reply_text(self, text, **kwargs):
        self.sent.append((text, kwargs))
        return self

    async def edit_text(self, text, **kwargs):
        self.sent.append((text, kwargs))
        return self


class FakeQuery:
    def __init__(self, data, message):
        self.data = data
        self.message = message

    async def answer(self, *args, **kwargs):
        return None

    async def edit_message_text(self, text, **kwargs):
        self.message.sent.append((text, kwargs))


class FakeUpdate:
    def __init__(self, telegram_id, message, query=None):
        self.effective_user = SimpleNamespace(id=telegram_id)
        self.effective_message = message
        self.callback_query = query


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))


@pytest.mark.asyncio
async def test_settings_shows_both_toggles_and_the_time(db_path):
    client_id = connect(db_path, 779001)
    message = FakeMessage()

    await settings_handler.settings_command(
        FakeUpdate(779001, message), None, path=db_path
    )

    text, kwargs = message.sent[0]
    assert "Ежедневный" in text and "Недельный" in text and "09:00" in text
    buttons = [
        button.callback_data
        for row in kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert f"{settings_handler.PREFIX}daily" in buttons
    assert f"{settings_handler.PREFIX}weekly" in buttons
    # Целевого ДРР в настройках нет: он относится к модулю ads, этап 3.
    assert "ДРР" not in text


@pytest.mark.asyncio
async def test_toggle_button_switches_the_mailing_off(db_path):
    client_id = connect(db_path, 779002)
    message = FakeMessage()
    query = FakeQuery(f"{settings_handler.PREFIX}daily", message)

    await settings_handler.toggle_callback(
        FakeUpdate(779002, message, query), None, path=db_path
    )

    assert lifecycle.daily_enabled(client_id, path=db_path) is False


@pytest.mark.asyncio
async def test_time_button_saves_the_hour_the_client_picked(db_path):
    client_id = connect(db_path, 779003)
    message = FakeMessage()
    query = FakeQuery(f"{settings_handler.PREFIX}at:08:00", message)

    await settings_handler.toggle_callback(
        FakeUpdate(779003, message, query), None, path=db_path
    )

    assert lifecycle.prefs(client_id, path=db_path).daily_at == time(8, 0)


def test_renewal_notice_offers_an_invoice_through_the_existing_dialog():
    event = lifecycle.Event(lifecycle.RENEWAL, 1, "rnp", 5, utc("2026-10-01 10:00"))
    text, markup = settings_handler.notice(event)

    assert "5" in text
    button = markup.inline_keyboard[0][0]
    assert "счёт" in button.text.lower()
    # Кнопка ведёт в диалог покупки таска 07, своего диалога здесь нет.
    assert button.callback_data == f"{tariffs.BUY_PREFIX}rnp"


def test_retention_notice_says_how_to_keep_the_data():
    event = lifecycle.Event(lifecycle.RETENTION, 1, "", 3, utc("2026-11-03 10:00"))
    text, markup = settings_handler.notice(event)

    assert "3" in text and "удал" in text.lower()
    assert markup is not None
    # Длинных тире в текстах бота нет; сам символ пишем кодом, чтобы не
    # уронить общий тест, который проходит и по тестам тоже.
    assert chr(8212) not in text


@pytest.mark.asyncio
async def test_daily_job_sends_the_warning_once_and_deletes_when_the_time_comes(db_path):
    bot = FakeBot()
    app = SimpleNamespace(bot=bot)
    lifecycle.set_notifier(settings_handler.make_notifier(app, path=db_path))

    client_id = with_access(db_path, 779010)
    await lifecycle.daily_job(now=utc("2026-09-27 09:00"), path=db_path)
    assert len(bot.sent) == 1
    assert bot.sent[0][0] == 779010

    # Второй день подряд то же самое не повторяется.
    await lifecycle.daily_job(now=utc("2026-09-28 09:00"), path=db_path)
    assert len(bot.sent) == 1

    # Срок хранения вышел, но предупреждения не было: сначала предупреждаем.
    await lifecycle.daily_job(now=utc("2026-11-05 09:00"), path=db_path)
    assert db.admin_repo(db_path).client(client_id) is not None
    assert any("удал" in text.lower() for _, text, _ in bot.sent)

    # И только потом, через retention_notice_days, стираем.
    await lifecycle.daily_job(now=utc("2026-11-08 09:00"), path=db_path)
    assert db.admin_repo(db_path).client(client_id) is None


def test_all_deadlines_come_from_config_not_from_code(db_path, monkeypatch):
    from core import config

    client_id = with_access(db_path, 779020)
    # Другие сроки в конфиге - другие границы. Если бы числа жили в коде,
    # предупреждение пришло бы в тот же день, что и с настройками по умолчанию.
    monkeypatch.setattr(
        config,
        "settings",
        lambda: {
            "access": {
                "grace_days": 0,
                "renewal_notice_days": 10,
                "retention_days": 5,
                "retention_notice_days": 1,
            }
        },
    )
    assert lifecycle.renewal_notice_days() == 10
    assert lifecycle.retention_days() == 5

    # За 10 дней до конца, а не за 5.
    assert kinds(lifecycle.check(client_id, now=utc("2026-09-21 09:00"), path=db_path)) == [
        lifecycle.RENEWAL
    ]
    # Льготных дней нет, хранение 5 дней: 6 октября предупреждать, и удалять
    # через retention_notice_days после предупреждения, то есть 7 октября.
    warned = utc("2026-10-06 09:00")
    events = lifecycle.check(client_id, now=warned, path=db_path)
    assert lifecycle.RETENTION in kinds(events)
    for event in events:
        lifecycle.remember(client_id, event, now=warned, path=db_path)
    assert kinds(lifecycle.check(client_id, now=utc("2026-10-07 09:00"), path=db_path)) == [
        lifecycle.DELETED
    ]


def test_only_one_morning_dispatch_is_registered_and_it_is_the_lifecycle_one(db_path):
    from agents import rnp
    from core import scheduler

    # Агент ставит свои работы сам, жизненный цикл свои: ни один не
    # регистрирует чужие, и утренняя рассылка в расписании ровно одна.
    rnp.register_jobs()
    lifecycle.register_jobs(path=db_path)

    names = set(scheduler.daily_names())
    assert lifecycle.REPORTS_JOB in names
    assert lifecycle.DAILY_JOB in names
    assert lifecycle.COLLECT_JOB in names
    assert rnp.COLLECT_ALL in names
    assert rnp.REPORT_ALL not in names


def test_the_weekly_probe_is_registered_once_and_by_the_lifecycle(db_path, monkeypatch):
    from agents import finance, rnp, watchdog
    from core import scheduler

    # Проба у планировщика одна на всех. Если её ставят двое, побеждает тот,
    # кто зарегистрировался позже, а порядок зависит от имён файлов в реестре
    # хендлеров: недельная рассылка сломается молча.
    put = []
    real = scheduler.set_report_probe

    def spy(fn):
        put.append(fn)
        real(fn)

    monkeypatch.setattr(scheduler, "set_report_probe", spy)

    rnp.register_jobs()
    finance.register_jobs()
    watchdog.register_jobs(path=db_path)
    lifecycle.register_jobs(path=db_path)

    assert len(put) == 1
    assert getattr(put[0], "func", put[0]) is lifecycle.new_report_of

    # И она отвечает на вопрос, ради которого стоит: появился новый отчёт.
    client_id = connect(db_path, 777080)
    assert lifecycle.new_report_of(client_id, path=db_path) is None
    db.repo(client_id, db_path).insert(
        "fin_weeks", report_id=7, date_from="2026-09-07", date_to="2026-09-13"
    )
    assert lifecycle.new_report_of(client_id, path=db_path) == 7
