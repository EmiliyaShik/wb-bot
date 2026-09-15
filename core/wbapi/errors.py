"""Ошибки WB, переведённые в классы.

401 и 403 разведены намеренно и смешивать их нельзя:
401 это проблема с самим токеном (отозван, просрочен, повреждён) и по ТЗ
означает паузу модулей; 403 это валидный токен без нужных прав, и пауза тут
была бы наказанием ни за что. Разные классы гарантируют, что вызывающий
не обработает их одинаково по невнимательности.

Ни в одном сообщении не появляется токен: тексты собираются из тела ответа
и пути, а тело ответа проходит через core.audit.redact.

Осторожно с именем WBApiError: такой класс есть и в legacy/card.py, но тот про
витрину card.wb.ru, а здесь про API продавца. Это разные исключения, и ловить
надо то, которое из того же модуля, что и вызов.
"""

from __future__ import annotations


class WBError(Exception):
    """Любая беда со стороны WB."""

    def __init__(self, message: str, *, status: int | None = None, path: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.path = path


class WBTokenFormatError(WBError):
    """Строка не похожа на токен Wildberries. Сети тут не было."""


class WBAuthError(WBError):
    """401: токен не принят. Отозван, просрочен, повреждён или сменился кабинет."""


class WBTokenMissing(WBAuthError):
    """Кабинет не подключён: токена в базе нет."""


class WBForbiddenError(WBError):
    """403: токен валиден, но прав не хватает.

    Поле category говорит, какой категории не хватило, если её удалось
    вывести из хоста. Это не повод ставить модули на паузу.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = 403,
        path: str = "",
        category: str = "",
    ) -> None:
        super().__init__(message, status=status, path=path)
        self.category = category


class WBRateLimited(WBError):
    """429: слишком часто. retry_after в секундах, если WB его назвал."""

    def __init__(
        self,
        message: str,
        *,
        retry_after: float | None = None,
        status: int | None = 429,
        path: str = "",
    ) -> None:
        super().__init__(message, status=status, path=path)
        self.retry_after = retry_after


class WBUnavailable(WBError):
    """5xx, таймаут, обрыв связи. Повторяем позже."""


class WBApiError(WBError):
    """Остальные ответы WB: 400, 404 и прочее, что не лечится повтором."""
