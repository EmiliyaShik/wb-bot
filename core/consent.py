"""Три согласия, которые человек даёт до работы с ботом.

Их именно три, и они разной природы. Два обязательных: **оферта** это
договор, по которому бот оказывает услугу, и **обработка персональных
данных** это разрешение хранить то, что человек о себе оставил. Третье,
**новости и специальные предложения**, необязательное: это реклама, а
реклама по сетям связи разрешена только с отдельно выраженного согласия
(статья 18 закона «О рекламе»).

Отсюда главное правило этого модуля: **третье согласие нельзя смешивать с
первыми двумя**. Одна кнопка «со всем согласен» юридически обесценивает
рекламное согласие целиком, потому что человек не мог отказаться от рекламы,
не отказавшись от услуги. Поэтому кнопки три, и отказ от третьей ничему не
мешает.

Хранится всё в `clients.settings`, как налог и напоминания: схема
применяется при каждом старте и каждый её оператор обязан быть
идемпотентным, а `ALTER TABLE` таким не бывает. Это не обходной путь, а
устоявшийся в проекте способ добавить состояние клиента.

Рекламное согласие своего места тут не заводит: им уже владеет
`core.leads`, и там же живут счёт напоминаний, пауза между ними, `/stop` и
срок хранения отказа. Две копии одного согласия разошлись бы в первый же
день, поэтому этот модуль просто зовёт соседа. Названия разные («новости и
предложения» против «напоминаний») намеренно: человеку обещано одно и то
же, но говорить с ним надо его словами, а не именем таблицы.

У каждого согласия записывается **адрес документа и дата**. Дату спросят,
если согласие оспорят, а адрес отвечает на вопрос, с чем именно человек
согласился: документ по ссылке меняется, и запись «согласился с офертой»
без ссылки не доказывает ничего.

Старое согласие с офертой из таблицы `consents` засчитывается: его давали
при подключении кабинета, и переспрашивать человека, который уже работает,
значит пугать его на ровном месте. Поэтому `given` смотрит в оба места, а
`missing` у такого человека вернёт только недостающее.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any

from core import clients, db, leads

logger = logging.getLogger(__name__)

# Ключ в clients.settings. Соседи по словарю чужие, поэтому читаем и пишем
# весь словарь целиком, как велит core.clients.save_settings.
SETTINGS_KEY = "consent"

# Виды согласий. Строки, а не числа: они лежат в базе и читаются глазами.
OFFER = "offer"
PERSONAL_DATA = "personal_data"
NEWS = "news"

# Обязательные. Без них бот не работает, кроме карточки товара по артикулу.
REQUIRED = (OFFER, PERSONAL_DATA)


@dataclass(frozen=True)
class Given:
    """Одно данное согласие: когда и с каким документом."""

    kind: str
    at: date | None
    document: str = ""


def _state(client_id: int, path: str | Path | None) -> dict[str, Any]:
    raw = clients.settings_of(client_id, path=path).get(SETTINGS_KEY)
    return dict(raw) if isinstance(raw, dict) else {}


def _save(client_id: int, state: dict[str, Any], path: str | Path | None) -> None:
    data = clients.settings_of(client_id, path=path)
    data[SETTINGS_KEY] = state
    clients.save_settings(client_id, data, path=path)


def _today(today: date | None = None) -> date:
    """Дата согласия в UTC, как у соседней записи про оферту.

    Московское время было бы ближе человеку, но согласие с офертой пишет
    `core.clients` по UTC, а два согласия, данные в одну секунду, обязаны
    показывать одну дату: на разные даты одного нажатия первым делом укажет
    тот, кто согласие оспаривает.
    """
    return today or datetime.now(timezone.utc).date()


def _as_date(raw: Any) -> date | None:
    try:
        return date.fromisoformat(str(raw)[:10])
    except (TypeError, ValueError):
        return None


def document_of(kind: str) -> str:
    """Адрес документа, с которым соглашаются. Пустая строка это «готовится»."""
    if kind == OFFER:
        return clients.offer_url()
    if kind == PERSONAL_DATA:
        return clients.privacy_url()
    return ""


def _offer_record(client_id: int, path: str | Path | None) -> Given | None:
    """Согласие с офертой лежит в таблице `consents`, а не в настройках.

    Своё место у него было и до этого модуля: туда же пишет подключение
    кабинета. Заводить вторую запись значило бы однажды получить две разные
    даты одного согласия, а спорная дата хуже отсутствующей.
    """
    if not clients.has_consent(client_id, path=path):
        return None
    rows = db.repo(client_id, path).rows("consents", order_by="id DESC", limit=1)
    row = rows[0] if rows else None
    at = _as_date(row["agreed_at"]) if row is not None else None
    url = str(row["offer_url"] or "") if row is not None else ""
    return Given(kind=OFFER, at=at, document=url)


def given(client_id: int, kind: str, *, path: str | Path | None = None) -> Given | None:
    """Данное согласие или None. У новостей отказ это не согласие."""
    if kind == NEWS:
        if not leads.allowed(client_id, path=path):
            return None
        return Given(kind=NEWS, at=leads.answered_at(client_id, path=path))
    if kind == OFFER:
        return _offer_record(client_id, path)
    record = _state(client_id, path).get(kind)
    if isinstance(record, dict):
        return Given(
            kind=kind,
            at=_as_date(record.get("at")),
            document=str(record.get("document") or ""),
        )
    return None


def has(client_id: int, kind: str, *, path: str | Path | None = None) -> bool:
    return given(client_id, kind, path=path) is not None


def answered_news(client_id: int, *, path: str | Path | None = None) -> bool:
    """Ответил ли человек про новости хоть что-нибудь. Отказ это тоже ответ."""
    return leads.asked(client_id, path=path)


def missing(client_id: int, *, path: str | Path | None = None) -> tuple[str, ...]:
    """Каких обязательных согласий не хватает. Пустой кортеж значит «можно»."""
    return tuple(kind for kind in REQUIRED if not has(client_id, kind, path=path))


def ready(client_id: int, *, path: str | Path | None = None) -> bool:
    """Можно ли пускать человека в бота."""
    return not missing(client_id, path=path)


def accept(
    client_id: int,
    kind: str,
    *,
    today: date | None = None,
    path: str | Path | None = None,
) -> None:
    """Записывает согласие вместе с датой и адресом документа."""
    if kind == NEWS:
        leads.set_choice(client_id, True, today=today, path=path)
        return
    if kind not in REQUIRED:
        raise ValueError(f"неизвестное согласие: {kind}")
    if kind == OFFER:
        # Та же дверь, которой пользуется подключение кабинета. Повтор
        # безопасен: записей будет две, а читается последняя. Дату передаём
        # насквозь: иначе два согласия одного нажатия получили бы разные
        # даты, и первым на это укажет тот, кто согласие оспаривает.
        moment = (
            datetime.combine(today, time(12, 0), tzinfo=timezone.utc)
            if today is not None
            else None
        )
        clients.record_consent(client_id, now=moment, path=path)
        return
    state = _state(client_id, path)
    state[kind] = {
        "at": _today(today).isoformat(),
        "document": document_of(kind),
    }
    _save(client_id, state, path)


def refuse_news(
    client_id: int,
    *,
    today: date | None = None,
    path: str | Path | None = None,
) -> None:
    """Отказ от новостей. Обязательные согласия отозвать так нельзя.

    Отзыв оферты и согласия на обработку данных это не кнопка: у первого
    есть договор, у второго удаление данных, и для него в боте своя команда
    `/disconnect`.
    """
    leads.set_choice(client_id, False, today=today, path=path)
