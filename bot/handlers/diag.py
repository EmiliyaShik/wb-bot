"""Команда /diag: проверка связи с серверами WB прямо с боевого хоста.

Это пункт 0.1 ТЗ: убедиться, что запросы к *-api.wildberries.ru уходят без
прокси, а если нет, назвать причину. Команда только для владельца и только
вручную: документация WB запрещает автоматизировать /ping, поэтому в
расписание она не ставится ни при каких условиях.

Две вещи, сделанные тут намеренно и не очевидные из кода:

Постороннему команда не отвечает вообще. Не «нельзя», а молчание: ответ
«только для владельца» сам по себе сообщает, что админка есть, и перебором
команд её можно найти. Остальные команды владельца молчат так же.

Сам обход уходит в очередь. Шесть доменов с паузами это около минуты, а бот
обрабатывает одно сообщение за раз: ожидание внутри хендлера было бы минутой
молчания для всех сразу. Хендлер отвечает сразу, отчёт приходит отдельным
сообщением, когда работа дойдёт до своей очереди.

Тексты живут здесь же, рядом с хендлером. Русский, без длинных тире.

Отчёт уходит с разметкой, а внутрь идут имя домена и приговор проверки,
поэтому подстановка тут общая, `bot.texts.fill`: граница стоит на ней, как
и во всех остальных хендлерах.
"""

from __future__ import annotations

import logging

from telegram.ext import CommandHandler

from bot.texts import Safe, fill
from core import audit, config, db, queue, wbapi

logger = logging.getLogger(__name__)

TASK_KIND = "wb_diag"

STARTED = (
    "Принято. Проверяю связь с серверами Wildberries: шесть доменов с паузами "
    "между ними. Пришлю отчёт, как только обход закончится."
)

HEADER = "🩺 <b>Проверка связи с WB</b>\n"

NO_TOKEN_NOTE = (
    "Проверка шла без токена: ответ 401 в этом случае тоже хорошая новость, "
    "он означает, что запрос дошёл до Wildberries."
)

WITH_TOKEN_NOTE = "Проверка шла с вашим токеном кабинета."

FAILED = "Проверку не удалось довести до конца. Подробности записаны в журнал бота."

MARKS = {True: "✅", False: "❌"}


def report_text(probes, note: str = "") -> Safe:
    """Собирает отчёт: по строке на домен плюс объяснение каждого ответа."""
    lines = [HEADER]
    for probe in probes:
        code = probe.status if probe.status is not None else "нет ответа"
        lines.append(
            fill(
                "{mark} <b>{host}</b>: {code}",
                mark=MARKS[probe.ok],
                host=probe.host,
                code=code,
            )
        )
        lines.append(fill("{verdict}", verdict=probe.verdict))
        lines.append("")
    good = sum(1 for probe in probes if probe.ok)
    lines.append(
        fill("Отвечают нормально: {good} из {total}.", good=good, total=len(probes))
    )
    if note:
        lines.append(note)
    return Safe("\n".join(lines).strip())


def _owner_client_id(telegram_id) -> int | None:
    """Свой кабинет владельца, если он подключён. Токен отсюда не выходит."""
    if telegram_id is None:
        return None
    try:
        row = db.admin_repo().client_by_telegram(int(telegram_id))
        if row is None:
            return None
        client_id = int(row["id"])
        return client_id if db.repo(client_id).one("wb_tokens") else None
    except Exception:
        logger.exception("не смог посмотреть, подключён ли кабинет владельца")
        return None


def make_runner(app):
    """Обработчик задачи очереди. Ждать лимиты здесь можно: воркер один."""

    async def run(task) -> None:
        payload = task.payload or {}
        chat_id = payload.get("chat_id")
        client_id = _owner_client_id(payload.get("telegram_id"))
        try:
            probes = await wbapi.probe_hosts(client_id=client_id)
        except Exception as exc:
            audit.log("diag", None, f"проверка связи сорвалась: {exc}", level="error")
            if chat_id is not None:
                await app.bot.send_message(chat_id=chat_id, text=FAILED)
            return
        note = WITH_TOKEN_NOTE if client_id else NO_TOKEN_NOTE
        reachable = sum(1 for probe in probes if probe.status is not None)
        audit.log("diag", None, f"проверка связи: ответили {reachable} из {len(probes)} доменов")
        if chat_id is not None:
            await app.bot.send_message(
                chat_id=chat_id, text=report_text(probes, note), parse_mode="HTML"
            )

    return run


async def diag(update, context) -> None:
    """Ставит обход в очередь и сразу отвечает. Только для владельца."""
    user = getattr(update, "effective_user", None)
    telegram_id = getattr(user, "id", None)
    if not config.is_admin(telegram_id):
        # Молча: для постороннего этой команды не существует.
        return

    chat = getattr(update, "effective_chat", None)
    queue.enqueue(
        None,
        TASK_KIND,
        {"chat_id": getattr(chat, "id", telegram_id), "telegram_id": int(telegram_id)},
        notify=False,
    )
    await update.message.reply_text(STARTED)


def register(app) -> None:
    """Хендлер ставит себя сам. В расписание команда не попадает никогда."""
    app.add_handler(CommandHandler("diag", diag))
    queue.register(TASK_KIND, make_runner(app))
