"""Проверка ИНН и подстановка реквизитов контрагента.

Две ступени, и порядок между ними важен.

1. Контрольная сумма считается локально. Неверный ИНН отвергается сразу, и
   наружу не уходит ничего: чужой сервис не должен узнавать об опечатках
   наших клиентов, а мы не должны тратить на них запросы.
2. Только верный ИНН идёт в DaData за наименованием и адресом.

Ключа нет, сервис молчит, ничего не нашлось - возвращается None, и клиент
вводит реквизиты руками. Это штатная ветка, а не сбой: без ключа бот обязан
работать, просто с лишним вопросом селлеру.

Третья ступень появилась позже и защищает не клиента, а ключ. У ключа DaData
дневная квота одна на весь сервис, а ИНН приходит обычным текстом: один
человек, шлющий ИНН подряд, выжег бы квоту всем остальным, и счета перестали
бы выставляться у всех. Поэтому на каждого клиента считается дневной предел
обращений (`[limits] dadata_lookups_per_client_per_day`), а уже спрошенный
сегодня ИНН отвечает из памяти и квоту не тратит: справочник за сутки не
меняется. Память о спрошенном ведётся отдельно на каждого клиента, чтобы
ответ, полученный для одного, не всплывал у другого.

Отказ по пределу это не тупик: он возвращает то же None, и клиент вводит
наименование и адрес руками, как и при пустом ключе. Спросить, упёрся ли
клиент в предел, можно через `throttled(client_id)`: хендлер по этому ответу
выбирает текст, а не поведение.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from core import audit, config

logger = logging.getLogger(__name__)

# Подсказки DaData по организациям и ИП. Ключ передаётся заголовком.
DADATA_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/findById/party"
TIMEOUT_SEC = 5.0

# Коэффициенты контрольных сумм ИНН. Для 10 знаков одна цифра, для 12 две.
_W10 = (2, 4, 10, 3, 5, 9, 4, 6, 8)
_W11 = (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
_W12 = (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)

# Счёт обращений и память об ответах живут в памяти процесса, а не в базе.
# Так же решено и в core.ratelimit, и по той же причине: это сведения про
# сутки, которые никому не нужны после перезапуска. Перезапуск обнуляет счёт,
# и это желаемое поведение: после падения никто не должен ждать до полуночи.
_spent: dict[tuple[int, str], int] = {}
_answers: dict[tuple[int, str, str], "Counterparty | None"] = {}
# Сутки, за которые сейчас идёт счёт. Смена даты стирает всё разом: вчерашние
# счётчики и вчерашние ответы справочника не нужны никому, а без уборки бот
# копил бы их до перезапуска.
_counted_day = ""
_lock = threading.Lock()


@dataclass(frozen=True)
class Counterparty:
    """Кого нашли по ИНН. Пустые поля означают, что их спросят у клиента."""

    inn: str
    name: str = ""
    address: str = ""


def normalize(value: Any) -> str:
    """Только цифры: селлер копирует ИНН вместе с пробелами и дефисами."""
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _control(digits: str, weights: tuple[int, ...]) -> int:
    total = sum(int(digits[i]) * weights[i] for i in range(len(weights)))
    return total % 11 % 10


def inn_is_valid(value: Any) -> bool:
    """Контрольная сумма ИНН, 10 или 12 знаков. Без сети.

    Длина проверяется по исходной строке, а не по вычищенной: иначе
    "12345O7894" с латинской буквой превратился бы в девять цифр и был бы
    отвергнут по длине, а не по тому, что это не ИНН. Разница видна в
    сообщении клиенту.
    """
    raw = str(value or "").strip()
    digits = normalize(raw)
    if digits != raw.replace(" ", "").replace("-", ""):
        return False
    if len(digits) == 10:
        return _control(digits, _W10) == int(digits[9])
    if len(digits) == 12:
        return _control(digits, _W11) == int(digits[10]) and _control(
            digits, _W12
        ) == int(digits[11])
    return False


def api_key() -> str:
    """Ключ DaData. Пусто означает, что реквизиты вводятся руками."""
    return config.env("DADATA_API_KEY")


# --- дневной предел на клиента ---


def per_day() -> int:
    """Сколько раз один клиент за сутки может отправить бота в DaData.

    Число из конфига, в коде его нет. Ноль и меньше означают, что предел
    выключен: владелец вправе так решить, как и с частотой сообщений.
    """
    return int(
        config.settings()["limits"].get("dadata_lookups_per_client_per_day", 0)
    )


def _day(now: datetime | None = None) -> str:
    """Сутки, в которых считается предел. Пояс проекта, а не UTC.

    Импорт местный: `core.scheduler` тянет за собой очередь и базу, а этому
    модулю от него нужен один часовой пояс.
    """
    from core import scheduler

    moment = now or datetime.now(scheduler.tz())
    if moment.tzinfo is not None:
        moment = moment.astimezone(scheduler.tz())
    return moment.strftime("%Y-%m-%d")


def throttled(client_id: Any, *, now: datetime | None = None) -> bool:
    """Упёрся ли клиент в дневной предел обращений к DaData.

    Спрашивается после `lookup`, чтобы выбрать текст: «не нашлось» и «на
    сегодня хватит» это разные слова, но одна и та же дальнейшая дорога -
    ввести реквизиты руками.
    """
    allowed = per_day()
    if allowed <= 0 or client_id is None:
        return False
    with _lock:
        return _spent.get((int(client_id), _day(now)), 0) >= allowed


def _remember(client_id: int, inn: str, found: "Counterparty | None", day: str) -> None:
    with _lock:
        _answers[(client_id, day, inn)] = found


def _take(client_id: Any, inn: str, now: datetime | None) -> tuple[bool, Any]:
    """Списывает одно обращение. Отвечает: идти ли в сеть и что отдать сразу.

    Три исхода. Этот ИНН у этого клиента уже спрашивали сегодня - отдаём
    запомненный ответ и квоту не трогаем. Предел выбран - в сеть не идём.
    Иначе списываем одно обращение и идём.
    """
    global _counted_day
    if client_id is None:  # служебный вызов без клиента: считать не на кого
        return True, None
    who = int(client_id)
    day = _day(now)
    allowed = per_day()
    with _lock:
        if day != _counted_day:
            _spent.clear()
            _answers.clear()
            _counted_day = day
        key = (who, day, inn)
        if key in _answers:
            return False, _answers[key]
        used = _spent.get((who, day), 0)
        if allowed > 0 and used >= allowed:
            return False, None
        _spent[(who, day)] = used + 1
    return True, None


def forget(client_id: Any = None) -> None:
    """Забыть счёт и запомненные ответы: целиком или про одного клиента."""
    global _counted_day
    with _lock:
        if client_id is None:
            _spent.clear()
            _answers.clear()
            _counted_day = ""
            return
        who = int(client_id)
        for key in [key for key in _spent if key[0] == who]:
            _spent.pop(key, None)
        for key in [key for key in _answers if key[0] == who]:
            _answers.pop(key, None)


def _parse(payload: Any, inn: str) -> Counterparty | None:
    """Первая подсказка DaData. Форма ответа описана в её документации."""
    if not isinstance(payload, dict):
        return None
    suggestions = payload.get("suggestions") or []
    if not suggestions:
        return None
    first = suggestions[0] or {}
    data = first.get("data") or {}
    address = data.get("address") or {}
    name = str(first.get("value") or "").strip()
    line = str(
        address.get("unrestricted_value") or address.get("value") or ""
    ).strip()
    if not name and not line:
        return None
    return Counterparty(inn=str(data.get("inn") or inn), name=name, address=line)


async def lookup(
    inn: Any,
    *,
    client_id: Any = None,
    now: datetime | None = None,
    http: Any = None,
    path: str | Path | None = None,
) -> Counterparty | None:
    """Наименование и адрес по ИНН. None означает «спроси у клиента».

    `client_id` включает дневной предел и память об уже спрошенном ИНН. Без
    него запрос идёт как раньше: считать некому, значит это служебный вызов.

    `http` подставляется в тестах: объект с методом `post`, как у
    httpx.AsyncClient. В работе клиент создаётся на один запрос и тут же
    закрывается - счета выставляются редко, держать соединение незачем.

    Ключ наружу из этой функции не выходит ни в каком виде. Текст исключения
    в лог не попадает, только имя его класса: сообщение чужой библиотеки
    может нести и заголовки запроса, а заголовок здесь один и в нём ключ.
    Журнал пишется через core.audit, который прячет DADATA_API_KEY сам.
    """
    digits = normalize(inn)
    if not inn_is_valid(digits):
        return None
    key = api_key()
    if not key:
        return None

    go, remembered = _take(client_id, digits, now)
    if not go:
        if remembered is None and throttled(client_id, now=now):
            # В журнал идёт событие и внутренний id, а не ИНН: чей это
            # справочник, владельцу для разбора знать не нужно.
            audit.log(
                "dadata.limit",
                int(client_id),
                "дневной предел обращений к справочнику выбран, "
                "реквизиты клиент введёт руками",
                level="warning",
                path=path,
            )
        return remembered
    day = _day(now)

    own = http is None
    client = http
    if own:
        import httpx

        client = httpx.AsyncClient(timeout=TIMEOUT_SEC)
    try:
        response = await client.post(
            DADATA_URL,
            json={"query": digits, "count": 1},
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Token {key}",
            },
        )
        status = getattr(response, "status_code", 200)
        if int(status) >= 400:
            logger.warning("DaData ответила %s, реквизиты спросим у клиента", status)
            audit.log(
                "dadata",
                None,
                f"DaData ответила {status}, реквизиты спросим у клиента",
                level="warning",
                path=path,
            )
            return None
        found = _parse(response.json(), digits)
        # Ответ справочника запоминается на сутки, в том числе пустой: если
        # ИНН там не числится, повторный вопрос про тот же ИНН даст то же
        # самое и потратит квоту зря. Сбои не запоминаются намеренно, иначе
        # минутная недоступность DaData стоила бы клиенту целого дня.
        if client_id is not None:
            _remember(int(client_id), digits, found, day)
        return found
    except Exception as failure:  # noqa: BLE001 - недоступность справочника не авария
        # Только имя класса. Текст исключения сюда не попадает намеренно:
        # вместе с ним в лог уехал бы и ключ, если библиотека решит
        # процитировать запрос.
        reason = type(failure).__name__
        logger.warning("DaData недоступна (%s), реквизиты спросим у клиента", reason)
        audit.log(
            "dadata",
            None,
            f"DaData недоступна ({reason}), реквизиты клиент введёт руками",
            level="warning",
            path=path,
        )
        return None
    finally:
        if own:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                logger.debug("не удалось закрыть клиента DaData", exc_info=True)
