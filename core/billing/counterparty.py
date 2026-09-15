"""Проверка ИНН и подстановка реквизитов контрагента.

Две ступени, и порядок между ними важен.

1. Контрольная сумма считается локально. Неверный ИНН отвергается сразу, и
   наружу не уходит ничего: чужой сервис не должен узнавать об опечатках
   наших клиентов, а мы не должны тратить на них запросы.
2. Только верный ИНН идёт в DaData за наименованием и адресом.

Ключа нет, сервис молчит, ничего не нашлось - возвращается None, и клиент
вводит реквизиты руками. Это штатная ветка, а не сбой: без ключа бот обязан
работать, просто с лишним вопросом селлеру.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
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
    inn: Any, *, http: Any = None, path: str | Path | None = None
) -> Counterparty | None:
    """Наименование и адрес по ИНН. None означает «спроси у клиента».

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
        return _parse(response.json(), digits)
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
