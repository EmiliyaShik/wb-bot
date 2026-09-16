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

**Никогда не трогай состояние git.** Ни `stash`, ни `checkout`, ни `reset`, ни `clean`. Рядом с тобой в этом же дереве пишут другие таски, и `git stash` на секунды забирает их файлы: один раз это уже случилось и обошлось. Нужно измерить, сколько тестов было до тебя, - спроси у меня, не двигай дерево. Коммиты делаю я.

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
  - `AdminRepo`: `.ensure_client(telegram_id) -> int`, `.client(id)`, `.client_by_telegram(tg_id)`, `.all_clients()`, `.set_client_fields(id, **values)`, `.delete_client(id)`, `.next_invoice_number(year) -> "WBR-ГГГГ-NNNN"`. **Произвольного SQL (`query`, `execute`) в общем слое нет и не будет:** нужен новый доступ - проси именованный метод, а не обход
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

### Из таска 02 - клиент WB API

**Единственное место в проекте, которое знает про HTTP и токены WB.** Свои обёртки к WB не пиши, свой разбор токена не пиши, свои классы ошибок не создавай.

- `get_wb_client(client_id, *, http=None, path=None, clock=None, sleep=None) -> WBClient`, `verify_token(raw) -> TokenInfo`, `check_token(raw, ...)` (один `/ping`), `probe_hosts(...) -> list[HostProbe]`, `verdict_for(status)`, `load_token(client_id, path=None) -> str`, `shared_session() -> AsyncClient`, `await close_session()`, `retry_policy() -> RetryPolicy(attempts, base, factor, cap)`, `reset_limits()`
- `TokenInfo(sid, exp, mask, acc, categories, read_only, is_test, token_id, issued_for)` плюс `.expires_at`, `.acc_title`, `.titles`, `.has(cat)`, `.missing(tuple)`, `.days_left(now)`, `.is_expired(now)`
- `WBClient`: `.session`, `.token_info`, `.sid`, `.client_id`, `.has_category(cat)`; `await`: `pause(sec)`, `ping(host) -> int`, `request(key, **kw)`, `request_endpoint(endpoint, *, json, params, attempts, timeout, raw)`
  - `sales_report_detailed(date_from, date_to, *, period, limit, fields)`, `sales_reports_list(date_from, date_to)` - агенты 1, 2, 3
  - **`sales_report_detailed_paged(...) -> ReportPages(rows, truncated, pages, last_rrd_id)`** - то же самое, но честно говорит, оборвалась ли выгрузка на потолке страниц. `truncated` поднимается только при «потолок исчерпан, курсор живой»: короткая страница и повтор курсора это штатный конец. Если тебе важно отличить полную выгрузку от обрезанной - бери этот метод, а не считай по длинам
  - `promotion_count()`, `advert_ids()`, `fullstats(ids, begin, end)` - агенты 3, 4
  - `sales_funnel_products(start, end, *, past=None, nm_ids, limit)`, `sales_funnel_history(start, end, *, nm_ids, aggregation_level)` - агент 4
  - `stocks_wb_warehouses(*, nm_ids, limit)` - агент 4
  - `cards_list(*, limit)` - таск 06, шаблон себестоимости
- Ошибки: `WBError` > `WBAuthError` > `WBTokenMissing`; `WBForbiddenError(.category)`; `WBRateLimited(.retry_after)`; `WBUnavailable`; `WBApiError`; `WBTokenFormatError`. У всех `.status` и `.path`
- Константы: `HOSTS`, `HOST_CATEGORY`, `ENDPOINTS`, `Endpoint`, `CATEGORY_BITS`, `CATEGORY_TITLES`, `READ_ONLY_BIT`
- `bot.handlers.diag`: `register(app)`, `report_text(probes, note="")`. Команда `/diag`, только владельцу
- **Повторы 5xx и таймаута внутри клиента убраны.** Повтор живёт только в очереди, по секции `[queue]`.

> **Правило для агентов (таски 09-13), важное.** Раз повтор остался только в очереди, любая работа с WB ставится через `core.queue.enqueue(...)` и **никогда не зовётся из хендлера напрямую**. Вызов мимо очереди при первой же недоступности WB упадёт без повтора, и клиент получит ошибку там, где по ТЗ должен был получить результат позже. Хендлер ставит задачу и отвечает; всё остальное делает обработчик задачи.
- **Лимиты методов - таблица `LANES` в `core/wbapi/limits.py`**, не в конфиге: это факты про WB, а не настройки владельца. Решение записано в спецификации
- **Отключённых методов в коде нет**, и тест падает при появлении метода, который что-то меняет в кабинете. Справочник - `docs/wb-api.md`

### Из таска 03 - очередь и расписание

- `core.queue`: `enqueue(client_id, kind, payload=None, *, run_at=None, notify=True, path=None) -> int`
  - **`notify=True` сама шлёт клиенту «принято, пришлю, когда будет готово».** Агенты ничего не пишут сами. Для расписания и служебных задач ставь `notify=False`
  - `register(kind, fn)`, `handlers()`, `reset()`, `recover(path=None) -> int`, `run_once(path=None) -> bool`, `run_worker(*, path=None, poll_sec=5.0, stop=None)`, `set_notifier(fn(client_id, text))`, `set_auth_handler(fn(task, error))`, `max_attempts() -> int`
  - `Task(id, client_id, kind, payload: dict, attempts)`. Обработчик это `fn(task)`, sync или async
  - Состояния: `PENDING="queued"`, `RUNNING`, `DONE`, `FAILED`, `CANCELLED`
  - Готовые тексты: `ACCEPTED`, `FAILED_TEXT`, `GENERIC_FAILED_TEXT`, `TOKEN_TEXT`, `forbidden_text(category)`
  - **Свою задачу регистрируешь сам:** `queue.register("finance_report", fn)` в своём модуле
- `core.scheduler`: `register_daily(name, fn)`, `register_weekly(name, fn)`, `set_report_probe(fn(client_id) -> report_id|None)`, `set_clients_provider(fn)`, `reset()`, `run_daily(path=None)`, `check_weekly(path=None)`, `install(job_queue, path=None)`, `daily_time()`, `timezone_name()`, **`tz()`**, `weekly_check_hours()`, `daily_names()`, `weekly_names()`
  - payload ежедневной задачи `{"date": "ГГГГ-ММ-ДД"}`, недельной `{"report_id": N}`. Повтор за тот же день или тот же отчёт не дублируется
- **`core.scheduler.tz()` - единственный источник часового пояса в проекте.** Своего разбора `Europe/Moscow` не пиши (см. D01)

### Из таска 04 - доступ к модулям

- `core.access.grant_access(client_id, module, days, payment_ref, method="manual", actor="system", *, now=None, path=None) -> Access` - **единственная дверь, через которую включается доступ.** Повтор с тем же `payment_ref` возвращает `Access.duplicate = True` и ничего не продлевает
- `core.access`: `has_access(client_id, module, *, now=None, path=None) -> bool`, `access_of(...)`, `status(client_id, *, now=None, path=None, include_hidden=False) -> list[Access]`, `pause(client_id, *, reason="token_401", ...) -> int`, `resume(client_id, ...) -> int`, `start_trial(client_id, module, ...) -> Access`, `TrialDenied(.reason)`, `packages()`, `grace_days()`, `trial_days()`, `trial_modules()`
  - `Access(client_id, module, state, until, source, paused_at, duplicate, as_of)` плюс `.works`, `.days_left`
  - Состояния: `ACTIVE`, `GRACE`, `OFF`, `PAUSED`, `HIDDEN`, множество `WORKING`
- `bot.handlers.tariffs`: **`require_module(module, *, path=None)` - декоратор для платной команды.** Нет доступа - клиент получает описание модуля, цену и кнопку «Оформить». Все платные команды тасков 09-13 оборачиваются им, свою проверку доступа не пишут
- `bot.handlers.tariffs`: `set_buy_dialog(callback)` - точка расширения для таска 07, `callback(update, context, module)`; `BUY_PREFIX="buy:"`, `BUY_GROUP=50`; плюс `tariffs_text`, `offer_text`, `offer_keyboard`, `granted_text`, `price_line`, `local_date`, `rubles`, `what_it_gives`, `client_id_of`
- **`core.access.revoke_access(client_id, module, reason="", actor="owner", *, payment_ref=None, now=None, path=None) -> Access`** - единственная дверь для отмены, симметрична выдаче. Гасит модуль, возвращает `Access` в состоянии `off`, пишет строку `revoke` в журнал
  - **Идемпотентность отмены стоит на состоянии, а не на номере платежа.** Гасить нечего - записи в журнале нет и вызов не падает. Это строже проверки по `payment_ref`: два разных основания не погасят один доступ дважды
  - **Следствие, которое надо знать:** отмена не открывает платёж заново. `grant_access` с тем же `payment_ref` по-прежнему считается дублем, поэтому после возврата новая выдача идёт по **новому** `payment_ref`
  - Работает по одному модулю, как и выдача. Погасить клиента целиком - обойти `status(client_id)` и позвать по каждому
- Команды: `/tariffs`, `/modules`, `/trial`. Журнал в `access_log`: `grant`, `duplicate`, `pause`, `resume`, `revoke`

### Добавилось в `core.config` и `core.db` по ходу волны 2

- `core.config.periods() -> tuple[int, ...]` - сроки подписки по возрастанию, из ключей `months_*` секции `[periods]`. Свой разбор не пиши
- `core.config.ModuleInfo` получил поля `gives: str` и `diagnostic_line: str`. Полный состав: `name`, `price_month`, `visible`, `title`, `agents`, `includes`, `gives`, `diagnostic_line`
- `config.modules()` больше не кэшируется: подмена конфига в тестах работает без чистки кэшей
- `core.db.AdminRepo.tasks_by_kind(kind, limit=None) -> list[Row]` - задачи одного вида, новые сверху, без среза. Только чтение
- `config.settings()["schedule"]["weekly_check_hours"] = 6`

### D01 - часовые пояса

На Windows системной базы часовых поясов нет, `ZoneInfo("Europe/Moscow")` падает. В зависимости добавлен `tzdata` (чистый Python). Своего отката больше не пиши: бери `core.scheduler.tz()`.

### Из таска 06 - себестоимость и общий помощник по Excel

**`core.xlsx` - общий помощник, им пользуются таски 08, 09 и 11.** Своей работы с openpyxl не пиши.

- `Sheet(title, headers, rows=(), widths=None)` - описание листа на запись
- `Row(number, cells, values)` плюс `.get(header, default)`. **`Row.number` настоящий номер строки в файле** (шапка 1, данные с 2), пустые строки пропускаются, номера не сдвигаются - именно это позволяет сказать клиенту «строка 12»
- `SheetData(title, headers, rows)` плюс `.column(*aliases) -> str|None`, `.missing({имя: синонимы}) -> tuple[str, ...]`
- `Book(sheets)` плюс `.titles`, `.first`, `.get(title)`, `book["Лист"]`, итерация, `len`
- `write_book(sheets, *, path=None) -> bytes` - жирная закреплённая шапка, ширины по содержимому
- `read_book(source, *, max_bytes=None) -> Book`, `read_sheet(source, *, title=None, max_bytes=None) -> SheetData`
- `looks_like_xlsx(data) -> bool`, `normalize(text)`, `find_column(headers, aliases)`
- `XlsxError` > `NotXlsxError`, `TooLargeError(.size, .limit)`, `SheetNotFoundError`
- Константы `HEADER_ROW=1`, `FIRST_DATA_ROW=2`, `MIN_WIDTH`, `MAX_WIDTH`

**`core.costs` - себестоимость.**

- `costs_for(client_id, *, nm_ids=None, path=None) -> dict[int, Decimal]` и `missing_costs(client_id, nm_ids, *, path=None) -> list[int]` - **этим пользуется таск 11** для блока «нет себестоимости»
- `save_costs(client_id, {nm_id: Decimal}, *, path=None) -> int`
- `parse_upload(data, *, max_bytes=None) -> Upload`, `save_upload(client_id, data, ...) -> Upload`
- `Upload(values, problems, blank, saved)` плюс `.total`, `.skipped`, `.ok`; `Problem(row, reason)`; `BadFile` (текст готов для селлера)
- `template_bytes(cards, existing=None) -> bytes`, `await build_template(client_id, ...)`, `request_template(client_id, ...) -> int`, `template_task(task)`, `set_sender(fn(client_id, filename, data))`, `max_upload_bytes() -> int`
- `TASK_KIND="costs_template"`, `TEMPLATE_SHEET`, `TEMPLATE_HEADERS`, `COL_NM`, `COL_VENDOR`, `COL_TITLE`, `COL_COST`, `ALIASES`
- `bot.handlers.costs`: `register(app, *, path=None)`, `result_text(Upload)`, `too_big_text(size, limit)`, `make_sender(app, path=None)`, `DOCUMENT_GROUP=40`

**Приём документов от клиента уже занят таском 06** (group 40, ловит любой документ, чтобы вежливо отказать по чужому формату). Если твоему таску тоже нужно принимать файлы, договорись через `interfaces.md`, а не вешай второй обработчик молча.

### Из таска 05 - подключение кабинета

- `core.clients`: `offer_url()`, `offer_ready()`, `record_consent(client_id, *, now, path) -> datetime`, `has_consent(client_id, *, path)`, `connected(client_id, *, path)`
- `await core.clients.connect(client_id, raw, *, http, path, now) -> Connected(client_id, info, missing, replaced, resumed)` - подключение кабинета целиком: проверка токена, шифрование, запись
- `missing_categories(info, needed=REQUIRED_CATEGORIES)`, `REQUIRED_CATEGORIES` - **пять категорий** (Статистика, Финансы, Аналитика, Продвижение, Контент)
- `on_wb_error(client_id, error, *, now, path) -> Trouble(kind: auth|forbidden|other, category, paused)` - **единая точка реакции на ошибку WB.** 401 ставит модули на паузу, 403 не ставит. Свою обработку 401 не пиши, зови это
- `token_row(...)`, `token_expires(...)`, `days_left(...)`, `expiring_tokens(*, now, path, days)`, `REMINDER_DAYS = (14, 3)`
- `disconnect(client_id, *, path) -> dict[таблица, сколько удалено]` - физическое удаление всех данных клиента
- `ConsentRequired`, `TokenExpired`
- `bot.handlers.connect`: `register(app)`; команды `/connect`, `/disconnect`; кнопки `connect:agree|replace|keep|wipe`; `instruction_text()`, `missing_text(missing)`, `forbidden_text(category)`, `confirm_text(...)`, `modules_text(...)`, `reminder_text(days)`, `make_reminder(app, ...)`, `make_auth_notice(app, ...)`, `REMINDER_JOB`, `TOKEN_PATTERN`
- **Сообщение клиента с токеном удаляется из переписки** сразу после приёма: токен не остаётся в истории чата
- Хендлер берёт путь к базе из `context.bot_data["db_path"]` и транспорт WB из `context.bot_data["wb_http"]`; в бою обоих ключей нет, и берутся конфиг и общая сессия

### Единственное исключение из правила изоляции (решено после таска 05)

Правило прежнее: **клиентские таблицы недостижимы из общего слоя.** У него есть ровно одно исключение, и оно названо здесь, чтобы следующий таск не повторил приём без разбора:

- `core.db.AdminRepo.tokens_with_exp() -> list[tuple[client_id, exp]]` - сквозной срез по `wb_tokens` для напоминаний о сроке токена. Отдаёт **только** `client_id` и срок; шифротокена в выборке нет и добавлять его туда нельзя.

Новое такое исключение не заводится молча: если кажется, что нужен ещё один сквозной срез по клиентской таблице, вернись со статусом `BLOCKED` и объясни, почему обход `all_clients()` не подходит.

### Соглашение о швах в хендлерах (решено после таска 05)

Швов в проекте ровно два, но подавать их в телеграм-поверхность три таска начали по-разному. Дальше - одинаково:

- **Путь к базе** приходит параметром `path=None` в `register(app, *, path=None)` и в сами функции хендлера. Так сделано в `tariffs` и `costs`. Через `context.bot_data` путь к базе не передаётся.
- **Транспорт WB** передаётся параметром `http=` в функцию ядра (`core.*`), а не в хендлер. Хендлер про транспорт не знает: он зовёт `core`-функцию, а та берёт общую сессию через `core.wbapi`.

Своего третьего способа не изобретай.

### Из таска 07 - счета

- `core.billing`: `create_invoice(client_id, module, months, *, inn="", org_name="", org_address="", now=None, path=None) -> Invoice`, `mark_paid(number, *, actor="owner", now=None, path=None) -> Paid`, `cancel(number, ...)`, `invoice(number, ...)`, `invoices_of(client_id, *, status=None, path=None)`, `expire_overdue(*, now=None, path=None) -> list[Invoice]`, `await lookup_inn(inn, *, http=None) -> Counterparty|None`
- Вспомогательное: `months_to_days(months, start=None)`, `months_words(months)`, `service_line(module, months)`, `vat_note()`, `vat_note_missing()`, `offer_url()`, `owner_contact()`, `seller_block()`, `missing_details()`, `local_day(dt)`
- `Invoice(number, client_id, module, period_months, amount_kop, status, inn, org_name, org_address, issued_at, due_at, paid_at)` плюс `.amount`, `.status_word`, `.is_open`, `.customer`, `.payment_purpose()`
- `Paid(invoice, granted, duplicate, already_paid)`; `DetailsMissing(.missing)`; статусы `ISSUED`, `PAID`, `OVERDUE`, `CANCELLED`, `STATUS_WORDS`, `METHOD="invoice"`
- `core.billing.bankdays`: `add_bank_days(start, days, holidays_list=None) -> date`, `is_bank_day(...)`, `due_date(start, days=None)`, `holidays()`, `valid_bank_days()`
- `core.billing.counterparty`: `inn_is_valid(v) -> bool`, `normalize(v)`, `api_key()`, `await lookup(inn, *, http=None) -> Counterparty|None`, `Counterparty(inn, name, address)`
- `core.billing.pdf`: `content(invoice) -> dict`, `build(invoice) -> bytes`, `file_name(invoice)`, `find_font()`, `money(amount)`, `FontMissing`
- `bot.handlers.billing`: `register(app)` - встаёт в `tariffs.set_buy_dialog`, команда `/paysupport`, префиксы callback `inv:` и `invpay:`, текстовый шаг в группе `DIALOG_GROUP=-10`, ежедневная работа `invoices_overdue`

**Диалог покупки занят таском 07.** Кнопка «Оформить» из витрины тарифов ведёт сюда. Своего диалога покупки не делай.

**Про шрифт в PDF (важно для деплоя).** `reportlab` не несёт кириллицы. `pdf.find_font()` ищет системный TTF (DejaVu, Liberation, Noto, Arial) и проверяет наличие русских букв. Шрифта нет - счёт в базе создаётся, PDF не собирается, владелец получает точную подсказку. На боевом хосте это надо проверить один раз.

### Из таска 12 - агент 4, РНП (план-факт)

- `agents.rnp`: `MODULE="rnp"`; виды задач `COLLECT_ALL="rnp_collect"`, `REPORT_ALL="rnp_report"`, `COLLECT_ONE="rnp_collect_client"`, `REPORT_ONE="rnp_report_client"`
- `await collect(client_id, *, day, window, http, path, clock, sleep) -> int` - сбор суточных данных в `nm_daily`
- `daily(client_id, *, today=None, path=None) -> RnpReport` - отчёт строится **по базе**, в WB не ходит
- `set_plan(client_id, "ГГГГ-ММ", *, revenue=None, orders=None, path=None) -> Plan`, `plan_of(client_id, month, *, path=None) -> Plan|None`
- `request_report(client_id, *, day=None, path=None) -> int`, `register_jobs()`, `set_delivery(fn(client_id, report))`
- `fan_out_collect(task, ...)`, `fan_out_report(task, ...)`, `await collect_client(task, ...)`, `await report_client(task, ...)`
- Чистые функции: `drr(spend, revenue)`, `forecast(fact, days_passed, days_in_month)`, `stock_days(stock, per_day)`, `percent(fact, target)`, `year_month(date)`, `yesterday(today=None)`
- `Plan(year_month, revenue, orders).is_set`; `StockRisk(nm_id, stock, per_day, days)`; `RnpReport(...)` плюс `.drr`, `.forecast_revenue`, `.forecast_orders`, `.revenue_percent`, `.orders_percent`
- `bot.handlers.rnp`: `register(app, *, path=None)`, команды `/plan` и `/rnp` под `require_module("rnp")`, `report_text(report)`, `make_delivery(app, path)`

**Решения, на которые стоит опираться, а не переспрашивать:**

- Сбор идёт **окном в неделю**, а не за вчерашний день: WB больше недели не отдаёт, и окно само закрывает дыры после простоя бота
- Среднее за неделю делится на дни, за которые данные реально есть, а не всегда на семь: иначе первая неделя работы занижала бы базу сравнения
- Скорость продаж для остатков берётся за 14 накопленных суток, не за вчера
- Остаток это снимок на момент сбора (истории остатков у WB нет), пишется в строку последнего собранного дня
- Без категории «Продвижение» сбор продолжается с нулевым расходом рекламы: иначе терялась бы вся история дня

### Из таска 09 - агент 1, Финансист

**Таблицы здесь тоже интерфейс.** Агенты 10 и 11 читают их и в WB не ходят вообще.

- `fin_rows` - одна строка ответа WB. Ключ `(client_id, rrd_id)`, `report_id` колонкой, деньги целыми копейками (`_kop`), проценты `REAL` как отдал WB. **`raw` содержит строку целиком:** параметр `fields` в запрос не шлётся. Если нужно поле, которого нет колонкой, бери из `raw`, а не ходи в WB
- `fin_weeks` - агрегат недели. Ключ `(client_id, report_id)`, всё в копейках. `revenue_kop` только продажи, `returns_amount_kop` возвраты отдельно, `for_pay_kop` сумма `forPay` включая возвраты, `commission_kop` сумма `vw`, `control_for_pay_kop` из `forPaySum`. `control_payload` это JSON `{"aggregate": <ответ list или null>, "complete": bool}`

- `agents.finance`: `MODULE="finance"`, `TASK_KIND="finance_report"`, `PERIODS{week 7, month 31, quarter 92, year 365}`, `PERIOD_TITLES`, `USED_FIELDS`, `PAGE_LIMIT=1000`, `MAX_PAGES=500`, `DATA_SINCE=2024-01-29`, `TOLERANCE=1₽`
- `period_bounds(period, today=None)`, `money(v) -> Decimal`, `is_return(row) -> bool`
- `await collect(client_id, date_from, date_to, *, limit=None, max_pages=MAX_PAGES, http, path, clock, sleep) -> Collected(rows, pages, weeks, truncated, verified)`
- `aggregate(rows) -> dict[report_id, Week]`, `save_rows(...) -> int`, `save_week(client_id, week, control=None, *, complete=True, path)`
- `build(client_id, period="week", *, today=None, path=None) -> FinanceReport` - **строится по базе, в WB не ходит**
- `weeks_of`, `articles_of`, `months_of`; `request_report`, `await report_task`, `set_sender`, `register_jobs`, `excel_bytes`, `file_name`
- `Amounts(revenue, returns_amount, for_pay, commission, acquiring, logistics, storage, acceptance, penalties, deductions, additional_payment, sales_count, returns_count)` плюс `.costs`, складывается
- **`Week(..., checks, verified, complete)` плюс `.checked` (сверено И сошлось), `.mismatches`, `.trustworthy`.** Три состояния различаются и не смешиваются: сверено и сошлось, сверка не выполнена, выгрузка обрезана
- `FinanceReport` плюс `.totals`, `.empty`, `.mismatches` (разошлось), `.unverified` (сверка не выполнена), `.incomplete` (выгрузка обрезана), `.title`
- `Month(key, amounts, weeks)`, `Article(nm_id, vendor_code, subject, quantity, amounts)`, `Check(name, ours, theirs)` плюс `.diff`, `.matches`
- `core.excel`: `finance_book(report, *, path=None) -> bytes`, `finance_sheets(report)`, `METHODOLOGY`. Листы «Недели» (с колонками «Данные за неделю» и «Сверка с отчётом WB»), «Месяцы», «По артикулам», «Методология»
- `bot.handlers.finance`: `register(app, *, path=None)`, `summary_text(report)`, `keyboard()`, `make_sender(app, path)`, `PREFIX="fin:"`, `BUTTONS`

**Три решения, принятые осознанно:**

- **Комиссия в рублях это сумма `vw`** (вознаграждение WB без НДС): одного поля «комиссия» в API нет. Названо в «Методологии» и в `CLAUDE.md`
- **Недельный процент это средневзвешенное готового поля WB** (вес `retailPriceWithDisc`). Сами проценты не пересчитываются
- **Усечение выгрузки ловится по признаку «все страницы полные и упёрлись в потолок».** Оценка консервативная: ложно неполной неделя стать может, ложно полной - нет. Неполная неделя помечена и видна клиенту

### Из таска 08 - владелец: статистика, акты, выдача, упавшие задачи

- `core.metering` (**только учёт**): `record_call(client_id, host, method, *, status, duration_ms, path)`, `record_ai(client_id, *, provider, model, kind, prompt_tokens, completion_tokens, cost_kop, path)`, `stats(period="month", *, today=None, path=None) -> Stats`, `PERIODS`, `MAX_ROWS`
  - `Stats(period, title, start, end, modules, active_clients, total_clients, revenue, unpaid_count, unpaid_amount, calls, ai_cost, clients)` плюс `.calls_total`, `.errors_total`, `.margin`; `MethodUse(method, count, errors)`; `ClientMoney(client_id, revenue, cost, calls, modules)` плюс `.margin`
- `core.billing` (**реестр актов живёт здесь, а не в учёте**: он собирается из счетов): `acts_xlsx(period=None, *, today=None, path=None) -> bytes`, `paid_invoices(start, end, *, path=None)`, `acts_book(invoices, *, path=None)`, `acts_rows(invoices)`, `acts_file_name(start)`, `ACTS_SHEET`, `ACTS_HEADERS`, `BadPeriod`
  - **Календарь месяца тоже здесь, один на проект:** `month_bounds(period=None, *, today=None)`, `previous_month_bounds(today=None)`, `month_title(start)`, `month_key(start)`, `in_period(moment, start, end)`. Своего разбора месяца не пиши
- `core.ratelimit`: `per_minute()`, `check(who, *, limit=None, now=None) -> Decision(allowed, retry_after)`, `allow(...)`, `reset(who=None)`, `WINDOW_SEC`. Ключ - Telegram ID, окно в памяти
- `bot.handlers.admin`: `register(app, *, path=None)`; команды `/stats`, `/acts`, `/grant`, `/revoke`, `/tasks`; `PREFIX="adm:"`, `RETRY="adm:retry:"`, `GUARD_GROUP=-100`, `ACTS_JOB="acts_monthly"`, `METHODS`, `ALL_MODULES`; `stats_text`, `tasks_text`, `tasks_keyboard`, `make_acts_job(app, path)`, `rate_guard`

**Ограничение частоты команд уже стоит** хендлером в группе -100 и накрывает все команды всех пользователей. Своего не добавляй.

### Из таска 10 - агент 2, сторож скрытых расходов

**В WB не ходит вообще.** Всё считается по `fin_weeks` и `fin_rows` после агента 1.

- `agents.watchdog`: `MODULE="finance"`, `TASK_KIND="watchdog_alerts"`, `METRICS`, `TITLES`
- `thresholds() -> dict[str, Decimal]`, `baseline_weeks() -> int`, `share(part, whole) -> Decimal|None`
- `metrics_of(finance.Week) -> WeekMetrics`, `average(weeks, metric)`, `compare(current, baseline) -> tuple[Alert, ...]`
- `history(client_id, period=None, *, today, path)`, `check(client_id, *, today=None, path=None) -> Watch`, `dynamics(client_id, period="month", *, today, path) -> Dynamics`
- `latest_report(client_id, *, path)`, `set_delivery(fn(client_id, watch))`, `await alerts_task(task, *, path)`, `register_jobs(*, path=None)`
- `WeekMetrics(report_id, date_from, date_to, revenue, commission, acquiring, spp, logistics_share, storage_share, complete, trustworthy)` плюс `.value(metric)`
- `Alert(metric, title, was, now, delta, threshold, revenue, rubles)` плюс `.grew`
- `Watch(client_id, week, baseline, alerts, have, needed, doubtful)` плюс `.enough`, `.missing`; **итерируется алертами**, `list(check(...))` даёт ровно их
- `Dynamics(client_id, period, weeks)` плюс `.empty`, `.title`
- `bot.handlers.dynamics`: `register(app, *, path=None)`, команда `/dynamics` под `require_module("finance")`, `alerts_text`, `table_text`, `alert_lines`, `period_of`, `make_delivery`

**Решения, принятые осознанно:**

- **В сравнение идут только недели с `complete=True`:** обрезанная выгрузка дала бы ложное падение выручки. Несверенная неделя в расчёт идёт, но сообщение получает оговорку (`Watch.doubtful`) - иначе клиент неделями не видел бы ничего
- Рубли алерта считаются одинаково для всех пяти показателей: модуль изменения в п.п. от выручки недели
- **`register_jobs()` занимает единственную общую пробу расписания `scheduler.set_report_probe`.** Проба читает максимальный `report_id` из `fin_weeks` и в WB не ходит. Появится вторая недельная работа - пробу надо выносить

### Из таска 11 - агент 3, прибыльность артикулов

- `agents.profit`: `MODULE="finance"`, `TASK_KIND="profit_report"`, `PERIODS` (те же, что у агента 1), `TOP_SIZE=5`
- Чистые: `margin(profit, net_revenue)`, `share(profit, total)`, **`spread(amount, weights) -> dict[int, Decimal]`** (разнесение обезлички), `ad_totals(campaigns)`
- `await collect_ads(client_id, date_from, date_to, *, http, path, clock, sleep) -> AdSpend`
- `build(client_id, period="week", *, today=None, ads=None, path=None) -> ProfitReport` - **в WB не ходит**
- `AdSpend(spend, available, reason)` плюс `.of(nm_id)`, `.total`; причины `ADS_OK`, `ADS_NO_CATEGORY`, `ADS_UNAVAILABLE`, `ADS_NOT_REQUESTED`
- `ArticleProfit(nm_id, vendor_code, subject, units, returns_count, revenue, returns_amount, cost_per_unit, cost, commission, acquiring, logistics, storage, acceptance, penalties, deductions, ad_spend, unallocated, profit, margin, share)` плюс `.net_revenue`, `.wb_costs`, `.has_cost`, `.is_loss`
- `ProfitReport(...)` плюс `.priced`, `.without_cost`, `.losses`, `.top`, `.bottom`, `.total_profit`, `.total_revenue`, `.total_ad_spend`, `.empty`, `.title`, `.incomplete`, `.unverified`
- `excel_sheets`, `excel_bytes`, `file_name`, `ARTICLES_SHEET`, `PROBLEMS_SHEET`, `METHOD_SHEET`, `METHODOLOGY`
- `request_report`, `await report_task`, `set_sender(fn)`, `register_jobs()`
- `bot.handlers.profit`: `register(app, *, path=None)`, `summary_text`, `keyboard`, `make_sender`, `PREFIX="profit:"`, команда `/profit` под `require_module("finance")`

**Решения, принятые осознанно (и они расходятся со сторожем - намеренно):**

- **Прибыль считается и по несверенным, и по обрезанным неделям**, а такие недели названы в сообщении через `.incomplete` и `.unverified`. У сторожа наоборот: обрезанные недели в сравнение не идут. Разница осмысленная: у сторожа обрезанная неделя даёт **ложную тревогу**, а здесь её исключение оставило бы селлера **вообще без ответа**
- **Комиссия по артикулу считается по `vw` из `fin_rows.raw`**, тем же основанием, что у агента 1: суммы по артикулам складываются в комиссию недели. `ppvzSalesCommission` не используется и запрещена к подстановке - она про другие деньги, и молчаливая подмена давала бы третью цифру. Пустой `raw` означает «комиссия неизвестна», а не ноль и не соседнее поле
- Себестоимость берётся по штукам «продано минус возвращено»
- Доля в прибыли у убыточных отрицательная, сумма долей прибыльных может быть больше 100%: названо в «Методологии»

### Из таска 14 - жизненный цикл: рассылки, настройки, продление, удаление

- `agents.lifecycle`: `prefs(client_id, *, path) -> Prefs(client_id, daily, weekly, daily_at)` плюс `.daily_at_text`; `set_daily/set_weekly(client_id, bool, *, path)`, `set_daily_time(client_id, "ЧЧ:ММ"|time, *, path)`, `daily_enabled/weekly_enabled`, `parse_time(value)`
- `grace_days()`, `renewal_notice_days()`, `retention_days()`, `retention_notice_days()` - всё из `[access]`
- `Event(kind, client_id, module, days_left, until)` плюс `.key`, `.stamp`; виды `RENEWAL`, `GRACE`, `SHUTDOWN`, `RETENTION`, `DELETED`
- `check(client_id, *, now, path) -> list[Event]`, `remember(client_id, event, *, path)`, `erase(client_id, *, path) -> dict` (обёртка над `clients.disconnect`, **второго удаления не написано**), `await daily_job(...)`, `off_since(...)`
- `fan_out_daily`, `weekly_job`, `new_report_of`, `scan_all`, `await scan_client`, `connected_clients`, `set_notifier(fn(client_id, event))`, `register_jobs(*, path)`; имена работ `DAILY_JOB`, `WEEKLY_JOB`, `SCAN_ALL`, `SCAN_ONE`
- **Ключи в `clients.settings`:** `notifications`, `lifecycle` (у таска 05 там же `token_reminders` - читай и сохраняй чужие ключи)
- `bot.handlers.settings`: `register(app, *, path=None)`, команда `/settings`, префикс callback `set:`, `settings_text(prefs)`, `keyboard(prefs)`, `notice(event)`, `make_notifier(app, *, path)`, `TIMES`

**Что надо знать про расписание:**

- **Утренняя рассылка РНП перерегистрирована поверх агентской** под тем же именем `rnp_report`: тумблеры и время клиента знает жизненный цикл, а не агент. **Сбор `rnp_collect` не тронут и идёт у всех подключённых кабинетов.** `register_jobs()` сам сначала зовёт `rnp.register_jobs()`, поэтому порядок не зависит от алфавита в реестре хендлеров
- Недельная рассылка опирается на новые строки в `fin_weeks`. Чтобы они появлялись без просьбы клиента, добавлена суточная работа `lifecycle_finance_scan` (сбор финансиста тем, у кого модуль работает)
- **`agents.lifecycle` импортирует `agents.finance` и `agents.rnp`** - первый случай, когда `core` смотрит вверх на агентов. Цикла нет, но если понадобится развязка - имена задач передаются параметром при регистрации

### Из таска 13 - бесплатная диагностика

- `agents.diagnostic`: `TASK_KIND="diagnostic"`, `PERIOD="month"`, `TOP=3`, `CATEGORY="finance"`, исходы `OK`, `NOT_CONNECTED`, `NO_CATEGORY`, `TAKEN`, `REPEAT`, `NO_DATA`, `COST_TITLES`
- `run(seller_id, client_id, *, today=None, path=None) -> Diagnostic`, `availability(client_id, *, path=None) -> str`, `previous(seller_id, *, path=None)`, `seller_of`, `has_category`, `module_lines()`, `leaks_of(alerts)`, `biggest_costs(finance.Week)`, `request(client_id, *, path=None)`, `set_delivery(fn)`, `await diagnostic_task(...)`, `register_jobs(*, path=None)`
- `Leak(title, rubles, metric, was, now, deviation)`, `ModuleLine(module, title, line)`, `Diagnostic(...)` плюс `.ok`, `.enough`
- `bot.handlers.diagnostic`: `register(app, *, path=None)`, команда `/diagnostic` **без `require_module`** (она бесплатная), `report_text`, `refusal_text`, `leak_line`, `make_delivery`

**Решения, принятые осознанно:**

- **Утечки считает сторож, своей формулы нет:** берутся его `Alert.rubles`. Мало истории - показываются крупнейшие статьи расходов с честной оговоркой, что это не отклонение
- **Привязка к кабинету, а не к Telegram:** ключ это `seller_id` из токена. Второй аккаунт с тем же кабинетом разбора не получает
- **`NO_DATA` не записывается в `diagnostics`:** бесплатный разбор не потрачен, человек может прийти снова, когда у WB появятся данные
- **Сбор недели задача делает сама через `finance.collect`, а не через `request_collect`:** две отдельные задачи в очереди не дают гарантии порядка, и диагностика могла бы посчитаться до сбора
