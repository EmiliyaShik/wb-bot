"""Команда /diag: проверка связи с серверами WB прямо с боевого хоста.

Это пункт 0.1 ТЗ: убедиться, что запросы к *-api.wildberries.ru уходят без
прокси, а если нет, назвать причину. Команда только для владельца и только
вручную: документация WB запрещает автоматизировать /ping, поэтому в
расписание она не ставится ни при каких условиях.

Тексты живут здесь же, рядом с хендлером. Русский, без длинных тире.
"""

from __future__ import annotations

import logging

from telegram.ext import CommandHandler

from core import audit, config, db, wbapi
from bot import texts

logger = logging.getLogger(__name__)

STARTED = (
    "Проверяю связь с серверами Wildberries. Это шесть доменов с паузами "
    "между ними, займёт около минуты."
)

HEADER = "🩺 <b>Проверка связи с WB</b>\n"

NO_TOKEN_NOTE = (
    "Проверка шла без токена: ответ 401 в этом случае тоже хорошая новость, "
    "он означает, что запрос дошёл до Wildberries."
)

WITH_TOKEN_NOTE = "Проверка шла с вашим токеном кабинета."

FAILED = "Проверку не удалось довести до конца. Подробности записаны в журнал бота."

MARKS = {True: "✅", False: "❌"}


def report_text(probes, note: str = "") -> str:
    """Собирает отчёт: по строке на домен плюс объяснение каждого ответа."""
    lines = [HEADER]
    for probe in probes:
        code = probe.status if probe.status is not None else "нет ответа"
        lines.append(f"{MARKS[probe.ok]} <b>{probe.host}</b>: {code}")
        lines.append(probe.verdict)
        lines.append("")
    good = sum(1 for probe in probes if probe.ok)
    lines.append(f"Отвечают нормально: {good} из {len(probes)}.")
    if note:
        lines.append(note)
    return "\n".join(lines).strip()


def _owner_client_id(telegram_id: int) -> int | None:
    """Свой кабинет владельца, если он подключён. Токен отсюда не выходит."""
    try:
        row = db.admin_repo().client_by_telegram(telegram_id)
        if row is None:
            return None
        client_id = int(row["id"])
        return client_id if db.repo(client_id).one("wb_tokens") else None
    except Exception:
        logger.exception("не смог посмотреть, подключён ли кабинет владельца")
        return None


async def diag(update, context) -> None:
    """Обходит домены WB и объясняет каждый ответ. Только для владельца."""
    user = getattr(update, "effective_user", None)
    if not config.is_admin(getattr(user, "id", None)):
        await update.message.reply_text(texts.ADMIN_ONLY)
        return

    await update.message.reply_text(STARTED)
    client_id = _owner_client_id(int(user.id))
    try:
        probes = await wbapi.probe_hosts(client_id=client_id)
    except Exception as exc:
        audit.log("diag", None, f"проверка связи сорвалась: {exc}", level="error")
        await update.message.reply_text(FAILED)
        return

    note = WITH_TOKEN_NOTE if client_id else NO_TOKEN_NOTE
    reachable = sum(1 for probe in probes if probe.status is not None)
    audit.log("diag", None, f"проверка связи: ответили {reachable} из {len(probes)} доменов")
    await update.message.reply_text(report_text(probes, note), parse_mode="HTML")


def register(app) -> None:
    """Хендлер ставит себя сам. В расписание команда не попадает никогда."""
    app.add_handler(CommandHandler("diag", diag))
