"""Присмотр за связью с Telegram: обрыв это пауза, а не конец работы.

Сначала то, что выяснено опытом про python-telegram-bot 21.9, потому что от
этого зависит, чем чинить.

Процесс при обрыве не умирает, и опрос `run_polling` не бросает: цикл
`Updater._network_loop_retry` ловит ошибку, зовёт наш обработчик ошибок и
пробует снова, растягивая паузу до тридцати секунд. Пока провайдер отвечает
отказом, так и идёт: `httpx.ConnectError` приезжает к нам как `NetworkError`,
и в журнале видно строку за строкой.

Но таймаут тот же цикл разбирает отдельной веткой и молча. Пауза ставится в
ноль, то есть «повторить сразу», наш обработчик ошибок не зовётся вовсе, а в
вывод уходит одна строка уровня DEBUG, которой при обычных настройках не
видно. Опыт на подставном транспорте: триста тысяч попыток за восемь секунд и
ни одной записи нигде. А таймаутом обрыв приезжает ровно тогда, когда пакеты
начинают пропадать без ответа, то есть при настоящих помехах, а не при
аккуратном отказе: `httpx.ConnectTimeout` и `ReadTimeout` библиотека переводит
в `TimedOut`, а не в `NetworkError`.

Отсюда и то, что видел владелец: три строки `NetworkError: httpx.ConnectError`
в 00:56:22, потом одиннадцать минут полной тишины при живом процессе, и
помогло только выключить и запустить. Бот всё это время ждал и пробовал, но
снаружи это неотличимо от зависшего.

Настройкой библиотеки это не лечится: до обработчика ошибок таймаут не доходит
ни при каких её параметрах, а перехватить его больше негде. Настройкой лечится
другая половина беды, и она стоит в `bot/app.py`: `bootstrap_retries` по
умолчанию ноль, то есть обрыв связи в момент запуска роняет процесс на первой
же неудаче.

Поэтому здесь ровно два дела.

Первое: держать паузу между попытками опроса, чтобы оборванная сеть не
долбилась без передышки. Числа в `[telegram]` конфига, в коде их нет.

Второе: не молчать. Начало обрыва, затянувшийся обрыв и возвращение связи идут
в журнал, а о затянувшемся владелец узнаёт словами. Про каждый обрыв ему не
пишут: обрыв на двадцать секунд это не новость, а поток таких сообщений хуже,
чем их отсутствие.

Сказать владельцу во время обрыва получается не всегда, и это честное
ограничение, а не недоделка: когда Telegram недостижим целиком, отправлять в
Telegram нечего. Поэтому слово пробуется сразу (бывает, что оборвался только
опрос: соединение под него у библиотеки своё, одно на всю работу) и, если не
дошло, повторяется, когда связь вернётся. В журнале запись есть в любом
случае: он лежит в sqlite и от Telegram не зависит.

Очередь задач при обрыве не останавливается, и это тоже решение, а не
недосмотр. Воркер живёт в том же процессе, но ходит в Wildberries, а не в
Telegram, и доставка готового отчёта у него и так обёрнута молчанием.
Недоступный Telegram означает «отчёт придёт позже», а пропущенный ночной сбор
воронки означает «этих суток не будет никогда»: WB отдаёт её только за неделю
назад.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from telegram.request import HTTPXRequest

from core import audit, config

logger = logging.getLogger(__name__)

# Вид события в журнале. Один на все записи про связь: владелец и тот, кто
# разбирает, читают историю обрывов одним срезом.
JOURNAL_KIND = "telegram"

# Соединений под опрос столько же, сколько ставит библиотека по умолчанию.
# Присмотр не должен заодно менять поведение транспорта: тогда непонятно, что
# именно починилось.
UPDATES_POOL = 1

# Сколько знаков чужой ошибки берём в запись. Длинный ответ ничего не
# добавляет, а журнал читают с телефона.
ERROR_CHARS = 200


# --- записи в журнал ---

STARTED = "Связь с Telegram оборвалась ({error}). Жду и пробую снова, перезапуск не нужен."

LONG = (
    "Связи с Telegram нет уже {spell}, попыток {attempts}. Опрос продолжается, "
    "сбор данных Wildberries идёт."
)

LONG_HUSHED = (
    "Связи с Telegram нет уже {spell}, попыток {attempts}. Владельцу промолчал: "
    "недавно уже писал об обрыве."
)

BACK = "Связь с Telegram вернулась. Без связи было {spell}, попыток {attempts}."


# --- слова владельцу. Разметки здесь нет и быть не должно: уходит как есть ---

OWNER_OUTAGE = (
    "Связи с Telegram нет уже {spell}. Я не остановился: жду и пробую снова "
    "сам, перезапускать меня не нужно. Сбор данных по кабинетам Wildberries "
    "идёт как обычно, а отчёты, которые не ушли, уйдут, когда связь вернётся."
)

OWNER_BACK = (
    "Связь с Telegram вернулась, без связи было {spell}. Всё это время я ждал "
    "и пробовал снова, перезапуск не понадобился."
)


# --- состояние обрыва ---
#
# В памяти, а не в базе: новой колонки схема не заводит, а перезапуск бота и
# так означает, что прежний обрыв кончился вместе с прежним процессом.
# Худшее, что даёт потеря памяти, это одно лишнее сообщение владельцу.

_failures = 0
_since: datetime | None = None
# Что решили про текущий обрыв: пусто - ещё ничего, "said" - сказали или
# пробовали сказать, "hushed" - промолчали по паузе между сообщениями.
_notice = ""
_last_said: datetime | None = None

_notifier: Callable[[str], Any] | None = None
_polling: Callable[[], bool] | None = None

SAID = "said"
HUSHED = "hushed"


def reset() -> None:
    """Забывает обрыв и обработчики. Нужно тестам и подъёму фона заново."""
    global _failures, _since, _notice, _last_said, _notifier, _polling
    _failures = 0
    _since = None
    _notice = ""
    _last_said = None
    _notifier = None
    _polling = None


def set_notifier(fn: Callable[[str], Any] | None) -> None:
    """Чем сказать владельцу. Ставит поверхность бота при старте.

    Отправка обязана бросать отказ наружу, а не гасить его: присмотру важно
    именно то, дошло ли слово. Не дошло - он повторит его, когда связь
    вернётся, и владелец всё-таки узнает про обрыв.
    """
    global _notifier
    _notifier = fn


def set_polling(fn: Callable[[], bool] | None) -> None:
    """Чем спросить, идёт ли ещё опрос. На выключении паузы не держат.

    Библиотека гасит опрос так: сначала снимает признак работы, потом ещё раз
    зовёт getUpdates, чтобы отметить прочитанное. Без этого вопроса выключение
    бота во время обрыва ждало бы нашу паузу целиком.
    """
    global _polling
    _polling = fn


def failures() -> int:
    """Сколько попыток опроса подряд не прошло. Ноль значит «связь есть»."""
    return _failures


def outage_sec(now: datetime | None = None) -> float:
    """Сколько секунд длится обрыв. Ноль значит, что обрыва нет."""
    if _since is None:
        return 0.0
    return max(0.0, ((now or _now()) - _since).total_seconds())


# --- настройки из конфига ---


def _conf() -> dict[str, Any]:
    try:
        return dict(config.settings().get("telegram", {}))
    except Exception:  # noqa: BLE001 - конфиг не должен ронять опрос
        return {}


def _number(name: str, default: float) -> float:
    try:
        return float(_conf().get(name, default))
    except (TypeError, ValueError):
        return default


def pause_sec(count: int) -> float:
    """Пауза перед следующей попыткой опроса. Растёт, но не выше потолка."""
    if count <= 0:
        return 0.0
    base = max(0.0, _number("retry_base_sec", 2.0))
    factor = max(1.0, _number("retry_factor", 2.0))
    cap = max(0.0, _number("retry_max_sec", 60.0))
    return min(base * (factor ** (count - 1)), cap)


def notice_after_sec() -> float:
    """Сколько связи нет, прежде чем об этом говорят. Ноль выключает разговор."""
    return _number("notice_after_sec", 300.0)


def notice_gap_sec() -> float:
    """Пауза между сообщениями об обрывах. Ноль выключает паузу."""
    return max(0.0, _number("notice_gap_sec", 3600.0))


# --- внутреннее ---


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _spell(seconds: float) -> str:
    """Длительность словами селлера: «4 мин», а не «247 секунд»."""
    whole = int(max(0.0, seconds))
    minutes = whole // 60
    if minutes < 1:
        return f"{whole} с"
    if minutes < 60:
        return f"{minutes} мин"
    return f"{minutes // 60} ч {minutes % 60} мин"


def _short(error: BaseException) -> str:
    """Чужая ошибка коротко. Секреты снимет чистка журнала, не мы."""
    text = f"{type(error).__name__}: {error}".strip()
    return text[:ERROR_CHARS]


def _polling_now() -> bool:
    if _polling is None:
        return True
    try:
        return bool(_polling())
    except Exception:  # noqa: BLE001 - непонятный ответ считаем работой
        return True


async def _say(text: str) -> bool:
    """Говорит владельцу. Отвечает, дошло ли слово: от этого зависит повтор."""
    if _notifier is None:
        return False
    try:
        result = _notifier(text)
        if hasattr(result, "__await__"):
            await result
    except Exception:  # noqa: BLE001 - молчащий Telegram это и есть наш случай
        logger.warning("сказать владельцу об обрыве связи не удалось")
        return False
    return True


def _ripe(now: datetime) -> bool:
    """Пора ли вообще заговаривать про этот обрыв."""
    after = notice_after_sec()
    if after <= 0:
        return False
    return outage_sec(now) >= after


def _may_say(now: datetime) -> bool:
    """Не писали ли владельцу об обрывах только что."""
    gap = notice_gap_sec()
    if gap <= 0 or _last_said is None:
        return True
    return (now - _last_said).total_seconds() >= gap


# --- то, что зовёт транспорт ---


async def failed(error: BaseException) -> float:
    """Попытка опроса не прошла. Возвращает паузу перед следующей.

    В журнал пишется начало обрыва и его затягивание, а не каждая попытка: при
    долгом обрыве их десятки, и нового в них нет ничего.
    """
    global _failures, _since, _notice, _last_said
    now = _now()
    _failures += 1
    if _since is None:
        _since = now
        audit.log(
            JOURNAL_KIND, None, STARTED.format(error=_short(error)), level="warning"
        )
    if not _notice and _ripe(now):
        spell = _spell(outage_sec(now))
        if _may_say(now):
            _notice = SAID
            audit.log(
                JOURNAL_KIND,
                None,
                LONG.format(spell=spell, attempts=_failures),
                level="error",
            )
            if await _say(OWNER_OUTAGE.format(spell=spell)):
                # Паузу между сообщениями считаем от дошедшего, а не от
                # попытки: не дошло - владелец ничего не узнал, и молчать
                # потом ещё час было бы не бережностью, а потерей слова.
                _last_said = now
        else:
            _notice = HUSHED
            audit.log(
                JOURNAL_KIND,
                None,
                LONG_HUSHED.format(spell=spell, attempts=_failures),
                level="error",
            )
    return pause_sec(_failures)


async def succeeded() -> None:
    """Опрос прошёл. Закрывает обрыв и, если о нём говорили, сообщает конец."""
    global _failures, _since, _notice, _last_said
    if _since is None:
        _failures = 0
        return
    spell = _spell(outage_sec())
    attempts = _failures
    told = _notice == SAID
    _failures = 0
    _since = None
    _notice = ""
    audit.log(JOURNAL_KIND, None, BACK.format(spell=spell, attempts=attempts))
    if not told:
        return
    # Конец разговора паузой между сообщениями не придерживается: оставить
    # владельца с «связи нет» и без «вернулась» хуже, чем написать дважды.
    # Сюда же попадает обрыв, слово о котором не дошло: владелец узнаёт о нём
    # сейчас, задним числом, и это всё ещё лучше, чем не узнать вовсе.
    if await _say(OWNER_BACK.format(spell=spell)):
        _last_said = _now()


class WatchedRequest(HTTPXRequest):
    """Транспорт опроса, который считает обрывы и держит паузу.

    Паузу приходится держать здесь, а не параметром библиотеки: таймаут она
    повторяет без паузы вовсе и мимо обработчика ошибок (см. заголовок файла),
    а другого места, через которое проходит каждая попытка опроса, нет.

    Присматриваем за транспортом, а не за ответом: отказ Telegram с кодом
    ошибки это не обрыв связи, связь в этот момент как раз есть, и разбирают
    такой ответ обработчики выше.
    """

    async def do_request(self, *args: Any, **kwargs: Any) -> tuple[int, bytes]:
        try:
            answer = await super().do_request(*args, **kwargs)
        except Exception as error:  # noqa: BLE001 - обрыв приезжает любым видом
            pause = await failed(error)
            # Пауза держится до того, как ошибка уйдёт наверх: наверху её ждёт
            # цикл, который на таймауте не ждёт нисколько.
            if pause > 0 and _polling_now():
                await asyncio.sleep(pause)
            raise
        await succeeded()
        return answer


def updates_request() -> WatchedRequest:
    """Транспорт getUpdates под присмотром. Зовёт сборка приложения."""
    return WatchedRequest(connection_pool_size=UPDATES_POOL)
