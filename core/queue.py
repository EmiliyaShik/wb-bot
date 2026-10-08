"""Очередь фоновых задач.

Тяжёлая работа не делается в хендлере: она кладётся в таблицу `tasks`,
клиент сразу получает «принято», а результат приходит отдельно. Очередь
лежит в базе, а не в памяти, поэтому перезапуск процесса ничего не теряет.

Здесь только механика. Сами задачи (выгрузка отчёта, суточные данные,
рассылки) регистрируют их владельцы через `register(kind, fn)`.

Длинная работа одного клиента не держит остальных. Отчёт по рекламе за
квартал это десятки запросов к Wildberries по дорожке «3 в минуту», то есть
около десяти минут одной задачи, и почти всё это время она ждёт разрешения
ограничителя частоты, а не считает. Поэтому воркер ведёт несколько работ
одновременно (`[queue] workers`), но тремя правилами: у одного клиента в
работе всегда не больше одной задачи, часть мест закреплена за работами
расписания (`[queue] client_slots`), и следующей берётся задача свободного
клиента, а не следующая по номеру. Потоков здесь нет и не будет: соединение
с базой одно на процесс и настоящих потоков не переживает, а корутина
прерваться посреди запроса к базе не может, поэтому выборка «взял и отметил
в работе» остаётся неделимой без всякого замка.

Сорвавшаяся задача не молчит, и это правило очереди, а не каждого агента.
Кому сказать, решает вид работы: заказанную клиентом объясняем клиенту его
словами (`title=` при регистрации), незаказанную (`quiet=True`: ночные
сборщики, служебные проверки, работы расписания) относим владельцу. Подряд
идущие сбои сливаются в одно сообщение, пауза в `[queue]` конфига, а полный
текст ошибки Wildberries в любом случае остаётся в журнале и никогда не
уезжает селлеру: он чужой, английский и объясняет не больше, чем пугает.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from core import audit, config, db
from core.wbapi import (
    WBAuthError,
    WBForbiddenError,
    WBRateLimited,
    WBTokenMissing,
    WBUnavailable,
)

logger = logging.getLogger(__name__)

# Состояния задачи. `queued` это «ждёт своей очереди»: так называет его
# слой данных, и по нему же отбирает `AdminRepo.due_tasks`.
PENDING = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


@dataclass(frozen=True)
class Task:
    """Задача такой, какой её видит обработчик."""

    id: int
    client_id: int | None
    kind: str
    payload: dict[str, Any]
    attempts: int


class TaskId(int):
    """Номер задачи и ответ на вопрос, новая она или нашлась прежняя.

    Это по-прежнему обычное целое: старые вызовы `enqueue` продолжают
    работать без единой правки, а тому, кто хочет отличить повтор от новой
    работы, доступно поле `created`. Отдельный тип понадобился именно
    потому, что вызывающих у `enqueue` много и все они лежат в чужих файлах.
    """

    def __new__(cls, value: int, *, created: bool = True) -> "TaskId":
        self = super().__new__(cls, int(value))
        self.created = bool(created)
        return self


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _stamp(moment: datetime) -> str:
    """Время в том же виде, в каком его пишет база: UTC, без часового пояса."""
    return moment.strftime(TIME_FORMAT)


def enqueue(
    client_id: int | None,
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    run_at: datetime | None = None,
    notify: bool = True,
    path: str | Path | None = None,
) -> TaskId:
    """Ставит задачу в очередь и сразу возвращает управление.

    Клиенту тут же уходит «принято, пришлю, когда будет готово»: ждать
    этого обещания от каждого хендлера нельзя, его даёт сама очередь.
    `notify=False` для работ, о которых клиенту знать не надо: расписание,
    служебные задачи, повторная постановка.

    Повтор не удваивает работу. Пока прежняя такая же задача не доделана,
    вторая не создаётся: возвращается номер прежней, а `created` у ответа
    False. Клиенту при этом говорится «уже считаю», а не «принято» второй
    раз. «Такая же» это тот же клиент, тот же вид и тот же payload: отчёт за
    другой период это другая работа, а второй сбор за тот же день нет.

    Одинаковость проверяет база уникальным индексом, а не запрос перед
    вставкой: хендлеры выполняются одновременно, и два нажатия подряд оба
    увидели бы пустую очередь.
    """
    body = json.dumps(payload or {}, ensure_ascii=False)
    next_run_at = _stamp(run_at) if run_at is not None else None
    # Три попытки, а не одна: между отказом вставки и поиском близнеца воркер
    # мог успеть его доделать, и тогда место в очереди снова свободно.
    for _ in range(3):
        task_id = _insert_once(client_id, kind, body, next_run_at, path)
        if task_id is not None:
            if notify:
                _notify_now(client_id, ACCEPTED)
            return TaskId(task_id, created=True)
        twin = _twin(client_id, kind, body, path)
        if twin is not None:
            if notify:
                _notify_now(client_id, ALREADY_QUEUED)
            return TaskId(twin, created=False)
    # Сюда можно попасть, только если воркер трижды подряд успел закрыть
    # близнеца между вставкой и поиском. Работу в таком случае ставим обычной
    # вставкой: остаться без отчёта хуже, чем поставить лишнюю задачу.
    logger.warning("задача %s клиента %s ставится в обход проверки", kind, client_id)
    task_id = _insert(client_id, kind, body, next_run_at, path)
    if notify:
        _notify_now(client_id, ACCEPTED)
    return TaskId(task_id, created=True)


def _insert(
    client_id: int | None,
    kind: str,
    body: str,
    next_run_at: str | None,
    path: str | Path | None,
) -> int:
    if client_id is None:
        return db.admin_repo(path).add_task(kind, body, next_run_at)
    return db.repo(client_id, path).insert(
        "tasks", kind=kind, payload=body, state=PENDING, next_run_at=next_run_at
    )


def _insert_once(
    client_id: int | None,
    kind: str,
    body: str,
    next_run_at: str | None,
    path: str | Path | None,
) -> int | None:
    """Вставка, которая молчит, если такая задача уже стоит. None это повтор."""
    if client_id is None:
        return db.admin_repo(path).add_task_once(kind, body, next_run_at)
    return db.repo(client_id, path).insert_once(
        "tasks", kind=kind, payload=body, state=PENDING, next_run_at=next_run_at
    )


def _twin(
    client_id: int | None, kind: str, body: str, path: str | Path | None
) -> int | None:
    """Номер такой же незавершённой задачи, если она есть.

    Читается только после того, как вставку отклонил индекс, и нужен ровно
    затем, чтобы ответить вызывающему номером прежней работы.
    """
    if client_id is None:
        rows = [
            row
            for row in db.admin_repo(path).tasks_by_kind(kind)
            if row["client_id"] is None
        ]
    else:
        rows = db.repo(client_id, path).rows("tasks", kind=kind)
    for row in rows:
        if row["payload"] == body and row["state"] in (PENDING, RUNNING):
            return int(row["id"])
    return None


# --- реестр обработчиков ---

Handler = Callable[[Task], Any]

_handlers: dict[str, Handler] = {}

# Как работа называется по-человечески: «шаблон себестоимости», а не
# `costs_template`. Нужно ровно в одном месте, в сообщении о сбое: человек
# нажал кнопку и должен узнать в ответе то, что нажимал.
_titles: dict[str, str] = {}

# Виды работ, которых клиент не заказывал: ночные сборщики и служебные
# проверки. Про их сбой клиенту сказать нечего, а владельцу есть.
_quiet: set[str] = set()


def register(kind: str, fn: Handler, *, title: str = "", quiet: bool = False) -> None:
    """Связывает вид задачи с обработчиком. Зовут владельцы самих задач.

    `title` это название работы для селлера в винительном падеже: оно встанет
    в «не получилось собрать ...». Пусто - скажем общими словами.

    `quiet=True` означает «клиент эту работу не заказывал»: сбор данных ночью,
    проверка срока токена, веерные работы расписания. Клиенту о таком сбое не
    пишем (он ничего не ждал и не поймёт, о чём речь), владельцу пишем.
    Свойство висит на виде работы, а не на отдельной задаче: `notify=False` у
    `enqueue` говорит только про обещание «принято» и у одного вида задач
    бывает и так, и так (суточный план-факт ставит и расписание, и сам
    клиент командой), а вот сборщик остаётся сборщиком при любом запуске.
    """
    _handlers[kind] = fn
    if title:
        _titles[kind] = title
    if quiet:
        _quiet.add(kind)
    else:
        _quiet.discard(kind)


def handlers() -> dict[str, Handler]:
    return dict(_handlers)


def title_of(kind: str) -> str:
    """Человеческое имя работы или пустая строка, если его не дали."""
    return _titles.get(kind, "")


def is_quiet(kind: str) -> bool:
    """Заказывал ли эту работу клиент. Незнакомый вид считаем заказанным.

    Незнакомый это чаще всего обработчик, который забыли зарегистрировать:
    клиент как раз нажал кнопку и ждёт. Промолчать тут хуже, чем сказать
    лишнее.
    """
    return kind in _quiet


def reset() -> None:
    """Очищает реестр и обработчики оповещений. Нужно тестам."""
    global _notifier, _auth_handler
    _handlers.clear()
    _titles.clear()
    _quiet.clear()
    _said.clear()
    _busy.clear()
    _notifier = None
    _auth_handler = None


def _payload(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _task_from_row(row: Any) -> Task:
    return Task(
        id=int(row["id"]),
        client_id=row["client_id"],
        kind=row["kind"],
        payload=_payload(row["payload"]),
        attempts=int(row["attempts"] or 0),
    )


def recover(path: str | Path | None = None) -> int:
    """Возвращает в очередь задачи, застрявшие в `running` после рестарта."""
    repo = db.admin_repo(path)
    stuck = repo.tasks(state=RUNNING, limit=1000)
    for row in stuck:
        repo.update_task(int(row["id"]), state=PENDING)
    return len(stuck)


async def _call(fn: Handler, task: Task) -> None:
    result = fn(task)
    if hasattr(result, "__await__"):
        await result


# Клиенты, у которых работа идёт прямо сейчас. Ключ это `client_id` задачи,
# и `None` в нём это тоже ключ: работы расписания и служебные проверки делят
# одного исполнителя. Память здесь, а не в базе: состояние `running` в таблице
# и так есть, а после перезапуска его подбирает `recover`.
#
# Больше одной задачи одного клиента одновременно не идёт, и это не только
# справедливость. Корзина лимита Wildberries своя на клиента и общая на все
# его задачи: две работы одного кабинета сразу выбрали бы её вдвоём и довели
# бы дело до блокировки токена.
_busy: set[Any] = set()


def _take(
    path: str | Path | None = None, *, kinds: Any = None
) -> Task | None:
    """Берёт следующую задачу свободного клиента и отмечает её в работе.

    Синхронная целиком, и это обязательное свойство: между выборкой и
    отметкой `running` нет ни одного `await`, поэтому второй исполнитель не
    может взять ту же задачу. Замка для этого не нужно ровно потому, что
    исполнители это корутины одного потока, а не потоки.
    """
    repo = db.admin_repo(path)
    rows = repo.due_tasks(
        _stamp(_now()), limit=1, skip_clients=tuple(_busy), kinds=kinds
    )
    if not rows:
        return None
    task = _task_from_row(rows[0])
    repo.update_task(task.id, state=RUNNING)
    _busy.add(task.client_id)
    return task


async def run_once(path: str | Path | None = None) -> bool:
    """Берёт одну задачу, которой пора, и выполняет её.

    Возвращает False, если очередь пуста. Одна задача за вызов: так эту
    функцию зовут тесты и так же работает воркер, когда место свободно одно.
    """
    task = _take(path)
    if task is None:
        return False
    try:
        await _execute(task, path)
    finally:
        _busy.discard(task.client_id)
    return True


async def _execute(task: Task, path: str | Path | None = None) -> None:
    """Выполняет взятую задачу и закрывает её в таблице. Не бросает наружу."""
    repo = db.admin_repo(path)
    fn = _handlers.get(task.kind)
    if fn is None:
        repo.update_task(
            task.id,
            state=FAILED,
            last_error=f"нет обработчика для задачи {task.kind}",
            finished_at=_stamp(_now()),
        )
        # Раньше это была самая тихая поломка из всех: задача умирала без
        # записи в журнале и без слова клиенту, а снаружи выглядела как
        # неработающая кнопка.
        audit.log(
            "queue",
            None,
            f"задача {task.kind} #{task.id} клиента {task.client_id} брошена:"
            " обработчик не зарегистрирован",
            level="error",
            path=path,
        )
        await _tell_about_failure(task, path)
        return

    try:
        await _call(fn, task)
    except WBTokenMissing as error:
        # Кабинет не подключён вовсе. Это не «Wildberries отверг ключ»: ставить
        # модули на паузу тут нечем и незачем, а пугать селлера рассказом про
        # непринятый ключ значит объяснять ему чужую беду вместо его.
        await _cancel(repo, task, error, path, token=False, missing=True)
    except WBAuthError as error:
        await _cancel(repo, task, error, path, token=True)
    except WBForbiddenError as error:
        await _cancel(repo, task, error, path, token=False)
    except (WBRateLimited, WBUnavailable) as error:
        await _retry_or_give_up(repo, task, error, path)
    except Exception as error:  # noqa: BLE001 - воркер не имеет права упасть
        repo.update_task(
            task.id,
            state=FAILED,
            attempts=task.attempts + 1,
            last_error=str(error),
            finished_at=_stamp(_now()),
        )
        audit.log(
            "queue",
            None,
            f"задача {task.kind} #{task.id} клиента {task.client_id} сорвалась: {error}",
            level="error",
            path=path,
        )
        await _tell_about_failure(task, path)
    else:
        repo.update_task(task.id, state=DONE, finished_at=_stamp(_now()))


async def _cancel(
    repo: Any,
    task: Task,
    error: BaseException,
    path: Any,
    *,
    token: bool,
    missing: bool = False,
) -> None:
    """Повторять бессмысленно: дело в ключе, а не в доступности WB."""
    repo.update_task(
        task.id, state=CANCELLED, last_error=str(error), finished_at=_stamp(_now())
    )
    if missing:
        audit.log(
            "queue",
            task.client_id,
            f"задача {task.kind} отменена: кабинет Wildberries не подключён",
            level="warning",
            path=path,
        )
        await _notify(task.client_id, NEED_CONNECT)
        return
    if token:
        audit.log(
            "queue",
            task.client_id,
            f"задача {task.kind} отменена: Wildberries не принял ключ доступа",
            level="warning",
            path=path,
        )
        # Про токен говорит тот, кто умеет ставить модули на паузу.
        # Если обработчика нет, клиент всё равно не остаётся в неведении.
        if not await _safe_call(_auth_handler, task, error):
            await _notify(task.client_id, TOKEN_TEXT)
        return

    category = str(getattr(error, "category", "") or "")
    audit.log(
        "queue",
        task.client_id,
        f"задача {task.kind} отменена: ключу не хватает категории {category or 'доступа'}",
        level="warning",
        path=path,
    )
    await _notify(task.client_id, forbidden_text(category))


async def _retry_or_give_up(repo: Any, task: Task, error: BaseException, path: Any) -> None:
    """WB не ответил: повторить с растущей паузой или сдаться понятным текстом."""
    attempts = task.attempts + 1
    limit = max_attempts()
    if attempts >= limit:
        repo.update_task(
            task.id,
            state=FAILED,
            attempts=attempts,
            last_error=str(error),
            finished_at=_stamp(_now()),
        )
        audit.log(
            "queue",
            None,
            f"задача {task.kind} #{task.id} клиента {task.client_id} остановлена"
            f" после {attempts} попыток: {error}",
            level="error",
            path=path,
        )
        await _tell_about_failure(task, path, waiting=True)
        return

    # Паузу после 429 называет сам WB: это поле retry_after его ошибки.
    pause = error.retry_after if isinstance(error, WBRateLimited) else None
    delay = float(pause) if pause else _backoff(attempts)
    repo.update_task(
        task.id,
        state=PENDING,
        attempts=attempts,
        last_error=str(error),
        next_run_at=_stamp(_now() + timedelta(seconds=delay)),
    )


async def _run_in_slot(task: Task, path: str | Path | None) -> None:
    """Одна работа на своём месте. Место освобождается в любом случае."""
    try:
        await _execute(task, path)
    except asyncio.CancelledError:
        # Бота гасят. Задача останется в `running`, и после перезапуска её
        # вернёт в очередь `recover`: это и есть её страховка.
        raise
    except Exception:  # noqa: BLE001 - воркер не имеет права упасть
        logger.exception("сбой задачи %s #%s", task.kind, task.id)
    finally:
        _busy.discard(task.client_id)


def _start_available(
    live: dict[Any, bool], slots: int, client_slots_count: int, path: Any
) -> int:
    """Занимает свободные места задачами. Возвращает, сколько работ началось.

    Места двух видов. Общее число работ ограничено `slots`, а заказанные
    клиентами отчёты занимают не больше `client_slots_count` из них: остаток
    закреплён за работами расписания. Иначе три заказанных квартала заняли бы
    всё, а ночной сбор воронки сдвинулся бы на эти минуты, и его потерянные
    сутки не вернуть: WB отдаёт воронку по дням только за последнюю неделю.
    """
    started = 0
    while len(live) < slots:
        ordered = sum(1 for client_task in live.values() if client_task)
        if ordered >= client_slots_count:
            # Клиентские места кончились: берём только то, что клиент не
            # заказывал, то есть сборщики и служебные проверки.
            task = _take(path, kinds=sorted(_quiet))
        else:
            task = _take(path)
        if task is None:
            return started
        live[asyncio.ensure_future(_run_in_slot(task, path))] = not is_quiet(task.kind)
        started += 1
    return started


async def run_worker(
    *,
    path: str | Path | None = None,
    poll_sec: float = 5.0,
    stop: asyncio.Event | None = None,
    workers: int | None = None,
) -> None:
    """Вечный цикл: подобрать брошенное после рестарта и разбирать очередь.

    Работ одновременно не больше, чем мест (`[queue] workers`), у одного
    клиента в работе всегда одна, и часть мест закреплена за работами
    расписания. `workers` в параметрах нужен тестам и владельцу скрипта:
    в работе число берётся из конфига.
    """
    recover(path)
    slots = max(1, int(workers) if workers is not None else worker_slots())
    ordered_slots = max(1, min(slots, client_slots()))
    live: dict[Any, bool] = {}
    try:
        while stop is None or not stop.is_set():
            try:
                _start_available(live, slots, ordered_slots, path)
            except Exception:  # noqa: BLE001 - воркер не имеет права упасть
                logger.exception("сбой цикла очереди")
            if live:
                done, _ = await asyncio.wait(
                    set(live), timeout=poll_sec, return_when=asyncio.FIRST_COMPLETED
                )
                for finished in done:
                    live.pop(finished, None)
                continue
            if stop is None:
                await asyncio.sleep(poll_sec)
                continue
            try:
                await asyncio.wait_for(stop.wait(), timeout=poll_sec)
            except asyncio.TimeoutError:
                pass
        # Выключение не рвёт начатое: новых задач не берём, а те, что идут,
        # доводим до конца. Ждать до бесконечности не придётся, выключение
        # само снимет воркер по своему сроку.
        if live:
            await asyncio.wait(set(live))
    except asyncio.CancelledError:
        for child in live:
            child.cancel()
        raise


# --- тексты клиенту. Русский, без кодов ошибок и слова «таймаут» ---

ACCEPTED = "Принято. Соберу и пришлю результат отдельным сообщением."

ALREADY_QUEUED = (
    "Эта работа уже идёт, повторно запускать её не нужно. "
    "Результат пришлю отдельным сообщением, как только он будет готов."
)

FAILED_TEXT = (
    "Не получилось собрать данные: Wildberries сейчас не отвечает. "
    "Я повторил несколько раз и остановился. Попробуйте позже, "
    "владелец бота уже видит эту заминку в журнале."
)

GENERIC_FAILED_TEXT = (
    "Не получилось выполнить эту работу. Запись об этом уже у владельца бота, "
    "он разберётся. Попробуйте повторить позже."
)

# Сообщение о сорвавшейся работе. Кода ошибки и текста Wildberries тут нет
# намеренно: они чужие, они по-английски и селлеру с телефона не говорят
# ничего. Всё это лежит в журнале, и владельцу видно в /tasks.
FAILED_WORK = (
    "Не получилось собрать {work}. Это не ваша ошибка: сбой на моей стороне, "
    "запись о нём уже у владельца бота. Попробуйте ещё раз попозже, а если "
    "снова не выйдет, напишите владельцу командой /paysupport."
)

UNAVAILABLE_WORK = (
    "Не получилось собрать {work}: Wildberries не отвечает. Я повторил "
    "несколько раз и остановился, чтобы не мучить ваш кабинет. Попробуйте "
    "позже, владелец бота уже видит эту заминку в журнале."
)


def failed_text(work: str = "") -> str:
    """Работа сорвалась и повторять её бессмысленно. Клиенту, словами."""
    return FAILED_WORK.format(work=work) if work else GENERIC_FAILED_TEXT


def unavailable_text(work: str = "") -> str:
    """Wildberries не ответил и попытки кончились. Клиенту, словами."""
    return UNAVAILABLE_WORK.format(work=work) if work else FAILED_TEXT


# Владельцу. Здесь можно называть вещи своими именами: это его бот. Но и тут
# текст ошибки Wildberries не пересказывается, он чужой и место ему в журнале.
OWNER_FAILED = (
    "Фоновая работа не выполнилась: {kind}, задача #{task}{whose}. "
    "Клиенту я об этом не писал: он эту работу не заказывал. "
    "Подробности в журнале, команда /tasks."
)

# Клиент получил слово о сбое совсем недавно. Второе такое же сообщение это
# не забота, а наказание: молчим и оставляем запись в журнале.
HUSHED = "о сбое задачи {kind} #{task} клиенту {client} промолчал: недавно уже писал"

# Кабинета нет вовсе. Прежде такая задача отвечала текстом про непринятый
# ключ и заодно ставила модули на паузу: клиент, которому модуль выдали
# вручную, получал испуг вместо следующего шага.
NEED_CONNECT = (
    "Чтобы собрать этот разбор, нужен доступ к вашему кабинету Wildberries. "
    "Выпустите ключ доступа в личном кабинете и пришлите его командой "
    "/connect: дальше бот всё соберёт сам."
)

TOKEN_TEXT = (
    "Wildberries больше не принимает ваш ключ доступа. Выпустите новый "
    "в личном кабинете и пришлите его командой /connect. Пока ключ не обновлён, "
    "разборы по вашим продажам не собираются."
)


def forbidden_text(category: str = "") -> str:
    """Что сказать клиенту, когда у ключа не хватает категории доступа."""
    if category:
        return (
            f"Для этой работы ключу Wildberries не хватает категории «{category}». "
            "Выпустите ключ заново и отметьте её, тогда всё заработает."
        )
    return (
        "Ключу Wildberries не хватает прав на эти данные. Выпустите ключ заново "
        "и отметьте все категории, которые просит бот."
    )


# --- оповещения. Очередь не знает про телеграм, ей дают обработчики ---

_notifier: Callable[[int | None, str], Any] | None = None
_auth_handler: Callable[[Task, BaseException], Any] | None = None


def set_notifier(fn: Callable[[int | None, str], Any] | None) -> None:
    """Чем сообщать клиенту. Ставит поверхность бота при старте."""
    global _notifier
    _notifier = fn


def set_auth_handler(fn: Callable[[Task, BaseException], Any] | None) -> None:
    """Что делать, когда WB отклонил ключ: снять модули с паузы и написать."""
    global _auth_handler
    _auth_handler = fn


async def _safe_call(fn: Callable[..., Any] | None, *args: Any) -> bool:
    if fn is None:
        return False
    try:
        result = fn(*args)
        if hasattr(result, "__await__"):
            await result
    except Exception:  # noqa: BLE001 - оповещение не роняет воркер
        logger.exception("не удалось выполнить обработчик очереди")
    return True


async def _notify(client_id: int | None, text: str) -> None:
    if client_id is None:
        return
    await _safe_call(_notifier, client_id, text)


async def _notify_owner(text: str) -> None:
    """Слово владельцу. Оно же уходит по `client_id = None` в поверхности бота."""
    await _safe_call(_notifier, None, text)


# Когда кому в последний раз говорили о сбое. Ключ это получатель: номер
# клиента или None у владельца. Память здесь, а не в базе, и это осознанно:
# новой колонки схема не заводит, а перезапуск бота и так означает, что поток
# сообщений прервался. Худшее, что даёт потеря памяти, это одно лишнее письмо.
_said: dict[Any, datetime] = {}


def _too_soon(whom: Any) -> bool:
    """Говорили ли этому получателю о сбое только что."""
    gap = notice_gap_sec()
    if gap <= 0:
        return False
    was = _said.get(whom)
    now = _now()
    if was is not None and (now - was).total_seconds() < gap:
        return True
    _said[whom] = now
    return False


async def _tell_about_failure(
    task: Task, path: Any = None, *, waiting: bool = False
) -> None:
    """Сорвавшаяся задача больше не молчит.

    Кому говорить, решает вид работы, а не то, обещали ли «принято»: работу,
    которую клиент не заказывал, обсуждать с ним нечего, а владелец должен
    знать, что ночью что-то не собралось.

    Поток сообщений тоже не забота: десять подряд сорвавшихся задач дают одно
    сообщение, остальные остаются в журнале. Пауза в `[queue]` конфига.
    """
    # Работа без клиента это тоже работа, которую никто не заказывал: сказать
    # о ней некому, кроме владельца.
    if is_quiet(task.kind) or task.client_id is None:
        if _too_soon(None):
            return
        whose = f", клиент {task.client_id}" if task.client_id is not None else ""
        await _notify_owner(
            OWNER_FAILED.format(kind=task.kind, task=task.id, whose=whose)
        )
        return
    if _too_soon(task.client_id):
        audit.log(
            "queue",
            None,
            HUSHED.format(kind=task.kind, task=task.id, client=task.client_id),
            path=path,
        )
        return
    work = title_of(task.kind)
    text = unavailable_text(work) if waiting else failed_text(work)
    await _notify(task.client_id, text)


async def _await_quietly(awaitable: Any) -> None:
    try:
        await awaitable
    except Exception:  # noqa: BLE001 - молчание Telegram не наша авария
        logger.exception("не удалось отправить сообщение о постановке задачи")


def _notify_now(client_id: int | None, text: str) -> None:
    """Сообщение из синхронного места: постановка задачи ничего не ждёт.

    Обработчик в боте асинхронный, поэтому отправка уходит в текущий цикл
    событий. Цикла нет (скрипт, тест без asyncio) - зовём как обычную
    функцию и молчим, если отправить некому.
    """
    if client_id is None or _notifier is None:
        return
    try:
        result = _notifier(client_id, text)
    except Exception:  # noqa: BLE001 - оповещение не роняет постановку задачи
        logger.exception("не удалось сообщить клиенту %s о постановке задачи", client_id)
        return
    if not hasattr(result, "__await__"):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        result.close()
        logger.warning("сообщать клиенту %s некому: цикл событий не запущен", client_id)
        return
    loop.create_task(_await_quietly(result))


# --- настройки повторов ---


def _conf() -> dict[str, Any]:
    try:
        return dict(config.settings().get("queue", {}))
    except Exception:  # noqa: BLE001 - конфиг не должен ронять воркер
        return {}


def max_attempts() -> int:
    """Сколько раз пробовать, прежде чем сдаться. Из конфига, секция queue."""
    try:
        return max(1, int(_conf().get("attempts", 3)))
    except (TypeError, ValueError):
        return 3


def worker_slots() -> int:
    """Сколько работ очередь ведёт одновременно. Из конфига, секция queue."""
    try:
        return max(1, int(_conf().get("workers", 3)))
    except (TypeError, ValueError):
        return 3


def client_slots() -> int:
    """Сколько мест из них могут занять заказанные клиентами отчёты.

    Меньше общего числа мест: остаток закреплён за работами расписания,
    иначе заказанные отчёты задержали бы ночной сбор, а несобранную воронку
    Wildberries потом не отдаст.
    """
    try:
        return max(1, int(_conf().get("client_slots", 2)))
    except (TypeError, ValueError):
        return 2


def notice_gap_sec() -> float:
    """Пауза между сообщениями о сбоях одному получателю. Секции queue.

    Ноль выключает паузу совсем: тогда о каждом сбое говорится отдельно.
    """
    try:
        return max(0.0, float(_conf().get("notice_gap_sec", 900)))
    except (TypeError, ValueError):
        return 900.0


def _backoff(attempts: int) -> float:
    conf = _conf()
    try:
        base = float(conf.get("retry_base_sec", 60))
        factor = float(conf.get("retry_factor", 3))
        cap = float(conf.get("retry_max_sec", 3600))
    except (TypeError, ValueError):
        base, factor, cap = 60.0, 3.0, 3600.0
    return min(base * (factor ** max(0, attempts - 1)), cap)
