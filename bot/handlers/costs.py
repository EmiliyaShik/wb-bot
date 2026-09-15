"""Телеграм-поверхность себестоимости: `/costs` и приём книги обратно.

Разделение простое. Хендлер только разговаривает с селлером: принимает
команду, проверяет присланный файл на входе и рассказывает, что получилось.
Сборка шаблона идёт в WB, поэтому её ставят в очередь: повтор при
недоступности WB живёт только там, и вызов мимо очереди упал бы без
повтора. Обещание «принято, пришлю, когда будет готово» тоже даёт очередь,
поэтому тут его нет.

Файл от постороннего проверяется трижды и в таком порядке, чтобы дорогая
работа не делалась зря: расширение, размер по данным Telegram (до
скачивания), содержимое (после). Отказ всегда объясняет причину.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import CommandHandler, MessageHandler, filters

from bot import texts
from bot.handlers.tariffs import client_id_of
from core import costs, db, queue

logger = logging.getLogger(__name__)

# Группа для обработчика документов: отдельная, чтобы приём файла не спорил
# с хендлерами соседних модулей, которые тоже смотрят на сообщения.
DOCUMENT_GROUP = 40

# Сколько строк с ошибками показываем, чтобы ответ остался читаемым.
PROBLEMS_SHOWN = 15

CAPTION = (
    "Заполните колонку «Себестоимость за единицу, ₽» и пришлите файл обратно. "
    "Артикулы, оставшиеся без себестоимости, попадут в отчётах в "
    "блок «нет себестоимости», остальное посчитается как обычно."
)

NOT_XLSX = (
    "Принимаю только файл xlsx. Откройте шаблон из команды <code>/costs</code>, "
    "заполните колонку с себестоимостью и сохраните в формате xlsx."
)


def too_big_text(size: int, limit: int) -> str:
    return (
        f"Файл слишком большой: {costs.mb(size)} при разрешённом размере "
        f"{costs.mb(limit)}. "
        "Пришлите только лист с себестоимостью, без картинок и лишних листов."
    )


def result_text(result: costs.Upload) -> str:
    """Что получилось из присланного файла, человеческим языком."""
    lines = [f"Принято строк: {result.saved}."]
    if result.skipped:
        lines.append(f"Пропущено: {result.skipped}.")
    if result.blank:
        lines.append(f"Из них без себестоимости: {result.blank}.")
    if result.problems:
        lines.append("")
        lines.append("Что не так:")
        for problem in result.problems[:PROBLEMS_SHOWN]:
            lines.append(f"строка {problem.row}: {problem.reason}")
        hidden = len(result.problems) - PROBLEMS_SHOWN
        if hidden > 0:
            lines.append(f"и ещё строк с ошибками: {hidden}.")
        lines.append("")
        lines.append("Поправьте эти строки и пришлите файл ещё раз.")
    elif result.saved:
        lines.append("Себестоимость сохранена, можно считать прибыль.")
    return "\n".join(lines)


async def costs_command(
    update: Update, context: Any, *, path: str | Path | None = None
) -> None:
    """`/costs`: ставит сборку шаблона в очередь и молчит.

    Ответ «принято, пришлю, когда будет готово» приходит от очереди, второй
    раз обещать то же самое незачем.
    """
    message = update.effective_message
    client_id = client_id_of(update, path)
    if client_id is None or message is None:
        return
    if db.repo(client_id, path).one("wb_tokens") is None:
        await message.reply_text(texts.NEED_CONNECT, parse_mode=ParseMode.HTML)
        return
    costs.request_template(client_id, path=path)


async def costs_document(
    update: Update, context: Any, *, path: str | Path | None = None
) -> None:
    """Присланный файл: проверить, сохранить хорошие строки, отчитаться."""
    message = update.effective_message
    client_id = client_id_of(update, path)
    if message is None or client_id is None:
        return
    document = getattr(message, "document", None)
    if document is None:
        return

    name = str(getattr(document, "file_name", "") or "")
    if not name.lower().endswith(".xlsx"):
        await message.reply_text(NOT_XLSX, parse_mode=ParseMode.HTML)
        return

    limit = costs.max_upload_bytes()
    size = int(getattr(document, "file_size", 0) or 0)
    if size > limit:
        await message.reply_text(too_big_text(size, limit))
        return

    try:
        handle = await context.bot.get_file(document.file_id)
        data = bytes(await handle.download_as_bytearray())
    except Exception:  # noqa: BLE001 - молчание Telegram не вина селлера
        logger.exception("не удалось скачать файл себестоимости")
        await message.reply_text(texts.SOMETHING_WENT_WRONG)
        return

    try:
        result = costs.save_upload(client_id, data, max_bytes=limit, path=path)
    except costs.BadFile as error:
        await message.reply_text(str(error))
        return

    await message.reply_text(result_text(result))


def make_sender(app: Any, path: str | Path | None = None):
    """Чем очередь отдаёт готовую книгу: перевод id клиента в чат Telegram."""

    async def send(client_id: int, filename: str, data: bytes) -> None:
        row = db.admin_repo(path).client(client_id)
        if row is None:
            logger.warning("некому отправить шаблон: клиента %s нет", client_id)
            return
        await app.bot.send_document(
            chat_id=int(row["telegram_id"]),
            document=data,
            filename=filename,
            caption=CAPTION,
        )

    return send


def register(app, *, path: str | Path | None = None) -> None:
    """Сам себя регистрирует: bot/app.py никто не трогает."""
    # Единственное место, где задача попадает в очередь: импорт модуля сам
    # по себе ничего не регистрирует.
    queue.register(costs.TASK_KIND, costs.template_task)
    costs.set_sender(make_sender(app, path))
    app.add_handler(CommandHandler("costs", costs_command))
    app.add_handler(
        MessageHandler(filters.Document.ALL, costs_document), group=DOCUMENT_GROUP
    )
