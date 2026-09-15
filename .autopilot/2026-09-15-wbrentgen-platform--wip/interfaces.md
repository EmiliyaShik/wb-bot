# Интерфейсы и правила проекта

Этот файл читает каждый исполнитель до того, как напишет первую строку. Он растёт по мере сборки: сюда дописываются подписи, которые реально появились в коде.

---

## Правила проекта, которые нельзя вывести из кода

**Стек.** Python 3.13. `python-telegram-bot` 21.x (с `[job-queue]`), `httpx`, `openpyxl`, `reportlab`, `cryptography`, `tomllib` (стандартная библиотека), `sqlite3` (стандартная библиотека). Тесты: `pytest` + `pytest-asyncio`.

**Команды.**

| Что | Команда |
|---|---|
| Установить | `pip install -r requirements.txt` |
| Запустить | `python bot.py` |
| Тесты | `pytest -q` |

**Чего нельзя трогать.**

- `legacy/` - перенесённая как есть старая функция бота (артикул → карточка товара с витрины WB). Логику не менять, поведение не менять. Это отдельный мир: витрина `card.wb.ru` не имеет отношения к API продавца `*-api.wildberries.ru`.
- Любые файлы вне своей зоны (зона указана в таске).
- `.autopilot/` - служебная папка сборки.

**Недостающая зависимость - это `BLOCKED`, а не повод её поставить.** Если для таска нужна библиотека, которой нет в списке выше, вернись со статусом `BLOCKED` и объясни, зачем она. Не добавляй зависимости молча.

**Секреты.** В коде нет ни одного реального значения: ни токена, ни ключа, ни реквизита. Всё через `os.getenv`. Новая переменная окружения обязательно добавляется в `.env.example` с пустым значением. Репозиторий публичный.

**Тексты бота.** Только русский. **Длинные тире запрещены** - используй дефис, запятую или перестрой фразу. Это проверяется тестом, который проходит по всем строкам модуля текстов. Тон: простой, без жаргона, объясняющий. Читатель - селлер, а не программист.

**Изоляция клиентов.** Любая функция, читающая или пишущая данные клиента, принимает `client_id` первым аргументом. Функции без `client_id` в слое данных не существует. Это не рекомендация, а конструкция: один клиент никогда не должен увидеть данные другого.

**Лимиты WB.** Лучше медленнее, чем блокировка токена. Ни один агент не ходит в WB напрямую - только через `core.wbapi`, который сам держит бюджет запросов.

**Бот ничего не пишет в кабинет WB.** Токен только на чтение. Любой метод WB, который что-то меняет, в этой сборке не используется.

---

## Границы, решённые в спецификации

Скопировано из `spec.md`, раздел «Границы и швы». Это контракт: если таску нужно что-то от соседнего модуля, он берёт это отсюда, а не придумывает своё.

| Модуль | Владеет | Выставляет | Прячет |
|---|---|---|---|
| `core.config` | конфигом и переменными окружения | `settings()`, `modules()`, `price(module, months)` | разбор toml, слияние с окружением |
| `core.db` | соединением, миграциями, репозиториями | `repo(client_id)`, `admin_repo()`, `migrate()` | SQL, схему, WAL |
| `core.crypto` | шифрованием токенов | `encrypt(str) -> bytes`, `decrypt(bytes) -> str` | Fernet, чтение ключа |
| `core.wbapi` | HTTP к WB, токенами, лимитами, ошибками | `get_wb_client(client_id)`, `verify_token(raw) -> TokenInfo`, `probe_hosts()` | заголовки, хосты, версии, bucket, ретраи, сам токен |
| `core.queue` | фоновыми задачами | `enqueue(client_id, kind, payload)`, `run_worker()` | таблицу, ретраи, порядок |
| `core.access` | правом пользоваться модулем | `grant_access(...)`, `has_access(client_id, module) -> bool`, `status(client_id)` | состояния, льготный период, паузу, идемпотентность |
| `core.billing` | счетами и реквизитами | `create_invoice(...)`, `mark_paid(number)`, `acts_xlsx(period)`, `lookup_inn(inn)` | нумерацию, PDF, банковские дни, DaData |
| `core.metering` | учётом вызовов и денег | `record_call(...)`, `stats(period)` | агрегацию |
| `core.audit` | журналом | `log(kind, client_id, message)` | фильтрацию секретов |
| `agents.finance` | недельной финансовой раскладкой | `build(client_id, period) -> FinanceReport` | поля WB, формулы, дедупликацию недель |
| `agents.watchdog` | поиском выросших расходов | `check(client_id) -> list[Alert]`, `dynamics(client_id, period)` | пороги, сравнение с 4 неделями |
| `agents.profit` | прибыльностью артикулов | `build(client_id, period) -> ProfitReport` | разнесение расходов по артикулам |
| `agents.rnp` | планом-фактом | `daily(client_id) -> RnpReport` | прогноз, расчёт дней остатка |
| `agents.diagnostic` | бесплатной диагностикой | `run(seller_id, client_id) -> Diagnostic` | выбор трёх утечек |
| `bot.*` | телеграм-поверхностью | хендлеры команд | тексты, клавиатуры, форматирование |
| `legacy.card` | витриной WB (старая функция) | `fetch_product(article)` | как есть |

### Швы для тестов - ровно два

1. **Транспорт WB.** `core.wbapi` принимает `httpx.AsyncClient` извне. В тестах подставляется фейк с записанными ответами. Через него проверяются все агенты целиком, без сети.
2. **Путь к базе.** `core.db` принимает путь. В тестах это временный файл. Через него проверяются доступ, счета, очередь и изоляция клиентов.

Чистые расчётные функции (формулы финансовой раскладки, пороги алертов, прогноз, скидки, банковские дни, контрольная сумма ИНН) тестируются напрямую, без швов.

**Новых швов не создавать.** Если кажется, что нужен третий - это признак, что модуль слишком широкий.


---

## Правила, которые устраняют столкновения между тасками

Несколько тасков идут параллельно. Эти четыре правила придуманы ровно для того, чтобы они не писали в одни и те же файлы.

**1. Хендлеры регистрируются сами.** Таск 01 делает так, что `bot/handlers/__init__.py` находит все модули в папке и вызывает у каждого `register(app)`. **Никто не трогает `bot/app.py`.** Новый хендлер это новый файл, и всё.

**2. Схема базы создаётся целиком в таске 01.** Все таблицы из спецификации появляются в `migrations/001_initial.sql` сразу. Остальные таски **не пишут новых миграций** и не меняют схему. Если таску действительно не хватает поля, он возвращается со статусом `BLOCKED`.

**3. `config.toml`, `.env.example` и `requirements.txt` пишет целиком таск 01.** Со всеми секциями и всеми переменными из спецификации, даже для тех модулей, которых ещё нет. Остальные таски их только читают.

**4. Тексты живут рядом со своим хендлером.** Каждый модуль в `bot/handlers/` держит свои строки у себя. Общий `bot/texts.py` только для того, что реально общее (приветствие, «нет доступа», названия модулей). Тест на отсутствие длинных тире проходит по всем модулям, а не по одному файлу.

---

## Подписи, появившиеся в коде

<!-- Сюда дописывает каждый завершённый таск: что он выставил наружу. -->

### Из таска 01 - каркас

- `core.config`: `settings()`, `modules() -> dict[str, ModuleInfo]`, `visible_modules()`, `price(module, months) -> int`, `discount_percent(months) -> int`, `data_dir() -> Path`, `db_path() -> Path`, `admin_ids() -> tuple[int, ...]`, `is_admin(tg_id) -> bool`, `env(name, default="") -> str`, `seller_details() -> dict[str, str]`, `missing_seller_details() -> tuple[str, ...]`. `ModuleInfo(name, price_month, visible, title, agents, includes)`
- `core.db`: `migrate(path=None) -> int`, `connect(path=None)`, `close_all()`, `repo(client_id, path=None) -> ClientRepo`, `admin_repo(path=None) -> AdminRepo`, `CLIENT_TABLES`
  - `ClientRepo`: `.insert(table, **values) -> int`, `.upsert(table, keys, **values)`, `.rows(table, order_by=None, limit=None, **where)`, `.one(table, **where)`, `.count(table, **where)`, `.update(table, where: dict, **values) -> int`, `.delete(table, **where) -> int`, `.client_id`
  - `AdminRepo`: `.ensure_client(telegram_id) -> int`, `.client(id)`, `.client_by_telegram(tg_id)`, `.all_clients()`, `.set_client_fields(id, **values)`, `.delete_client(id)`, `.next_invoice_number(year) -> "WBR-ГГГГ-NNNN"`, `.query(sql, params)`, `.execute(sql, params)`
- `core.crypto`: `encrypt(str) -> bytes`, `decrypt(bytes) -> str`, `key_available() -> bool`, `generate_key() -> str`, `KEY_HINT`, `MissingKeyError`, `DecryptError`
- `core.audit`: `log(kind, client_id, message, level="info")`, `redact(text) -> str`, `recent(limit=50, level=None, client_id=None)`, `HIDDEN`
- `bot.handlers`: `register_all(app) -> list[str]`. **Файл в `bot/handlers/` с функцией `register(app)` подхватывается сам.** Имена с `_` в начале пропускаются. `bot/app.py` больше никто не трогает
- `bot.app`: `startup() -> dict{schema_version, admins, tokens_enabled, db_path}`, `build_app(token) -> Application`
- `bot.texts`: `INTRO`, `HELP`, `NO_ACCESS`, `NEED_CONNECT`, `TOKENS_DISABLED`, `RATE_LIMITED`, `ADMIN_ONLY`, `WB_UNAVAILABLE`, `SOMETHING_WENT_WRONG`, `module_title(name)`
- `legacy.card`: `fetch_product(article)`, `Product` и прежние классы ошибок (переехало из корневого `wb_api.py`, файл в корне удалён). `legacy.handlers`: `parse_article`, `format_product`, `register(app)`
- **База:** все 17 таблиц спецификации плюс `schema_version`. Клиентские таблицы с внешним ключом `ON DELETE CASCADE`. Новых миграций не писать
- **`fin_rows` и `nm_daily` несут дополнительное поле `raw`** (JSON исходной строки WB) - чтобы агентам не понадобилась новая миграция
- **В `config.toml` сверх спецификации:** секция `[limits]` (частота команд на клиента, лимит WB 3 запроса за 30 секунд) и `[token_categories]` (категория токена -> какие модули без неё не работают)
- Тесты: `pytest -q`, один файл `pytest -q tests/test_config.py`
