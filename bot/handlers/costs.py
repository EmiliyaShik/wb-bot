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

Здесь же живёт состояние себестоимости и приглашение её внести. Себестоимость
это единственное, чего бот не может взять у Wildberries сам, и без неё прибыль
по артикулам не считается вовсе. Раньше об этом не говорил никто: селлер
подключал кабинет, открывал прибыльность и видел пустоту без объяснения.
Поэтому состояние и приглашение лежат рядом с дорогой, которая это чинит, а
зовут их подключение (`connect`), настройки (`settings`) и меню (`menu`).
"""

from __future__ import annotations

import functools
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import CommandHandler, MessageHandler, filters

from bot import texts
from bot.handlers.tariffs import client_id_of
from bot.texts import Safe, fill
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

# Приглашение внести себестоимость. Оно намеренно говорит и то, что без неё
# работает: «без себестоимости не работает ничего» было бы неправдой и напугало
# бы человека на ровном месте.
INVITE = (
    "📦 <b>Остался один шаг: себестоимость</b>\n\n"
    "Сколько вам обошёлся товар, знаете только вы: Wildberries эту цифру "
    "никому не отдаёт. Поэтому прибыль по артикулам я считаю по вашим числам "
    "и не выдумываю их за вас.\n\n"
    "Что без неё работает, а что нет:\n"
    "• недельная раскладка удержаний <code>/finance</code> и динамика "
    "<code>/dynamics</code> считаются как обычно;\n"
    "• прибыль по артикулам <code>/profit</code> не посчитается совсем, "
    "отчёт придёт пустым.\n\n"
    "Дорога короткая: команда <code>/costs</code> пришлёт таблицу с вашими "
    "артикулами, впишите цену закупки в одну колонку и верните файл обратно."
)

HEAD = "📦 <b>Себестоимость</b>"

# Дальше пять состояний, и каждое говорит правду про своё. Числа стоят после
# двоеточия нарочно: «продавалось 40 артикулов» и «продавался 1 артикул»
# требуют разного склонения, а «Артикулов с продажами: 1» читается одинаково
# при любом числе.
NO_COSTS_NO_SALES = (
    "Не внесена ни по одному артикулу.\n"
    "Пока её нет, прибыль по артикулам (<code>/profit</code>) не считается "
    "совсем. Недельная раскладка расходов и динамика работают как обычно."
)

NO_COSTS_COUNTED = (
    "Не внесена ни по одному артикулу.\n"
    "Артикулов с продажами за последнюю собранную неделю: {total}. "
    "Прибыль по артикулам (<code>/profit</code>) не посчитается ни по одному "
    "из них. Недельная раскладка расходов и динамика работают как обычно."
)

COSTS_NO_SALES = (
    "Внесена. Артикулов с себестоимостью: {known}.\n"
    "Сколько артикулов у вас продаётся, скажу, когда соберётся первый "
    "недельный отчёт Wildberries."
)

PART = (
    "Артикулов с продажами за последнюю собранную неделю: {total}.\n"
    "Себестоимость есть у {covered}. По остальным ({rest}) прибыль не "
    "посчитается: они уйдут в блок «нет себестоимости»."
)

FULL = (
    "Артикулов с продажами за последнюю собранную неделю: {total}.\n"
    "Себестоимость есть у всех, прибыль посчитается по каждому."
)

OTHERS = (
    "Артикулов с продажами за последнюю собранную неделю: {total}.\n"
    "Себестоимость у вас внесена (артикулов: {known}), но ни один из "
    "продававшихся в неё не попал, и прибыль не посчитается. Запросите "
    "свежий шаблон командой <code>/costs</code>: в нём будут ваши нынешние "
    "артикулы."
)


@dataclass(frozen=True)
class Coverage:
    """Состояние себестоимости: что внесено и с чем это сравнивать."""

    # Сколько артикулов вообще имеют себестоимость, включая давно снятые.
    known: int = 0
    # Сколько артикулов продавалось за последнюю собранную неделю.
    selling: int = 0
    # Сколько из продававшихся имеют себестоимость.
    covered: int = 0

    @property
    def empty(self) -> bool:
        """Себестоимости нет вовсе: именно с этим человек упирался в пустоту."""
        return self.known <= 0

    @property
    def counted(self) -> bool:
        """Есть ли с чем сравнивать: собрана ли хоть одна неделя продаж."""
        return self.selling > 0

    @property
    def complete(self) -> bool:
        """Прибыль посчитается по всем, кто продавался."""
        return self.counted and self.covered >= self.selling


def has_costs(client_id: int, *, path: str | Path | None = None) -> bool:
    """Внесена ли себестоимость хоть по одному артикулу.

    Отдельно от `coverage`, потому что дёшево: этого хватает и кнопке меню, и
    решению, показывать ли приглашение после подключения кабинета.
    """
    return db.repo(client_id, path).count("costs") > 0


def coverage(client_id: int, *, path: str | Path | None = None) -> Coverage:
    """Сколько артикулов с себестоимостью и сколько их продавалось.

    Знаменатель это последняя собранная неделя, а не вся история. Причины две.
    Первая: столько же недель берёт `/profit` по умолчанию, и число, названное
    в настройках, совпадёт с тем, что человек увидит в отчёте. Вторая: строки
    отчёта о реализации лежат вместе с исходным JSON Wildberries, и проход по
    году означал бы чтение сотен мегабайт на каждую команду `/settings`.

    Своего разреза по артикулам здесь нет: это перечисление nmId, а не вторая
    трактовка выручки и возвратов, она по-прежнему одна и живёт у агента 1.
    """
    repo = db.repo(client_id, path)
    known = {int(row["nm_id"]) for row in repo.rows("costs")}
    selling: set[int] = set()
    weeks = repo.rows("fin_weeks", order_by="date_from DESC", limit=1)
    if weeks:
        for row in repo.rows("fin_rows", report_id=int(weeks[0]["report_id"])):
            if row["nm_id"] is not None:
                selling.add(int(row["nm_id"]))
    return Coverage(
        known=len(known), selling=len(selling), covered=len(selling & known)
    )


def state_text(state: Coverage) -> Safe:
    """Состояние себестоимости словами. Пять случаев, и все они разные."""
    if not state.counted:
        body = (
            Safe(NO_COSTS_NO_SALES)
            if state.empty
            else fill(COSTS_NO_SALES, known=state.known)
        )
    elif state.empty:
        body = fill(NO_COSTS_COUNTED, total=state.selling)
    elif state.covered <= 0:
        # Себестоимость есть, но от прошлого ассортимента: сказать «не внесена»
        # было бы неправдой, а промолчать значит оставить человека без причины.
        body = fill(OTHERS, total=state.selling, known=state.known)
    elif state.complete:
        body = fill(FULL, total=state.selling)
    else:
        body = fill(
            PART,
            total=state.selling,
            covered=state.covered,
            rest=state.selling - state.covered,
        )
    return Safe(HEAD + "\n" + body)


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
    # Путь к базе передаётся так же, как у остальных хендлеров: без этого шва
    # команда в тестах молча ушла бы в боевую базу.
    app.add_handler(
        CommandHandler("costs", functools.partial(costs_command, path=path))
    )
    app.add_handler(
        MessageHandler(
            filters.Document.ALL, functools.partial(costs_document, path=path)
        ),
        group=DOCUMENT_GROUP,
    )
