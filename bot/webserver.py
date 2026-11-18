"""Маленький веб-сервер в том же процессе, что и бот: приём оплаты.

Один адрес и один метод: `POST /pay/webhook`, туда стучится Prodamus с
уведомлением об оплате. Всё остальное получает 404, включая GET на тот же
адрес: уведомление приходит только POST'ом, и отвечать на любопытный заход
браузером нам нечего.

Почему сервер живёт рядом с ботом, а не отдельным процессом: так советует
хостинг, и так проще. Боту нужны та же база и тот же Telegram, а второй
процесс означал бы вторую копию и очереди, и расписания.

Порт берётся из окружения, и он же решает, запускать сервер вообще. Нет
`PORT` - сервера нет: локально при разработке лишний слушающий порт не
нужен, а на хостинге переменную передаёт платформа.

Почему aiohttp, а не «сами разберём запрос»: Prodamus шлёт уведомление
`multipart/form-data`, и разбирать его руками значит написать свой парсер
многочастного тела. Ошибка в нём выглядела бы как неверная подпись, то есть
как потерянная оплата.

Разметки и текстов для клиента тут нет: сервер отвечает платёжному сервису,
а тот читает только код ответа.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from aiohttp import web

from bot import payments
from core import audit, config

logger = logging.getLogger(__name__)

# Путь уведомления. Тот же, что владелец впишет в настройках Prodamus.
PAY_PATH = "/pay/webhook"

# Порт, который ждёт хостинг. Это его число, а не наша настройка: то же
# самое стоит в поле «Порт» карточки бота, снаружи на него проксируется 443,
# и поменять его в одном месте без другого нельзя.
DEFAULT_PORT = 3000

# Откуда берётся порт, по порядку. Своя переменная идёт первой, хостинговая
# второй: платформа передаёт PORT сама, но строить приём денег на честном
# слове чужой переменной нельзя. Не приехала - сервер молча не поднимется, и
# уведомления об оплате будут уходить в никуда, а узнаем мы об этом от
# клиента, который заплатил и не получил доступ. WEB_PORT это способ сказать
# прямо и не зависеть от того, передаёт платформа что-нибудь или нет.
PORT_VARS = ("WEB_PORT", "PORT")

# Заголовок с подписью уведомления.
SIGN_HEADER = "Sign"


def web_port() -> int:
    """Порт для входящих запросов. Ноль означает «сервер не поднимать»."""
    for name in PORT_VARS:
        raw = config.env(name)
        if not raw:
            continue
        try:
            port = int(raw)
        except ValueError:
            logger.warning("значение %s=%s не разобрать", name, raw)
            continue
        if 0 < port < 65536:
            return port
        logger.warning("значение %s=%s вне диапазона портов", name, raw)
    return 0


def make_app(app: Any, path: str | Path | None = None) -> web.Application:
    """Приложение aiohttp с единственным обработчиком оплаты."""
    receive = payments.make_receiver(app, path)

    async def handle(request: web.Request) -> web.Response:
        # `post()` сам разбирает и multipart, и обычную форму. Поля читаются
        # плоско, как их прислали: разворачивает их обратно в структуру уже
        # core.billing.payform, потому что по ней считается подпись.
        try:
            fields = dict(await request.post())
        except Exception:  # noqa: BLE001 - кривое тело это не наша авария
            logger.exception("не удалось разобрать тело уведомления об оплате")
            return web.Response(status=400, text="bad request")
        sign = request.headers.get(SIGN_HEADER, "")
        code, body = await receive(fields, sign)
        return web.Response(status=code, text=body)

    served = web.Application()
    served.router.add_post(PAY_PATH, handle)
    return served


async def start(app: Any, path: str | Path | None = None) -> Any:
    """Поднимает сервер, если порт задан. Возвращает runner или None.

    Сбой запуска сервера бота не роняет: без приёма оплаты бот работает
    по-прежнему, а владелец выдаёт доступ командой. Упасть целиком из-за
    занятого порта было бы хуже самой проблемы.
    """
    port = web_port()
    if not port:
        return None
    try:
        runner = web.AppRunner(make_app(app, path))
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", port).start()
    except Exception:  # noqa: BLE001 - бот важнее приёма оплаты
        logger.exception("не удалось поднять веб-сервер на порту %s", port)
        audit.log(
            "webserver",
            None,
            f"Веб-сервер на порту {port} не поднялся: уведомления об оплате "
            "приходить не будут, доступ придётся выдавать командой.",
            level="error",
        )
        return None
    audit.log("webserver", None, f"Приём оплаты слушает порт {port}, путь {PAY_PATH}.")
    return runner


async def stop(runner: Any) -> None:
    """Гасит сервер. Молча, если его и не было."""
    if runner is None:
        return
    try:
        await runner.cleanup()
    except Exception:  # noqa: BLE001 - остановку нечем чинить
        logger.exception("веб-сервер не остановился чисто")
