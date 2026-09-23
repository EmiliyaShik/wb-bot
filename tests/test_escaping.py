"""Экранирование разметки живёт в одном месте, и туда ходит весь бот.

Сторож про инструмент, а не про отдельный текст. Сам инструмент проверяют
тесты хендлеров: они подсовывают ссылку в имя модуля, в номер платежа, в
last_error, в запись журнала, в наименование организации, в ответ DaData, в
артикул продавца и в название товара с витрины и смотрят, что в сообщении
её нет. Сломайте `bot.texts.fill` нарочно, и падают все они разом: это и
есть доказательство, что копий у инструмента больше нет.

Здесь проверяется то, что теми тестами не проверяется никак: что инструмент
один физически и что мимо него не проходит ни одна отправка с разметкой.
Две одинаковые копии дают зелёные тесты каждая, расходятся молча, и дыра
возвращается в половину бота. Один непереведённый хендлер выглядит так же:
он работает, пока в чужом значении не встретится угловая скобка.

Под присмотром весь пакет `bot` и `legacy`: это вся телеграм-поверхность
бота. Правил четыре, и каждое закрывает свою дорогу к разметке мимо
подстановки.

1. `html.escape` есть только в `bot/texts.py`. Вторая копия начинается
   именно с этой строки.
2. Пометка `Safe` объявлена один раз, там же.
3. Шаблон с тегом заполняется только через `fill`, а не через `str.format`:
   вызовы выглядят одинаково, но `format` увозит чужое значение в сообщение
   как есть. Шаблоны без тегов сюда не попадают: их заполняют для подписи к
   файлу, для надписи на кнопке и для всплывающей подсказки, где разметка не
   разбирается вовсе и экранирование показало бы клиенту `&quot;`.
4. Разметка не собирается f-строкой: `f"<b>{title}</b>"` это та же дыра,
   только записанная короче.
5. Текст, который уходит с `ParseMode.HTML`, собран ботом: это `fill`,
   `Safe`, собственная константа или функция, объявленная как `-> Safe`. А
   функция, объявленная `-> Safe`, и возвращает только такое.

Чего этот сторож не ловит, честно. Значение, которое склеили в строку без
единого тега и завернули в `Safe` руками, он пропустит: `Safe` это и есть
наша подпись под «здесь разметку ставил бот», и другого способа сказать это
нет. Зато каждое такое место видно в файле глазами, а вслепую, одной
забытой f-строкой, дыра больше не появляется.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from bot import texts
from bot.handlers import admin, billing

ROOT = Path(__file__).resolve().parent.parent
BOT = ROOT / "bot"
LEGACY = ROOT / "legacy"
HOME = BOT / "texts.py"

TRAP = '<a href="http://zlo.example">нажми</a>'

# Открывающий или закрывающий тег. Одиночный «<» с числом или пробелом это
# не разметка, а обычный текст, и придираться к нему незачем.
TAG = re.compile(r"<\s*/?[a-zA-Z]")

# Как в этом проекте уходит сообщение с разметкой.
SENDERS = ("reply_text", "send_message", "edit_message_text", "edit_text")

# Файлы, которые честно не переводятся на общую подстановку. Пусто, и это
# не случайность: сюда пишут имя файла и причину рядом, а не молча.
EXCEPTIONS: dict[str, str] = {}


def bot_files() -> list[Path]:
    """Вся телеграм-поверхность: пакет bot и старое поведение из legacy."""
    found = []
    for base in (BOT, LEGACY):
        found += [
            path
            for path in sorted(base.rglob("*.py"))
            if "__pycache__" not in path.parts
            and path.name not in EXCEPTIONS
        ]
    return found


def name_of(path: Path) -> str:
    return str(path.relative_to(ROOT)).replace("\\", "/")


def parsed(path: Path) -> ast.Module:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            child.parent = node  # type: ignore[attr-defined]
    return tree


# --- инструмент один ---


def test_there_is_something_to_check():
    names = {path.name for path in bot_files()}
    assert {"texts.py", "admin.py", "billing.py", "handlers.py"} <= names


def test_both_handlers_use_the_very_same_tool():
    """Не «такой же», а тот же самый объект: копии расходятся, общий нет."""
    assert admin.Safe is texts.Safe
    assert billing.Safe is texts.Safe
    assert admin.fill is texts.fill
    assert billing.fill is texts.fill


def test_escaping_lives_only_in_texts():
    """`html.escape` во всём боте ровно одно, и оно в bot/texts.py.

    Вторая копия начинается именно с этой строки: кто-то пишет своё
    экранирование рядом со своим шаблоном, потому что в чужой файл лезть
    неловко. Пусть об этом скажет тест, а не следующая утечка.
    """
    offenders = [
        name_of(path)
        for path in bot_files()
        if path != HOME and "html.escape" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        "экранирование разметки живёт в bot/texts.py, зовите texts.fill: "
        + ", ".join(offenders)
    )


def test_the_mark_of_our_own_markup_is_declared_once():
    """`Safe` тоже одна: своя такая же означает, что и подстановка своя."""
    offenders = []
    for path in bot_files():
        if path == HOME:
            continue
        for node in ast.walk(parsed(path)):
            if isinstance(node, ast.ClassDef) and node.name == "Safe":
                offenders.append(name_of(path))
    assert not offenders, (
        "пометка Safe объявлена второй раз, берите её из bot/texts.py: "
        + ", ".join(offenders)
    )


# --- разбор: что здесь считается текстом, собранным ботом ---


def _module_constants(tree: ast.Module) -> set[str]:
    """Имена, которым файл присвоил значение на верхнем уровне."""
    found = set()
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else []
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                found.add(target.id)
    return found


def _markup_templates(tree: ast.Module) -> set[str]:
    """Шаблоны файла, которые несут разметку.

    Это либо строка с тегом внутри, либо строка, которую файл уже хоть раз
    отдал в `fill`: раз её заполняет подстановка, то заполнять её ещё и
    `format` значит держать для чужого значения вторую дорогу.
    """
    found = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not isinstance(node.value, ast.Constant) or not isinstance(node.value.value, str):
            continue
        if not TAG.search(node.value.value):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                found.add(target.id)
    constants = _module_constants(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id != "fill" or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Name) and first.id in constants:
            found.add(first.id)
    return found


def _functions(tree: ast.Module) -> dict[str, ast.AST]:
    """Все функции файла по имени, включая вложенные."""
    found: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found[node.name] = node
    return found


def _promise(node: ast.AST) -> str:
    """Что функция обещает отдать, как это написано в объявлении."""
    returns = getattr(node, "returns", None)
    return ast.unparse(returns) if returns is not None else ""


def _gives_safe(node: ast.AST) -> bool:
    """Отдаёт готовое сообщение: сам `Safe` или пару, где он первым.

    Список `list[Safe]` это строки будущего сообщения, а не сообщение, и
    отправить его нельзя: в текст он не годится.
    """
    promise = _promise(node)
    return promise == "Safe" or promise.startswith("tuple[Safe")


def _promises_safe(node: ast.AST) -> bool:
    """Обещание вообще поминает `Safe`: текст, список строк или пара."""
    return "Safe" in _promise(node)


def _owner_function(node: ast.AST) -> ast.AST | None:
    """Функция, внутри которой лежит узел."""
    current = getattr(node, "parent", None)
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current
        current = getattr(current, "parent", None)
    return None


def _parameters(func: ast.AST | None) -> set[str]:
    if func is None:
        return set()
    args = func.args
    every = list(args.args) + list(args.posonlyargs) + list(args.kwonlyargs)
    if args.vararg:
        every.append(args.vararg)
    if args.kwarg:
        every.append(args.kwarg)
    return {item.arg for item in every}


class Vouched:
    """Умеет сказать, собран ли текст ботом, не выходя за один файл.

    Чужие функции разбираются по имени модуля: `tariffs.tariffs_text()` это
    функция соседнего хендлера, и её объявление видно точно так же.
    """

    def __init__(self, path: Path, tree: ast.Module, everywhere: dict[str, dict[str, bool]]):
        self.path = path
        self.tree = tree
        self.constants = _module_constants(tree)
        self.functions = _functions(tree)
        self.everywhere = everywhere

    def call(self, node: ast.Call) -> bool:
        func = node.func
        if isinstance(func, ast.Name):
            if func.id in ("fill", "Safe"):
                return True
            own = self.functions.get(func.id)
            return own is not None and _gives_safe(own)
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            module = self.everywhere.get(func.value.id, {})
            return module.get(func.attr, False)
        return False

    def name(self, node: ast.Name, inside: ast.AST | None) -> bool:
        if node.id in self.constants or node.id in _parameters(inside):
            return True
        if inside is None:
            return False
        # Локальная переменная годится ровно настолько, насколько годится то,
        # что в неё положили. Присваиваний может быть несколько: сообщение
        # часто собирают оговорка за оговоркой.
        values = []
        for item in ast.walk(inside):
            if isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name) and target.id == node.id:
                        values.append(item.value)
                    if isinstance(target, ast.Tuple) and any(
                        isinstance(part, ast.Name) and part.id == node.id
                        for part in target.elts
                    ):
                        values.append(item.value)
        return bool(values) and all(self.ok(value, inside) for value in values)

    def ok(self, node: ast.AST | None, inside: ast.AST | None) -> bool:
        if node is None:
            return False
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return True
        if isinstance(node, ast.Attribute):
            # Константа соседнего модуля: `texts.NEED_CONNECT`.
            return node.attr.isupper()
        if isinstance(node, ast.Name):
            return self.name(node, inside)
        if isinstance(node, ast.Call):
            return self.call(node)
        if isinstance(node, ast.IfExp):
            return self.ok(node.body, inside) and self.ok(node.orelse, inside)
        if isinstance(node, ast.BoolOp):
            return all(self.ok(value, inside) for value in node.values)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return self.ok(node.left, inside) and self.ok(node.right, inside)
        return False


def _safe_functions_everywhere() -> dict[str, dict[str, bool]]:
    """Какие функции какого файла объявлены отдающими `Safe`."""
    found: dict[str, dict[str, bool]] = {}
    for path in bot_files():
        found[path.stem] = {
            name: _gives_safe(node) for name, node in _functions(parsed(path)).items()
        }
    return found


EVERYWHERE = _safe_functions_everywhere()
FILES = bot_files()
IDS = [path.name for path in FILES]


def _html_sends(tree: ast.Module) -> list[ast.Call]:
    """Вызовы, которые уходят в Telegram с разбором разметки."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in SENDERS:
            continue
        for keyword in node.keywords:
            if keyword.arg != "parse_mode":
                continue
            written = ast.unparse(keyword.value)
            if "HTML" in written:
                found.append(node)
    return found


def _promised_pieces(value: ast.AST, single: bool) -> list[ast.AST]:
    """Что именно проверять в возврате функции, обещавшей `Safe`.

    Обещали текст - проверяется он сам. Обещали пару, где текст первый, -
    проверяется первый. Обещали список строк - проверяются перечисленные
    тут же; накопленный построчно список отдают именем, и за строки в нём
    отвечает та же подстановка, которая их собирала.
    """
    if single:
        return [value]
    if isinstance(value, ast.Tuple) and value.elts:
        return [value.elts[0]]
    if isinstance(value, ast.List):
        return list(value.elts)
    return []


def _sent_text(node: ast.Call) -> ast.AST | None:
    if node.args:
        return node.args[0]
    for keyword in node.keywords:
        if keyword.arg == "text":
            return keyword.value
    return None


# --- ни одной дороги к разметке мимо подстановки ---


@pytest.mark.parametrize("path", FILES, ids=IDS)
def test_a_template_with_markup_is_never_filled_by_format(path: Path):
    """Шаблон с тегом заполняется только через `fill`.

    `str.format` тем же шаблоном выглядит в точности так же и работает, но
    чужое значение уезжает в сообщение как есть.
    """
    tree = parsed(path)
    risky = _markup_templates(tree)
    offenders = [
        node.func.value.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in risky
    ]
    assert not offenders, (
        f"в {path.name} шаблон с разметкой заполнен мимо fill: " + ", ".join(offenders)
    )


@pytest.mark.parametrize("path", FILES, ids=IDS)
def test_markup_is_never_assembled_by_an_f_string(path: Path):
    """`f"<b>{title}</b>"` это та же дыра, только записанная короче.

    Тег в f-строке означает, что значение подставили в разметку своими
    руками, минуя единственное место, где оно стало бы текстом.
    """
    offenders = []
    for node in ast.walk(parsed(path)):
        if not isinstance(node, ast.JoinedStr):
            continue
        if not any(isinstance(part, ast.FormattedValue) for part in node.values):
            continue
        literal = "".join(
            part.value
            for part in node.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
        if TAG.search(literal):
            offenders.append(f"строка {node.lineno}")
    assert not offenders, (
        f"в {path.name} разметка собрана f-строкой, зовите fill: " + ", ".join(offenders)
    )


@pytest.mark.parametrize("path", FILES, ids=IDS)
def test_our_own_mark_is_never_put_on_a_freshly_interpolated_string(path: Path):
    """`Safe(f"...")` и `Safe(ШАБЛОН.format(...))` это подпись не глядя.

    Пометка `Safe` говорит «здесь разметку ставил бот». Ставить её прямо
    поверх подстановки значит подписаться под тем, что только что обошли.
    Исключение одно и оно же единственное честное: сам `bot/texts.py`, где
    подстановка и живёт.
    """
    if path == HOME:
        pytest.skip("подстановка живёт здесь, `Safe` поверх неё это и есть fill")
    offenders = []
    for node in ast.walk(parsed(path)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id != "Safe" or not node.args:
            continue
        inner = node.args[0]
        if isinstance(inner, ast.JoinedStr) and any(
            isinstance(part, ast.FormattedValue) for part in inner.values
        ):
            offenders.append(f"строка {node.lineno}")
        if (
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Attribute)
            and inner.func.attr == "format"
        ):
            offenders.append(f"строка {node.lineno}")
    assert not offenders, (
        f"в {path.name} пометка Safe стоит поверх подстановки: " + ", ".join(offenders)
    )


@pytest.mark.parametrize("path", FILES, ids=IDS)
def test_every_html_message_is_text_the_bot_assembled(path: Path):
    """Отправка с `ParseMode.HTML` берёт только собранное ботом.

    Это `fill`, пометка `Safe`, собственная константа файла или функция,
    объявленная `-> Safe`. Всё остальное, включая склейку на месте, значит,
    что чужое значение поехало в разметку без подстановки.
    """
    tree = parsed(path)
    vouched = Vouched(path, tree, EVERYWHERE)
    offenders = []
    for node in _html_sends(tree):
        text = _sent_text(node)
        if not vouched.ok(text, _owner_function(node)):
            shown = ast.unparse(text) if text is not None else "без текста"
            offenders.append(f"строка {node.lineno}: {shown}")
    assert not offenders, (
        f"в {path.name} сообщение с разметкой собрано мимо fill: " + "; ".join(offenders)
    )


@pytest.mark.parametrize("path", FILES, ids=IDS)
def test_a_function_promising_our_markup_keeps_the_promise(path: Path):
    """Объявленная `-> Safe` отдаёт только собранное ботом.

    Иначе обещание становится способом провести чужую строку дальше: её
    возьмут как готовую разметку, потому что так написано в объявлении.
    """
    tree = parsed(path)
    vouched = Vouched(path, tree, EVERYWHERE)
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _promises_safe(node):
            continue
        single = _promise(node) == "Safe"
        for item in ast.walk(node):
            if not isinstance(item, ast.Return) or item.value is None:
                continue
            if _owner_function(item) is not node:
                continue  # возврат вложенной функции, у неё своё объявление
            for value in _promised_pieces(item.value, single):
                if not vouched.ok(value, node):
                    offenders.append(f"{node.name}, строка {item.lineno}")
    assert not offenders, (
        f"в {path.name} обещание -> Safe не выполнено: " + "; ".join(offenders)
    )


# --- как инструмент себя ведёт ---


def test_foreign_text_becomes_text_and_not_markup():
    filled = texts.fill("Клиент {who}", who=TRAP)

    assert "<a href" not in filled
    assert "&lt;a href=&quot;" in filled
    assert "нажми" in filled  # текст не потерян, он просто не ссылка


def test_quotes_are_escaped_too():
    """Кавычка внутри тега бота сделала бы из значения атрибут."""
    assert "&quot;" in texts.fill("{value}", value='он сказал "да"')


def test_our_own_markup_goes_through_untouched():
    assert texts.fill("{value}", value=texts.Safe("<b>жирным</b>")) == "<b>жирным</b>"


def test_a_filled_piece_is_marked_as_ours():
    """Иначе второй проход показал бы наши собственные теги текстом."""
    once = texts.fill("<b>{who}</b>", who=TRAP)

    assert isinstance(once, texts.Safe)
    assert texts.fill("{line}", line=once) == once


def test_the_template_itself_is_left_alone():
    """Шаблон пишет бот: его теги это разметка, а не данные."""
    assert texts.fill("<b>жирным</b>") == "<b>жирным</b>"
