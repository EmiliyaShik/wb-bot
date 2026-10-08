"""Кабинет клиента: согласие с офертой, токен WB, отключение.

Здесь нет ни одного текста для селлера и ни одной кнопки: это слой данных,
тексты живут рядом со своим хендлером, в bot/handlers/connect.py.

Токен проходит через модуль ровно один раз, по дороге в шифротекст. Обратно
он не возвращается ни значением, ни полем, ни текстом ошибки: наружу выходит
только TokenInfo, в котором самой строки нет. Репозиторий публичный, и любая
строка отсюда может оказаться в журнале.

Разбор JWT и запросы к WB тут не повторяются: это дело core.wbapi.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from core import access, audit, config, crypto, db, wbapi

# Пять категорий, которые бот просит у селлера. Их состав не выдумывается
# здесь: конфиг заодно знает, что перестанет работать без каждой из них.
REQUIRED_CATEGORIES: tuple[str, ...] = (
    "statistics",
    "finance",
    "analytics",
    "promotion",
    "content",
)

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# Персональный токен. Спецификация просит именно его: часть методов WB
# другим типам не отвечает, а тестовый токен это песочница без ваших продаж.
PERSONAL_ACC = 3


class ConsentRequired(Exception):
    """Токен прислали до согласия с офертой. Принимать его нельзя."""


class TokenExpired(Exception):
    """У токена уже кончился срок. Пробный запрос делать незачем."""


class DisconnectIncomplete(RuntimeError):
    """После удаления клиента в базе что-то осталось. Молчать об этом нельзя."""


def _now(moment: datetime | None = None) -> datetime:
    """Момент в UTC. Наивная дата считается UTC, как и в core.access.

    Иначе вычитание наивного и осведомлённого времени в days_left упало бы,
    и напоминания о сроке токена умерли бы тихо.
    """
    if moment is None:
        return datetime.now(timezone.utc)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(TIME_FORMAT)


# --- оферта ---


def offer_url() -> str:
    """Ссылка на оферту. Пустая строка означает, что текста ещё нет."""
    return config.env("OFFER_URL")


def offer_ready() -> bool:
    """Опубликована ли оферта. Пока нет, кнопка согласия всё равно работает."""
    return bool(offer_url())


def record_consent(
    client_id: int,
    *,
    now: datetime | None = None,
    path: str | Path | None = None,
) -> datetime:
    """Записывает факт и время согласия вместе со ссылкой на оферту.

    Ссылки пока может не быть: владелец ещё готовит текст. Тогда согласие
    всё равно сохраняется, но в журнал уходит предупреждение, иначе про
    незакрытую оферту забудут ровно до первого спора с клиентом.
    """
    moment = _now(now)
    link = offer_url()
    db.repo(client_id, path).insert(
        "consents", offer_url=link, agreed_at=_stamp(moment)
    )
    if not link:
        audit.log(
            "consent",
            None,
            "Клиент согласился с офертой, но переменная OFFER_URL пуста: "
            "текста оферты пока нет. Согласие записано со ссылкой пустой строкой.",
            level="warning",
            path=path,
        )
    return moment


def has_consent(client_id: int, *, path: str | Path | None = None) -> bool:
    """Нажимал ли клиент кнопку согласия хоть раз."""
    return db.repo(client_id, path).count("consents") > 0


# --- подключение кабинета ---


@dataclass(frozen=True)
class Connected:
    """Чем закончилось подключение. Строки токена тут нет и быть не может."""

    client_id: int
    info: wbapi.TokenInfo
    missing: tuple[str, ...] = ()
    replaced: bool = False
    resumed: int = 0
    # Пробный запрос к WB делается не всегда: бюджет на неподключённых общий
    # и маленький. probed=False означает «ключ разобран, но связь не проверена»,
    # и обещать клиенту проверку тогда нельзя, её надо доделать в фоне.
    probed: bool = True


def connected(client_id: int, *, path: str | Path | None = None) -> bool:
    """Подключён ли кабинет: есть ли сохранённый токен."""
    return db.repo(client_id, path).count("wb_tokens") > 0


def is_personal(info: wbapi.TokenInfo) -> bool:
    """Тот ли это тип токена, на который рассчитан бот."""
    return int(info.acc) == PERSONAL_ACC and not info.is_test


def missing_categories(
    info: wbapi.TokenInfo, needed: tuple[str, ...] = REQUIRED_CATEGORIES
) -> tuple[str, ...]:
    """Каких из пяти нужных категорий у токена нет."""
    return info.missing(needed)


async def connect(
    client_id: int,
    raw: str,
    *,
    http: httpx.AsyncClient | None = None,
    path: str | Path | None = None,
    now: datetime | None = None,
) -> Connected:
    """Принимает токен: согласие, разбор, пробный запрос, шифротекст.

    Порядок именно такой. Согласие проверяется до всего остального: строка
    токена не должна попасть даже в шифротекст, пока клиент не согласился.
    """
    if not has_consent(client_id, path=path):
        raise ConsentRequired()

    moment = _now(now)
    # check_token_live, а не check_token: нужен не только разбор ключа, но и
    # ответ на вопрос, дошло ли дело до пробного запроса. От этого зависит,
    # что бот пообещает клиенту.
    check = await wbapi.check_token_live(raw, http=http, path=path, client_id=client_id)
    info = check.info
    if info.is_expired(moment):
        raise TokenExpired()

    repo = db.repo(client_id, path)
    was = connected(client_id, path=path)
    repo.upsert(
        "wb_tokens",
        {},
        ciphertext=crypto.encrypt(raw),
        exp=_stamp(info.expires_at),
        scopes=",".join(info.categories),
        read_only=int(info.read_only),
        last_ok_at=_stamp(moment),
        last_401_at=None,
    )
    # Токен сменился, значит и срок другой: отметки о прошлых напоминаниях
    # больше ничего не значат и мешали бы предупредить о новом сроке.
    # Это состояние, а не журнал: записи о самих отправках остаются на месте.
    reset_reminders(client_id, path=path)
    db.admin_repo(path).set_client_fields(client_id, seller_id=info.sid)
    # Кабинет снова на связи, значит пауза после 401 больше не нужна.
    # Снимает её access: он же следит, чтобы оплаченные дни не сгорели.
    resumed = access.resume(client_id, now=moment, path=path)
    audit.log(
        "connect",
        client_id,
        "Кабинет подключён. Категорий у токена: "
        f"{len(info.categories)}, только на чтение: {'да' if info.read_only else 'нет'}.",
        path=path,
    )
    return Connected(
        client_id=client_id,
        info=info,
        missing=missing_categories(info),
        replaced=was,
        resumed=resumed,
        probed=check.probed,
    )


# --- что делать, когда Wildberries отказал ---


@dataclass(frozen=True)
class Trouble:
    """Разбор отказа WB. Пауза тут только у 401, и это не мелочь.

    401 означает, что токена больше нет: посчитать нечего, и оплаченные дни
    гореть не должны. 403 означает, что токен рабочий, просто у него нет
    категории для одного сервиса: остальные модули продолжают работать,
    и ставить их на паузу значило бы наказать клиента за чужой недосмотр.
    """

    kind: str
    category: str = ""
    paused: int = 0


def on_wb_error(
    client_id: int,
    error: BaseException,
    *,
    now: datetime | None = None,
    path: str | Path | None = None,
) -> Trouble:
    """Решает, что делать с отказом WB, и возвращает разбор для хендлера."""
    moment = _now(now)
    if isinstance(error, wbapi.WBAuthError):
        db.repo(client_id, path).update(
            "wb_tokens", {}, last_401_at=_stamp(moment)
        )
        paused = access.pause(client_id, reason="token_401", now=moment, path=path)
        return Trouble(kind="auth", paused=paused)
    if isinstance(error, wbapi.WBForbiddenError):
        category = str(getattr(error, "category", "") or "")
        audit.log(
            "connect",
            client_id,
            f"Wildberries отказал в доступе: у токена нет категории {category or 'из нужных'}. "
            "Модули на паузу не ставим, токен рабочий.",
            level="warning",
            path=path,
        )
        return Trouble(kind="forbidden", category=category)
    return Trouble(kind="other")


# --- срок жизни токена ---

# За сколько дней до конца срока предупреждать. Ровно два раза, а не каждый
# день: ежедневное напоминание перестают читать к третьему разу.
REMINDER_DAYS: tuple[int, ...] = (14, 3)

# Вид записи в журнале о самом факте отправки. Записи этого вида никогда
# не удаляются: владелец должен видеть, что и когда уходило клиенту.
REMINDER_KIND = "token_reminder"

# Ключ в settings клиента, где лежит рабочее состояние: какие пороги уже
# отработаны по нынешнему токену.
SETTINGS_KEY = "token_reminders"


def token_row(client_id: int, *, path: str | Path | None = None) -> Any:
    """Строка о токене: срок, категории, признак чтения. Шифротекст не трогаем."""
    return db.repo(client_id, path).one("wb_tokens")


def _parse_stamp(raw: Any) -> datetime | None:
    """Отметка времени из базы в осведомлённый момент. Мусор это None."""
    if not raw:
        return None
    try:
        return datetime.strptime(str(raw), TIME_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def token_expires(client_id: int, *, path: str | Path | None = None) -> datetime | None:
    """Когда у токена кончается срок. None, если кабинет не подключён."""
    row = token_row(client_id, path=path)
    return None if row is None else _parse_stamp(row["exp"])


def days_left(client_id: int, *, now: datetime | None = None, path=None) -> int | None:
    """Сколько дней осталось токену. None, если кабинета нет."""
    until = token_expires(client_id, path=path)
    return None if until is None else (until - _now(now)).days


def settings_of(client_id: int, *, path: str | Path | None = None) -> dict:
    """Настройки клиента как словарь. Испорченный JSON это пустой словарь.

    Колонка `clients.settings` одна на весь проект, и ключей в ней уже
    несколько: напоминания о сроке токена, тумблеры рассылок, отметки
    жизненного цикла, целевой ДРР. Разбор общий затем, чтобы правило
    «читаем весь словарь, пишем весь словарь» не пришлось повторять каждому,
    кто заводит свой ключ: чужие ключи иначе затрутся молча.
    """
    row = db.admin_repo(path).client(client_id)
    if row is None:
        return {}
    try:
        data = json.loads(row["settings"] or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(client_id: int, data: dict, *, path: str | Path | None = None) -> None:
    """Кладёт словарь настроек целиком. Читать перед этим обязательно.

    Чтение и запись обязаны стоять в одной синхронной функции, без `await`
    между ними. Это и есть вся защита от потерянной правки: бот однопоточный,
    и корутина не прерывается посреди синхронного кода, поэтому замок не
    нужен. Разнести эти два шага через `await` значит завести гонку, в
    которой правка соседнего ключа пропадёт молча.
    """
    # Пишем весь словарь целиком, но только после чтения: в settings живут
    # и чужие ключи, затирать их нельзя.
    db.admin_repo(path).set_client_fields(
        client_id, settings=json.dumps(data, ensure_ascii=False)
    )


def _settings(client_id: int, path: str | Path | None) -> dict:
    return settings_of(client_id, path=path)


def _save_settings(client_id: int, data: dict, path: str | Path | None) -> None:
    save_settings(client_id, data, path=path)


def reminded(client_id: int, *, path: str | Path | None = None) -> tuple[int, ...]:
    """Пороги, о которых этому клиенту уже говорили по нынешнему токену.

    Это рабочее состояние, и живёт оно в settings клиента. В журнале ему
    не место: журнал это летопись, из него ничего не удаляется, а состояние
    при обмене токена обнуляется.
    """
    raw = _settings(client_id, path).get(SETTINGS_KEY)
    if not isinstance(raw, list):
        return ()
    found = []
    for value in raw:
        try:
            found.append(int(value))
        except (TypeError, ValueError):
            continue
    return tuple(sorted(found, reverse=True))


def reset_reminders(client_id: int, *, path: str | Path | None = None) -> None:
    """Забыть отметки о напоминаниях: у нового токена свой срок."""
    if not reminded(client_id, path=path):
        return
    data = _settings(client_id, path)
    data[SETTINGS_KEY] = []
    _save_settings(client_id, data, path)


def due_reminders(
    *,
    now: datetime | None = None,
    path: str | Path | None = None,
    days: tuple[int, ...] = REMINDER_DAYS,
) -> list[tuple[int, int, int]]:
    """Кому пора напомнить про срок токена: (клиент, осталось дней, порог).

    Условие не «осталось ровно 14», а «порог перейдён, и об этом ещё не
    говорили». Равенство дню теряло бы напоминание при любом простое бота,
    а повторить его было бы уже нечем. Отметка об отправленном напоминании
    лежит в журнале клиента и не даёт сказать одно и то же дважды.

    Сроки читаются одной выборкой: tokens_with_exp() отдаёт пары
    (клиент, срок) сразу по всем, и запроса на каждого клиента тут нет.
    Пороги и отметки остаются здесь: слой данных про них не знает, и знать
    ему незачем. Журнал спрашивается только у тех, кто уже перешёл порог,
    а это единицы из всего списка.
    """
    moment = _now(now)
    thresholds = sorted(int(value) for value in days)
    due: list[tuple[int, int, int]] = []
    for client_id, exp in db.admin_repo(path).tokens_with_exp():
        client_id = int(client_id)
        until = _parse_stamp(exp)
        left = None if until is None else (until - moment).days
        if left is None or left < 0:
            # Срока нет или он уже вышел: про это говорит не напоминание,
            # а 401 от WB, и он же ставит модули на паузу.
            continue
        crossed = [value for value in thresholds if left <= value]
        if not crossed:
            continue
        # Ближайший порог, а не самый дальний: если бот молчал до трёх дней,
        # клиент получит одно сообщение, а не два подряд.
        target = min(crossed)
        if target in reminded(client_id, path=path):
            continue
        due.append((client_id, int(left), target))
    return due


def mark_reminded(
    client_id: int,
    threshold: int,
    *,
    path: str | Path | None = None,
    days: tuple[int, ...] = REMINDER_DAYS,
) -> None:
    """Отмечает, что напоминание ушло. Пороги подальше закрываются заодно.

    Состояние уходит в settings, а в журнал пишется факт отправки. Разные
    места нужны затем, что состояние сбрасывается при обмене токена, а факт
    остаётся навсегда.
    """
    done = set(reminded(client_id, path=path))
    closing = {
        value for value in (int(item) for item in days) if value >= int(threshold)
    }
    if closing <= done:
        return
    data = _settings(client_id, path)
    data[SETTINGS_KEY] = sorted(done | closing, reverse=True)
    _save_settings(client_id, data, path)
    audit.log(
        REMINDER_KIND,
        client_id,
        f"Отправил напоминание о сроке токена, порог {int(threshold)} дн.",
        path=path,
    )


# --- уход клиента ---


def disconnect(client_id: int, *, path: str | Path | None = None) -> dict[str, int]:
    """Физически удаляет токен и все строки клиента. Возвращает, сколько чего.

    Удаляется и сама строка клиента: остаться после ухода не должно ничего,
    включая согласие с офертой. Отчёт нужен, чтобы клиенту было что показать,
    а не чтобы что-то оставить на потом.
    """
    repo = db.repo(client_id, path)
    before = {table: repo.count(table) for table in sorted(db.CLIENT_TABLES)}
    db.admin_repo(path).delete_client(client_id)
    # Пересчёт после удаления, а не обещание каскада: если внешний ключ
    # когда-нибудь окажется не тем, клиент должен узнать об этом сразу.
    left = {table: repo.count(table) for table in before}
    left = {table: count for table, count in left.items() if count}
    if left:
        audit.log(
            "disconnect",
            None,
            f"Удаление клиента прошло не до конца, остались строки: {left}.",
            level="error",
            path=path,
        )
        raise DisconnectIncomplete(str(left))
    return {table: count for table, count in before.items() if count}
