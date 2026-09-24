-- Полная схема WBРентген. Откат не поддерживается: только вперёд.
-- Каждая таблица с данными клиента имеет client_id и ссылается на clients
-- с ON DELETE CASCADE - тогда удаление клиента убирает всё разом и без остатка.
--
-- Деньги. Все денежные поля это целые копейки, колонка называется *_kop.
-- REAL для денег не используется нигде: 0.1 + 0.2 в двоичной дробной
-- арифметике не равно 0.3, а отчёт клиенту должен сходиться до копейки.
-- Агент поднимает значение как Decimal(value) / 100 и обратно кладёт целым.
-- Проценты и доли остаются REAL: это не деньги, копейки им не нужны.

CREATE TABLE IF NOT EXISTS clients (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id  INTEGER NOT NULL UNIQUE,
    seller_id    TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    settings     TEXT NOT NULL DEFAULT '{}',
    paused_since TEXT
);

CREATE TABLE IF NOT EXISTS consents (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id     INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    offer_url     TEXT NOT NULL DEFAULT '',
    offer_version TEXT NOT NULL DEFAULT '',
    agreed_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_consents_client ON consents(client_id);

CREATE TABLE IF NOT EXISTS wb_tokens (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id   INTEGER NOT NULL UNIQUE REFERENCES clients(id) ON DELETE CASCADE,
    ciphertext  BLOB NOT NULL,
    exp         TEXT,
    scopes      TEXT NOT NULL DEFAULT '',
    read_only   INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    last_ok_at  TEXT,
    last_401_at TEXT
);

CREATE TABLE IF NOT EXISTS module_access (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id  INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    module     TEXT NOT NULL,
    until      TEXT,
    source     TEXT NOT NULL DEFAULT 'manual',
    state      TEXT NOT NULL DEFAULT 'off',
    paused_at  TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (client_id, module)
);

CREATE TABLE IF NOT EXISTS access_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id   INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    module      TEXT NOT NULL,
    action      TEXT NOT NULL,
    days        INTEGER,
    payment_ref TEXT,
    method      TEXT,
    actor       TEXT,
    at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_access_log_client ON access_log(client_id, at);
-- Один платёж продлевает доступ один раз. Замок в core.access это правило уже
-- держит, но только внутри процесса: второй процесс бота на той же базе его
-- не видит, а правило, которое держится на «мы пока не масштабировались»,
-- правилом не является. Уникальность в базе остаётся и без замка.
-- Индекс в пределах клиента, как и проверка в core.access: номер счёта
-- сквозной, а вот «trial:...» и ручные основания у клиентов свои.
-- Только action = 'grant': строк duplicate по одному платежу бывает сколько
-- угодно, а pause, resume и revoke идут вообще без номера платежа.
CREATE UNIQUE INDEX IF NOT EXISTS idx_access_log_grant_once
    ON access_log(client_id, payment_ref)
    WHERE action = 'grant';

CREATE TABLE IF NOT EXISTS invoices (
    number        TEXT PRIMARY KEY,
    client_id     INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    inn           TEXT,
    org_name      TEXT,
    org_address   TEXT,
    module        TEXT NOT NULL,
    period_months INTEGER NOT NULL,
    amount_kop    INTEGER NOT NULL,
    status        TEXT NOT NULL DEFAULT 'issued',
    issued_at     TEXT NOT NULL DEFAULT (datetime('now')),
    due_at        TEXT,
    paid_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_invoices_client ON invoices(client_id, status);

CREATE TABLE IF NOT EXISTS invoice_seq (
    year        INTEGER PRIMARY KEY,
    last_number INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS costs (
    client_id         INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    nm_id             INTEGER NOT NULL,
    cost_per_unit_kop INTEGER NOT NULL,
    updated_at        TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (client_id, nm_id)
);

-- Названия карточек товаров. В отчёте о реализации названия нет вовсе: там
-- nmId, vendorCode, brandName и subjectName, а subjectName это предмет, то
-- есть категория («Наматрасник»), а не название карточки. Название живёт в
-- другом методе (карточки товаров, категория токена «Контент»), и держать его
-- здесь нужно затем, чтобы не ходить в WB на каждый отчёт.
--
-- Пустой title это не «не спрашивали», а «спросили, и карточки у WB нет»:
-- товар могли удалить из кабинета. Разница важна, потому что именно по ней
-- решается, идти ли в Wildberries ещё раз.
CREATE TABLE IF NOT EXISTS card_names (
    client_id  INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    nm_id      INTEGER NOT NULL,
    title      TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (client_id, nm_id)
);

CREATE TABLE IF NOT EXISTS fin_weeks (
    client_id              INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    report_id              INTEGER NOT NULL,
    date_from              TEXT NOT NULL,
    date_to                TEXT NOT NULL,
    create_dt              TEXT,
    revenue_kop            INTEGER NOT NULL DEFAULT 0,
    for_pay_kop            INTEGER NOT NULL DEFAULT 0,
    commission_kop         INTEGER NOT NULL DEFAULT 0,
    acquiring_kop          INTEGER NOT NULL DEFAULT 0,
    logistics_kop          INTEGER NOT NULL DEFAULT 0,
    storage_kop            INTEGER NOT NULL DEFAULT 0,
    penalties_kop          INTEGER NOT NULL DEFAULT 0,
    deductions_kop         INTEGER NOT NULL DEFAULT 0,
    additional_payment_kop INTEGER NOT NULL DEFAULT 0,
    acceptance_kop         INTEGER NOT NULL DEFAULT 0,
    returns_amount_kop     INTEGER NOT NULL DEFAULT 0,
    sales_count            INTEGER NOT NULL DEFAULT 0,
    returns_count          INTEGER NOT NULL DEFAULT 0,
    -- сверка: то же «к перечислению», но из sales-reports/list
    control_for_pay_kop    INTEGER,
    control_payload        TEXT,
    loaded_at              TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (client_id, report_id)
);
CREATE INDEX IF NOT EXISTS idx_fin_weeks_period ON fin_weeks(client_id, date_from, date_to);

-- Строки отчёта в исходных именах WB. Денежные поля переведены в копейки и
-- получили суффикс _kop; соответствие имён WB подписано в комментариях.
CREATE TABLE IF NOT EXISTS fin_rows (
    client_id                 INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    report_id                 INTEGER NOT NULL,
    rrd_id                    INTEGER NOT NULL,
    nm_id                     INTEGER,
    sa_name                   TEXT,
    ts_name                   TEXT,
    subject_name              TEXT,
    barcode                   TEXT,
    doc_type_name             TEXT,
    supplier_oper_name        TEXT,
    quantity                  INTEGER,
    retail_price_kop          INTEGER,   -- retail_price
    retail_amount_kop         INTEGER,   -- retail_amount
    retail_price_withdisc_kop INTEGER,   -- retail_price_withdisc_rub
    sale_percent              REAL,
    commission_percent        REAL,
    delivery_amount           INTEGER,
    return_amount             INTEGER,
    delivery_kop              INTEGER,   -- delivery_rub
    ppvz_spp_prc              REAL,
    ppvz_kvw_prc_base         REAL,
    ppvz_kvw_prc              REAL,
    ppvz_sales_commission_kop INTEGER,   -- ppvz_sales_commission
    ppvz_for_pay_kop          INTEGER,   -- ppvz_for_pay
    ppvz_reward_kop           INTEGER,   -- ppvz_reward
    acquiring_fee_kop         INTEGER,   -- acquiring_fee
    acquiring_percent         REAL,
    acquiring_bank            TEXT,
    penalty_kop               INTEGER,   -- penalty
    additional_payment_kop    INTEGER,   -- additional_payment
    storage_fee_kop           INTEGER,   -- storage_fee
    deduction_kop             INTEGER,   -- deduction
    acceptance_kop            INTEGER,   -- acceptance
    rebill_logistic_cost_kop  INTEGER,   -- rebill_logistic_cost
    bonus_type_name           TEXT,
    order_dt                  TEXT,
    sale_dt                   TEXT,
    rr_dt                     TEXT,
    srid                      TEXT,
    raw                       TEXT,      -- вся строка WB целиком, JSON
    PRIMARY KEY (client_id, rrd_id)
);
CREATE INDEX IF NOT EXISTS idx_fin_rows_report ON fin_rows(client_id, report_id);
CREATE INDEX IF NOT EXISTS idx_fin_rows_nm ON fin_rows(client_id, nm_id);

CREATE TABLE IF NOT EXISTS nm_daily (
    client_id         INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    date              TEXT NOT NULL,
    nm_id             INTEGER NOT NULL,
    orders            INTEGER NOT NULL DEFAULT 0,
    orders_sum_kop    INTEGER NOT NULL DEFAULT 0,
    buyouts           INTEGER NOT NULL DEFAULT 0,
    buyouts_sum_kop   INTEGER NOT NULL DEFAULT 0,
    open_card_count   INTEGER NOT NULL DEFAULT 0,
    add_to_cart_count INTEGER NOT NULL DEFAULT 0,
    cart_to_order_pct REAL,
    buyout_pct        REAL,
    stocks_wb         INTEGER,
    stocks_mp         INTEGER,
    ad_spend_kop      INTEGER NOT NULL DEFAULT 0,
    views             INTEGER,
    clicks            INTEGER,
    raw               TEXT,
    updated_at        TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (client_id, date, nm_id)
);
CREATE INDEX IF NOT EXISTS idx_nm_daily_date ON nm_daily(client_id, date);

-- Реклама. Копится у нас по той же причине, что и суточная воронка, но
-- граница другая: Wildberries отдаёт статистику только по кампаниям в
-- статусах 7, 9 и 11 (завершена, активна, на паузе). Кампания, которую
-- селлер удалил или отменил, уносит свою историю с собой, и выгрузить её
-- задним числом уже нельзя.
CREATE TABLE IF NOT EXISTS ad_campaigns (
    client_id   INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    advert_id   INTEGER NOT NULL,
    -- Название кампании пишет сам селлер. Это чужой текст: в книгу Excel он
    -- едет как есть, а в сообщение бота только через bot.texts.fill.
    name        TEXT NOT NULL DEFAULT '',
    advert_type INTEGER,
    status      INTEGER,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (client_id, advert_id)
);

-- Кампания за сутки. Разрез готовый, складывать его не приходится:
-- spend_kop это поле sum, ad_revenue_kop это поле sum_price, и оба лежат в
-- одной строке ответа. Именно поэтому ДРР рекламы считается внутри одного
-- источника, без сшивки с финансовым отчётом.
CREATE TABLE IF NOT EXISTS ad_daily (
    client_id      INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    date           TEXT NOT NULL,
    advert_id      INTEGER NOT NULL,
    views          INTEGER NOT NULL DEFAULT 0,
    clicks         INTEGER NOT NULL DEFAULT 0,
    atbs           INTEGER NOT NULL DEFAULT 0,
    orders         INTEGER NOT NULL DEFAULT 0,
    shks           INTEGER NOT NULL DEFAULT 0,
    canceled       INTEGER NOT NULL DEFAULT 0,
    spend_kop      INTEGER NOT NULL DEFAULT 0,
    ad_revenue_kop INTEGER NOT NULL DEFAULT 0,
    updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (client_id, date, advert_id)
);
CREATE INDEX IF NOT EXISTS idx_ad_daily_date ON ad_daily(client_id, date);

-- Кампания, сутки и артикул. Нужна второй цифре ДРР: без неё неизвестно,
-- какие товары кампания рекламировала, а значит и с какой выручкой её
-- расход сравнивать. Расход по артикулу размазан по площадкам, и здесь он
-- уже сложен.
CREATE TABLE IF NOT EXISTS ad_nm_daily (
    client_id      INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    date           TEXT NOT NULL,
    advert_id      INTEGER NOT NULL,
    nm_id          INTEGER NOT NULL,
    views          INTEGER NOT NULL DEFAULT 0,
    clicks         INTEGER NOT NULL DEFAULT 0,
    atbs           INTEGER NOT NULL DEFAULT 0,
    orders         INTEGER NOT NULL DEFAULT 0,
    shks           INTEGER NOT NULL DEFAULT 0,
    spend_kop      INTEGER NOT NULL DEFAULT 0,
    ad_revenue_kop INTEGER NOT NULL DEFAULT 0,
    updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (client_id, date, advert_id, nm_id)
);
CREATE INDEX IF NOT EXISTS idx_ad_nm_daily_date ON ad_nm_daily(client_id, date);

-- Фактически списанные суммы. Статистический расход (sum) и выставленная
-- сумма (updSum) расходятся: часть расхода могла уйти бонусами или кэшбэком,
-- и об этом говорит payment_type. Показываются обе цифры, расхождение не
-- прячется - тот же приём, что и в сверке финансового отчёта.
CREATE TABLE IF NOT EXISTS ad_upd (
    client_id    INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    date         TEXT NOT NULL,
    advert_id    INTEGER NOT NULL,
    upd_num      INTEGER NOT NULL DEFAULT 0,
    sum_kop      INTEGER NOT NULL DEFAULT 0,
    payment_type TEXT NOT NULL DEFAULT '',
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (client_id, date, advert_id, upd_num)
);
CREATE INDEX IF NOT EXISTS idx_ad_upd_date ON ad_upd(client_id, date);

-- Пятый этап воронки: видимость товара в поиске. Это НЕ показы и не штуки, а
-- вероятность в процентах, что покупатель увидит карточку; Wildberries считает
-- её по средней позиции. Раздел поисковых запросов доступен только с подпиской
-- Джем, поэтому у большинства кабинетов эта таблица останется пустой, и это
-- нормальное состояние, а не сбой: без неё воронка живёт четырьмя этапами.
--
-- Ключ включает границы периода, а не одну дату: видимость приходит средней
-- за запрошенный период и одному дню не принадлежит. Отчёт спрашивает ровно
-- те границы, за которые сам и собран, поэтому промаха тут быть не может.
CREATE TABLE IF NOT EXISTS funnel_visibility (
    client_id  INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    date_from  TEXT NOT NULL,
    date_to    TEXT NOT NULL,
    nm_id      INTEGER NOT NULL,
    -- Проценты и доли в этом проекте остаются REAL: деньгами они не являются.
    visibility REAL,
    -- Динамика против прошлого периода в процентах, готовое поле Wildberries.
    -- Видимость прошлого периода он не отдаёт, а вычислять её из динамики
    -- нельзя: сама видимость приходит целым процентом, и деление на такой
    -- округлённой цифре наврало бы больше, чем показало.
    dynamics   REAL,
    open_card  INTEGER,
    avg_position REAL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (client_id, date_from, date_to, nm_id)
);

CREATE TABLE IF NOT EXISTS plans (
    client_id          INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    year_month         TEXT NOT NULL,
    revenue_target_kop INTEGER,
    orders_target      INTEGER,
    updated_at         TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (client_id, year_month)
);

-- Диагностика и пробный период привязаны к кабинету WB, а не к Telegram:
-- иначе один и тот же продавец получил бы их заново с нового аккаунта.
CREATE TABLE IF NOT EXISTS diagnostics (
    seller_id TEXT PRIMARY KEY,
    done_at   TEXT NOT NULL DEFAULT (datetime('now')),
    summary   TEXT
);

CREATE TABLE IF NOT EXISTS trials (
    seller_id  TEXT NOT NULL,
    module     TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (seller_id, module)
);

CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id   INTEGER REFERENCES clients(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    payload     TEXT NOT NULL DEFAULT '{}',
    state       TEXT NOT NULL DEFAULT 'queued',
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    next_run_at TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state, next_run_at);
-- Одна и та же работа одного клиента не стоит в очереди дважды. Проверять это
-- запросом перед вставкой нельзя: хендлеры выполняются одновременно, и два
-- нажатия подряд оба увидели бы пустую очередь. Уникальность держит база, а
-- вставка идёт одним оператором с ON CONFLICT DO NOTHING.
-- Задача без клиента (расписание) тоже считается: COALESCE вместо NULL нужен
-- потому, что в уникальном индексе SQLite все NULL различны между собой.
-- Доделанные задачи (done, failed, cancelled) в индекс не попадают: повторить
-- вчерашний отчёт клиент имеет полное право.
CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_no_duplicates
    ON tasks(COALESCE(client_id, 0), kind, payload)
    WHERE state IN ('queued', 'running');

CREATE TABLE IF NOT EXISTS api_calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id   INTEGER REFERENCES clients(id) ON DELETE CASCADE,
    host        TEXT NOT NULL,
    method      TEXT NOT NULL,
    status      INTEGER,
    duration_ms INTEGER,
    at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_api_calls_at ON api_calls(at);

-- Расход нейросети: журнал требует показывать не только вызовы WB, но и
-- деньги за модель. Этап 3 начнёт сюда писать, до тех пор таблица пустует.
CREATE TABLE IF NOT EXISTS ai_calls (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id         INTEGER REFERENCES clients(id) ON DELETE CASCADE,
    provider          TEXT NOT NULL DEFAULT '',
    model             TEXT NOT NULL DEFAULT '',
    kind              TEXT NOT NULL DEFAULT '',
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost_kop          INTEGER NOT NULL DEFAULT 0,
    at                TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ai_calls_at ON ai_calls(at);

CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at        TEXT NOT NULL DEFAULT (datetime('now')),
    level     TEXT NOT NULL DEFAULT 'info',
    client_id INTEGER REFERENCES clients(id) ON DELETE CASCADE,
    kind      TEXT NOT NULL,
    message   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_at ON events(at);
CREATE INDEX IF NOT EXISTS idx_events_level ON events(level, at);
