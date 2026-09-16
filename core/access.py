"""Право пользоваться модулем.

Здесь одна дверь: grant_access(). Через неё проходят все способы включить
доступ - счёт, ручная выдача владельцем, пробный период, и так же пойдёт
Tribute, если его когда-нибудь включат. Другого способа записать доступ в
базу в системе нет, и это главное свойство модуля: пока дверь одна, правило
«повторный платёж ничего не продлевает» держится само собой, а не проверкой
в каждом месте, откуда доступ включают.

Сроки и цены в коде не заданы: льготный период, длительность пробного и
стоимость приходят из core.config.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core import audit, config, db

# Состояния. active -> (срок вышел) grace -> off, отдельно paused при 401.
ACTIVE = "active"
GRACE = "grace"
OFF = "off"
PAUSED = "paused"
# Скрытый модуль: он есть в конфиге, но не продаётся (visible = false).
HIDDEN = "hidden"

# Состояния, в которых модуль работает.
WORKING = (ACTIVE, GRACE, PAUSED)

_STAMP = "%Y-%m-%d %H:%M:%S"


@dataclass(frozen=True)
class Access:
    """Доступ клиента к одному модулю на конкретный момент времени."""

    client_id: int
    module: str
    state: str
    until: datetime | None = None
    source: str = "manual"
    paused_at: datetime | None = None
    duplicate: bool = False
    # Момент, на который посчитано состояние. Остаток дней считается от него же,
    # иначе объект противоречил бы сам себе: состояние на одну дату, дни на другую.
    as_of: datetime | None = None

    @property
    def works(self) -> bool:
        return self.state in WORKING

    @property
    def days_left(self) -> int:
        """Сколько полных суток осталось на тот же момент, что и состояние."""
        if self.until is None or not self.works:
            return 0
        left = self.until - (self.as_of or _now())
        return max(0, left.days)


def _now(moment: datetime | None = None) -> datetime:
    """Текущий момент в UTC. Тесты передают свой, чтобы не зависеть от часов."""
    if moment is None:
        return datetime.now(timezone.utc)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _stamp(moment: datetime) -> str:
    return _now(moment).strftime(_STAMP)


def _parse(raw: Any) -> datetime | None:
    if not raw:
        return None
    text = str(raw).strip().replace("T", " ")
    for fmt in (_STAMP, "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def grace_days() -> int:
    """Льготный период после окончания срока. Из конфига, не из кода."""
    return int(config.settings()["access"].get("grace_days", 0))


def _state_of(
    until: datetime | None, paused_at: datetime | None, moment: datetime
) -> str:
    """Состояние по сроку и паузе. Единственное место, где оно вычисляется."""
    if paused_at is not None:
        return PAUSED
    if until is None:
        return OFF
    if moment <= until:
        return ACTIVE
    if moment <= until + timedelta(days=grace_days()):
        return GRACE
    return OFF


def _row_to_access(client_id: int, module: str, row: Any, moment: datetime) -> Access:
    if row is None:
        return Access(client_id=client_id, module=module, state=OFF, as_of=moment)
    until = _parse(row["until"])
    paused_at = _parse(row["paused_at"])
    return Access(
        client_id=client_id,
        module=module,
        state=_state_of(until, paused_at, moment),
        until=until,
        source=str(row["source"] or "manual"),
        paused_at=paused_at,
        as_of=moment,
    )


def _read(client_id: int, module: str, moment: datetime, path) -> Access:
    row = db.repo(client_id, path).one("module_access", module=module)
    return _row_to_access(client_id, module, row, moment)


def grant_access(
    client_id: int,
    module: str,
    days: int,
    payment_ref: str,
    method: str = "manual",
    actor: str = "system",
    *,
    now: datetime | None = None,
    path: str | Path | None = None,
) -> Access:
    """Единственный способ включить или продлить доступ.

    Повтор с тем же payment_ref не продлевает ничего: возвращает уже выданный
    доступ и пишет в журнал строку «дубль». Проверка идёт по журналу клиента -
    там же, где лежит выдача, так что забыть её при новом способе оплаты
    невозможно: другого пути к таблице доступов просто нет.

    Срок считается от большей из двух дат - сегодня или текущее «до»: продление
    не сжигает уже оплаченные дни.
    """
    module = str(module)
    if module not in config.modules():
        raise KeyError(f"нет такого модуля: {module}")
    days = int(days)
    if days < 1:
        raise ValueError("доступ выдаётся минимум на сутки")
    payment_ref = str(payment_ref).strip()
    if not payment_ref:
        raise ValueError("payment_ref обязателен: без него дубль не поймать")

    moment = _now(now)
    repo = db.repo(client_id, path)

    seen = repo.one("access_log", payment_ref=payment_ref, action="grant")
    if seen is not None:
        repo.insert(
            "access_log",
            module=str(seen["module"]),
            action="duplicate",
            days=days,
            payment_ref=payment_ref,
            method=method,
            actor=actor,
            at=_stamp(moment),
        )
        audit.log(
            "access.duplicate",
            client_id,
            f"дубль платежа {payment_ref}, модуль {seen['module']}: доступ не продлён",
            path=path,
        )
        current = _read(client_id, str(seen["module"]), moment, path)
        return replace(current, duplicate=True)

    current = _read(client_id, module, moment, path)
    base = current.until if current.until and current.until > moment else moment
    until = base + timedelta(days=days)

    repo.upsert(
        "module_access",
        {"module": module},
        until=_stamp(until),
        source=method,
        state=ACTIVE,
        updated_at=_stamp(moment),
    )
    repo.insert(
        "access_log",
        module=module,
        action="grant",
        days=days,
        payment_ref=payment_ref,
        method=method,
        actor=actor,
        at=_stamp(moment),
    )
    audit.log(
        "access.grant",
        client_id,
        f"модуль {module} включён на {days} дн. по платежу {payment_ref}",
        path=path,
    )
    return _read(client_id, module, moment, path)


def revoke_access(
    client_id: int,
    module: str,
    reason: str = "",
    actor: str = "owner",
    *,
    payment_ref: str | None = None,
    now: datetime | None = None,
    path: str | Path | None = None,
) -> Access:
    """Отмена доступа: обратная сторона grant_access и такая же единственная.

    Гасит модуль и пишет в access_log строку revoke рядом с grant, чтобы
    владелец видел обе стороны истории. Причина ложится в колонку method: у
    выдачи там основание «как оплачено», у отмены основание «почему снято».

    Идемпотентность здесь стоит не на номере платежа, а на состоянии: гасить
    нечего - значит, и записи нет. Это строже, чем проверка по payment_ref:
    два разных основания не погасят один доступ дважды, а возврат по тому же
    платежу остаётся одним возвратом. payment_ref нужен, только чтобы связать
    отмену с оплатой в журнале.

    Отмена не открывает платёж заново: grant_access с тем же payment_ref
    по-прежнему считается дублем. После возврата новая выдача идёт по новому
    платежу, иначе возврат и повторная оплата стали бы неразличимы.
    """
    module = str(module)
    moment = _now(now)
    repo = db.repo(client_id, path)
    current = _read(client_id, module, moment, path)

    if current.until is None and current.paused_at is None:
        return current

    repo.upsert(
        "module_access",
        {"module": module},
        until=None,
        state=OFF,
        paused_at=None,
        updated_at=_stamp(moment),
    )
    repo.insert(
        "access_log",
        module=module,
        action="revoke",
        days=current.days_left,
        payment_ref=payment_ref,
        method=reason or "revoke",
        actor=actor,
        at=_stamp(moment),
    )
    audit.log(
        "access.revoke",
        client_id,
        f"модуль {module} отключён ({reason or 'без причины'}), "
        f"сгорело оплаченных дней: {current.days_left}",
        level="warning",
        path=path,
    )
    return _read(client_id, module, moment, path)


def access_of(
    client_id: int,
    module: str,
    *,
    now: datetime | None = None,
    path: str | Path | None = None,
) -> Access:
    """Доступ к одному модулю как он есть сейчас, без учёта пакета «всё сразу»."""
    return _read(client_id, str(module), _now(now), path)


def packages() -> tuple[str, ...]:
    """Модули-пакеты: те, у кого includes = "*". Пакет открывает всё видимое.

    Имя пакета в коде не зашито: когда в конфиге появится второй такой модуль,
    он заработает сам. И когда откроются ads и funnel, они войдут в пакет без
    доплаты - именно потому, что список считается из конфига каждый раз, а не
    записан в базу при покупке.
    """
    return tuple(
        name for name, info in config.modules().items() if info.includes == "*"
    )


def has_access(
    client_id: int,
    module: str,
    *,
    now: datetime | None = None,
    path: str | Path | None = None,
) -> bool:
    """Работает ли модуль у клиента: active, grace и paused считаются рабочими."""
    module = str(module)
    moment = _now(now)
    if _read(client_id, module, moment, path).works:
        return True
    info = config.modules().get(module)
    if info is None or not info.visible or module in packages():
        return False
    return any(_read(client_id, name, moment, path).works for name in packages())


def pause(
    client_id: int,
    *,
    reason: str = "token_401",
    now: datetime | None = None,
    path: str | Path | None = None,
) -> int:
    """Останавливает счётчик срока у всех модулей клиента. Модули продолжают работать.

    Нужно при 401 от WB: токен отозвали, бот ничего не может посчитать, и
    оплаченные дни в это время гореть не должны. Возвращает число модулей,
    которые встали на паузу.
    """
    moment = _now(now)
    repo = db.repo(client_id, path)
    touched = 0
    for row in repo.rows("module_access"):
        if row["paused_at"] or not row["until"]:
            continue
        repo.update(
            "module_access",
            {"module": row["module"]},
            paused_at=_stamp(moment),
            state=PAUSED,
            updated_at=_stamp(moment),
        )
        repo.insert(
            "access_log",
            module=str(row["module"]),
            action="pause",
            actor=reason,
            at=_stamp(moment),
        )
        touched += 1
    if touched:
        db.admin_repo(path).set_client_fields(client_id, paused_since=_stamp(moment))
        audit.log(
            "access.pause",
            client_id,
            f"доступ на паузе ({reason}), модулей: {touched}",
            level="warning",
            path=path,
        )
    return touched


def resume(
    client_id: int,
    *,
    now: datetime | None = None,
    path: str | Path | None = None,
) -> int:
    """Снимает паузу и сдвигает срок каждого модуля на её длительность.

    Сдвиг считается по каждому модулю от его собственного paused_at, а не от
    общей отметки клиента: модуль, купленный во время паузы, не получит чужих
    дней.
    """
    moment = _now(now)
    repo = db.repo(client_id, path)
    touched = 0
    for row in repo.rows("module_access"):
        paused_at = _parse(row["paused_at"])
        if paused_at is None:
            continue
        until = _parse(row["until"])
        shifted = (until + (moment - paused_at)) if until else None
        repo.update(
            "module_access",
            {"module": row["module"]},
            until=_stamp(shifted) if shifted else None,
            paused_at=None,
            state=_state_of(shifted, None, moment),
            updated_at=_stamp(moment),
        )
        repo.insert(
            "access_log",
            module=str(row["module"]),
            action="resume",
            days=max(0, (moment - paused_at).days),
            actor="system",
            at=_stamp(moment),
        )
        touched += 1
    if touched:
        db.admin_repo(path).set_client_fields(client_id, paused_since=None)
        audit.log(
            "access.resume",
            client_id,
            f"пауза снята, срок сдвинут у модулей: {touched}",
            path=path,
        )
    return touched


class TrialDenied(Exception):
    """Пробный период не выдан. Причина в reason, текст подбирает хендлер.

    Причины: no_seller - кабинет WB ещё не подключён, и привязать пробный
    период не к чему; used - на этот кабинет пробный уже брали; not_sold -
    модуль скрыт и не продаётся; too_many - в конфиге разрешено меньше
    модулей, чем просят.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def trial_days() -> int:
    """Длительность пробного периода. Из конфига."""
    return int(config.settings()["trial"].get("days", 0))


def trial_modules() -> int:
    """Сколько модулей можно взять пробно. Из конфига."""
    return int(config.settings()["trial"].get("modules", 1))


def start_trial(
    client_id: int,
    module: str,
    *,
    now: datetime | None = None,
    path: str | Path | None = None,
) -> Access:
    """Пробный период: столько дней, сколько сказано в конфиге, один раз на кабинет.

    Привязка к seller_id, а не к Telegram-аккаунту: второй аккаунт с тем же
    кабинетом пробный уже не получит. Включение идёт через ту же дверь
    grant_access, поэтому пробный период виден в журнале наравне с оплатой.
    """
    module = str(module)
    info = config.modules().get(module)
    if info is None or not info.visible:
        raise TrialDenied("not_sold")

    admin = db.admin_repo(path)
    row = admin.client(client_id)
    seller_id = str((row["seller_id"] if row else "") or "").strip()
    if not seller_id:
        raise TrialDenied("no_seller")
    if len(admin.trials(seller_id)) >= trial_modules():
        raise TrialDenied("used")
    if not admin.start_trial(seller_id, module):
        raise TrialDenied("used")

    return grant_access(
        client_id,
        module,
        trial_days(),
        f"trial:{seller_id}:{module}",
        "trial",
        "system",
        now=now,
        path=path,
    )


def status(
    client_id: int,
    *,
    now: datetime | None = None,
    path: str | Path | None = None,
    include_hidden: bool = False,
) -> list[Access]:
    """Таблица «модуль - состояние - до какой даты» по всем модулям конфига.

    Скрытые модули попадают в список только по просьбе (include_hidden) и
    получают состояние hidden: их не купить, и показывать их как выключенные
    значило бы предлагать то, чего нет.
    """
    moment = _now(now)
    covered = any(_read(client_id, name, moment, path).works for name in packages())
    out: list[Access] = []
    for name, info in config.modules().items():
        if not info.visible:
            if include_hidden:
                out.append(
                    Access(
                        client_id=client_id, module=name, state=HIDDEN, as_of=moment
                    )
                )
            continue
        item = _read(client_id, name, moment, path)
        if not item.works and covered and name not in packages():
            package = next(
                (
                    _read(client_id, pkg, moment, path)
                    for pkg in packages()
                    if _read(client_id, pkg, moment, path).works
                ),
                None,
            )
            if package is not None:
                item = replace(package, module=name, source=package.module)
        out.append(item)
    return out
