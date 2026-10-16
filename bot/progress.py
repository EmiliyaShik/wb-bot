"""Рассказ о ходе долгой работы одним живым сообщением.

Выгрузка года идёт страницами по минуте каждая, и полтора часа клиент видел
только «принято». Молчание неотличимо от поломки: человек жмёт кнопку второй
раз, очередь отвечает «уже идёт», и доверия это не прибавляет.

Сообщение всегда одно и то же: первая весть отправляется, следующие правят её
на месте. Поток из полусотни сообщений «идёт работа» вытеснил бы из переписки
всё остальное, а редактирование оставляет в чате ровно одну строку о работе.

Редкость правок держит пауза: Telegram не любит частых правок одного
сообщения, а страницы на коротком периоде приходят пачкой. Последняя весть
(`final`) идёт мимо паузы: она закрывает рассказ, и потерять её нельзя.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Callable

from core import db

logger = logging.getLogger(__name__)

# Пауза между правками одного сообщения. Страница отчёта о реализации приходит
# раз в минуту, так что на длинной выгрузке пауза не мешает вовсе; она нужна
# коротким периодам, где страницы приезжают подряд.
MIN_GAP_SEC = 20.0


def make_reporter(
    app: Any,
    path: str | Path | None = None,
    *,
    min_gap_sec: float = MIN_GAP_SEC,
    clock: Callable[[], float] = time.monotonic,
):
    """Возвращает `report(client_id, text, final=False)` для агента.

    Сбой рассказа не должен ронять работу, о которой он рассказывает, поэтому
    отсюда наружу не летит ничего: ни молчащий Telegram, ни удалённое
    клиентом сообщение, ни неизвестный клиент.
    """
    live: dict[int, tuple[int, float]] = {}

    async def report(client_id: int, text: str, final: bool = False) -> None:
        row = db.admin_repo(path).client(int(client_id))
        if row is None:
            return
        chat_id = int(row["telegram_id"])
        known = live.get(int(client_id))
        now = clock()
        if known is None:
            try:
                message = await app.bot.send_message(chat_id=chat_id, text=text)
            except Exception:  # noqa: BLE001 - рассказ не важнее работы
                logger.exception("не удалось начать рассказ о ходе работы")
                return
            if not final:
                live[int(client_id)] = (int(message.message_id), now)
            return
        message_id, said_at = known
        if not final and now - said_at < min_gap_sec:
            return
        try:
            await app.bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=text
            )
        except Exception:  # noqa: BLE001 - сообщение могли удалить, это не авария
            logger.exception("не удалось поправить рассказ о ходе работы")
        if final:
            live.pop(int(client_id), None)
        else:
            live[int(client_id)] = (message_id, now)

    return report
