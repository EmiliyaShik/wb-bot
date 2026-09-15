"""Проверка связи с серверами WB: обход всех доменов методом /ping.

Документация прямо запрещает автоматизировать /ping: при попытке запросы
временно блокируют. Поэтому здесь нет ни повторов, ни расписания, а между
доменами стоит пауза. Единственный вызывающий, который допустим, это ручная
команда владельца /diag.

Смысл проверки в том, чтобы ответить на вопрос из брифа: доходит ли запрос
до WB с боевого хоста без прокси, и если нет, то почему.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from core import db
from core.wbapi.client import HOSTS, WBClient, load_token, shared_session
from core.wbapi.token import verify_token
from core.wbapi.errors import (
    WBApiError,
    WBAuthError,
    WBError,
    WBForbiddenError,
    WBRateLimited,
    WBUnavailable,
)

# Пауза между доменами. Лимит /ping считается на каждый домен отдельно,
# но идти вплотную незачем: это проверка связи, а не гонка.
PAUSE_BETWEEN_HOSTS = 1.0


@dataclass(frozen=True)
class HostProbe:
    """Ответ одного домена и что он означает простым языком."""

    name: str
    host: str
    status: int | None
    ok: bool
    verdict: str
    duration_ms: int = 0


def verdict_for(status: int | None) -> str:
    """Объяснение кода ответа. Читатель это владелец бота, а не программист."""
    if status is None:
        return (
            "Ответа нет вообще: запрос не дошёл или его оборвали. "
            "Так выглядит запрет по адресу или сети, тут нужен прокси или другой хост."
        )
    if 200 <= status < 300:
        return "Связь есть: запрос дошёл, токен принят."
    if status == 401:
        return (
            "Связь есть, запрос дошёл, но токен не принят. "
            "Дело в самом токене: отозван, просрочен или из другого кабинета."
        )
    if status == 403:
        return (
            "Связь есть, токен рабочий, но у него нет нужной категории "
            "для этого сервиса. Это про права токена, а не про доступ к WB."
        )
    if status == 429:
        return (
            "Связь есть, но запросов слишком много: у /ping лимит "
            "3 запроса за 30 секунд на домен. Повторите через полминуты."
        )
    if status >= 500:
        return "Связь есть, отвечает сам WB, но с ошибкой на своей стороне."
    return f"Связь есть, ответ {status}. Смотрите журнал: это не типовой случай."


async def probe_hosts(
    *,
    client_id: int | None = None,
    http: httpx.AsyncClient | None = None,
    path: str | None = None,
    sleep=None,
    clock=None,
    hosts: dict[str, str] | None = None,
    pause: float = PAUSE_BETWEEN_HOSTS,
) -> list[HostProbe]:
    """Обходит домены WB методом /ping и объясняет каждый ответ.

    Токен берётся у клиента, если он назван, и наружу не выходит. Без токена
    проверка тоже осмысленна: 401 означает, что запрос дошёл, а это и есть
    главный вопрос про боевой хост.
    """
    token = load_token(client_id, path) if client_id is not None else ""
    client = WBClient(
        client_id, token, http or shared_session(), path=path, clock=clock, sleep=sleep
    )
    probes: list[HostProbe] = []
    for index, (name, host) in enumerate((hosts or HOSTS).items()):
        if index:
            await client.pause(pause)
        status: int | None = None
        try:
            # Код берётся у самого домена, а не подставляется константой:
            # весь смысл проверки в том, чтобы показать настоящий ответ.
            status = await client.ping(host)
        except (WBAuthError, WBForbiddenError, WBRateLimited, WBApiError) as exc:
            status = exc.status
        except WBUnavailable as exc:
            status = exc.status
        except WBError:
            status = None
        probes.append(
            HostProbe(
                name=name,
                host=host,
                status=status,
                ok=status is not None and 200 <= status < 300,
                verdict=verdict_for(status),
            )
        )
    return probes


async def check_token(
    raw: str,
    *,
    http: httpx.AsyncClient | None = None,
    path: str | None = None,
    client_id: int | None = None,
    host: str | None = None,
    sleep=None,
    clock=None,
):
    """Разбирает токен и делает ровно один пробный запрос: не отозван ли он.

    Всё остальное о токене известно без сети. Запрос нужен только затем, чтобы
    отличить живой токен от отозванного: этого из JWT не видно.

    403 отказом не считается: он означает, что токен приняли, просто у него
    нет категории для этого домена. Такой токен годится, и что именно
    перестанет работать, скажет уже хендлер подключения.
    """
    info = verify_token(raw)
    client = WBClient(
        client_id, raw, http or shared_session(), path=path, clock=clock, sleep=sleep
    )
    try:
        await client.ping(host or HOSTS["finance"])
    except WBForbiddenError:
        pass
    return info


def last_probe_calls(path: str | None = None, limit: int = 12) -> list:
    """Последние записи об обходе из api_calls. Нужны владельцу в журнале."""
    return [row for row in db.admin_repo(path).api_calls(limit=limit) if row["method"].endswith("/ping")]
