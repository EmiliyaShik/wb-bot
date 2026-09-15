window.STATE =
{
  "slug": "wbrentgen-platform",
  "dir": "2026-09-15-wbrentgen-platform--wip",
  "title": "WBРентген - платформа платных модулей аналитики Wildberries",
  "mode": "semi",
  "depth": "normal",
  "polish": null,
  "tier": "T3",
  "briefFile": "2026-09-15-brief.md",
  "memoryFile": "CLAUDE.md",
  "skillDir": "/c/Users/emili/.claude/skills/autopilot",
  "startedAt": "2026-09-15T21:35:25+03:00",
  "updatedAt": "2026-09-15T23:09:34+03:00",
  "finishedAt": null,
  "stages": [
    {
      "id": "preflight",
      "status": "done",
      "startedAt": "2026-09-15T21:35:25+03:00",
      "finishedAt": "2026-09-15T21:38:46+03:00"
    },
    {
      "id": "manifest",
      "status": "done",
      "startedAt": "2026-09-15T21:38:46+03:00",
      "finishedAt": "2026-09-15T21:42:25+03:00"
    },
    {
      "id": "briefing",
      "status": "done",
      "startedAt": "2026-09-15T21:42:25+03:00",
      "finishedAt": "2026-09-15T21:57:15+03:00"
    },
    {
      "id": "spec",
      "status": "done",
      "startedAt": "2026-09-15T21:57:15+03:00",
      "finishedAt": "2026-09-15T22:28:14+03:00"
    },
    {
      "id": "plan",
      "status": "done",
      "startedAt": "2026-09-15T22:28:14+03:00",
      "note": "14 тасков, 6 волн, ярус T3",
      "finishedAt": "2026-09-15T22:29:03+03:00"
    },
    {
      "id": "build",
      "status": "active",
      "startedAt": "2026-09-15T22:29:03+03:00",
      "note": "0 из 14 тасков готовы"
    },
    {
      "id": "review",
      "status": "active",
      "startedAt": "2026-09-15T22:44:50+03:00",
      "note": "01 в ремонте"
    },
    {
      "id": "final",
      "status": "pending"
    }
  ],
  "requirements": {
    "total": 176,
    "done": 2,
    "inTicket": 147,
    "inSpec": 6,
    "placeholder": 3,
    "deferred": 17,
    "dropped": 1
  },
  "tickets": [
    {
      "id": "01",
      "title": "Каркас: конфиг, база, шифрование, журнал, сохранение старой функции",
      "requirements": [
        "R02",
        "R10",
        "R12",
        "R13",
        "R14",
        "R46",
        "R47",
        "R48",
        "R53",
        "R57",
        "R58",
        "R59",
        "R60",
        "R61",
        "R62",
        "R140",
        "R141",
        "R142",
        "R143",
        "R144",
        "R145",
        "R149",
        "R151",
        "R152",
        "R154",
        "R155",
        "R156",
        "R159",
        "R167",
        "R173i",
        "R174i",
        "R175i",
        "R147"
      ],
      "blockedBy": [],
      "wave": 1,
      "zone": [
        "config.toml",
        "core/config.py",
        "core/db/",
        "core/crypto.py",
        "core/audit.py",
        "migrations/",
        "legacy/",
        "bot/app.py",
        "bot/handlers/__init__.py",
        "bot/texts.py",
        "tests/",
        "requirements.txt",
        ".env.example",
        "bot.py"
      ],
      "status": "repair",
      "retries": 0,
      "repairs": 2,
      "handoffs": 0,
      "startedAt": "2026-09-15T22:29:03+03:00",
      "repairFindings": [
        "DATA_DIR при пустой переменной уводил базу в /app/data локально",
        "admin_repo давал произвольный SQL по клиентским таблицам - обход изоляции (R147)",
        "деньги в схеме REAL вместо копеек, а новых миграций не будет",
        "в схеме не было места под расход нейросети (R144)",
        "redact вырезал артикулы WB и Telegram ID из журнала",
        "INTRO обещал /connect, которого ещё нет",
        "[token_categories] расходилась со спецификацией",
        "тест на длинные тире уходил в skip вместо падения, когда нарушение найдено в занятом файле",
        "в карте [token_categories] не было категории statistics",
        "общий слой всё ещё писал клиентские строки мимо repo(client_id)"
      ]
    },
    {
      "id": "02",
      "title": "Клиент WB API: авторизация, лимиты, ошибки, проверка связи",
      "requirements": [
        "R05",
        "R06",
        "R15",
        "R16",
        "R17",
        "R19",
        "R20",
        "G01",
        "R24",
        "R26",
        "R37",
        "R39",
        "R40",
        "R40.1",
        "R41",
        "R49",
        "R93",
        "R112",
        "R120",
        "R146",
        "R150",
        "R166"
      ],
      "blockedBy": [
        "01"
      ],
      "wave": 2,
      "zone": [
        "core/wbapi/",
        "docs/wb-api.md",
        "bot/handlers/diag.py"
      ],
      "status": "pending",
      "retries": 0,
      "repairs": 0,
      "handoffs": 0
    },
    {
      "id": "03",
      "title": "Очередь фоновых задач и расписание",
      "requirements": [
        "R36",
        "R38",
        "R41",
        "R42",
        "R43",
        "R44",
        "R166"
      ],
      "blockedBy": [
        "01"
      ],
      "wave": 2,
      "zone": [
        "core/queue.py",
        "core/scheduler.py"
      ],
      "status": "pending",
      "retries": 0,
      "repairs": 0,
      "handoffs": 0
    },
    {
      "id": "04",
      "title": "Доступ к модулям: одна дверь, статусы, пауза, /tariffs",
      "requirements": [
        "R53",
        "R57",
        "R58",
        "R59",
        "R60",
        "R61",
        "R63",
        "R64",
        "R65",
        "R66",
        "R67",
        "R68",
        "R70",
        "R30",
        "R141"
      ],
      "blockedBy": [
        "01"
      ],
      "wave": 2,
      "zone": [
        "core/access.py",
        "bot/handlers/tariffs.py"
      ],
      "status": "pending",
      "retries": 0,
      "repairs": 0,
      "handoffs": 0
    },
    {
      "id": "05",
      "title": "Подключение кабинета: оферта, токен, проверка, отключение",
      "requirements": [
        "R18",
        "R19",
        "R20",
        "G01",
        "R21",
        "R22",
        "R23",
        "R25",
        "R26",
        "R27",
        "R28",
        "R29",
        "R30",
        "R31",
        "R32",
        "R40",
        "R40.1",
        "R140",
        "R160",
        "R161",
        "R169i"
      ],
      "blockedBy": [
        "02",
        "04"
      ],
      "wave": 3,
      "zone": [
        "core/clients.py",
        "bot/handlers/connect.py"
      ],
      "status": "pending",
      "retries": 0,
      "repairs": 0,
      "handoffs": 0
    },
    {
      "id": "06",
      "title": "Себестоимость: шаблон Excel, приём файла, хранение",
      "requirements": [
        "R33",
        "R33.1",
        "R34",
        "R34.1",
        "R35",
        "R143",
        "R158",
        "R168"
      ],
      "blockedBy": [
        "02",
        "03"
      ],
      "wave": 3,
      "zone": [
        "core/costs.py",
        "core/xlsx.py",
        "bot/handlers/costs.py"
      ],
      "status": "pending",
      "retries": 0,
      "repairs": 0,
      "handoffs": 0
    },
    {
      "id": "07",
      "title": "Счета: ИНН, реквизиты, PDF, статусы, оплата",
      "requirements": [
        "R72",
        "R76",
        "R77",
        "R78",
        "R79",
        "R80",
        "R81",
        "R82",
        "R83",
        "R84",
        "R85",
        "R86",
        "R90",
        "R91",
        "R142",
        "R163",
        "R164",
        "R170i",
        "R175i",
        "R176i",
        "R148"
      ],
      "blockedBy": [
        "03",
        "04"
      ],
      "wave": 3,
      "zone": [
        "core/billing/",
        "bot/handlers/billing.py"
      ],
      "status": "pending",
      "retries": 0,
      "repairs": 0,
      "handoffs": 0
    },
    {
      "id": "08",
      "title": "Владелец: статистика, акты, ручная выдача, упавшие задачи",
      "requirements": [
        "R49",
        "R51",
        "R52",
        "R73",
        "R74",
        "R75",
        "R87",
        "R144",
        "R156",
        "R157",
        "R159"
      ],
      "blockedBy": [
        "03",
        "04",
        "06",
        "07"
      ],
      "wave": 4,
      "zone": [
        "core/metering.py",
        "core/ratelimit.py",
        "bot/handlers/admin.py"
      ],
      "status": "pending",
      "retries": 0,
      "repairs": 0,
      "handoffs": 0
    },
    {
      "id": "09",
      "title": "Агент 1. Финансист: недельная раскладка и Excel",
      "requirements": [
        "R93",
        "R94",
        "R95",
        "R96",
        "R97",
        "R98",
        "R99",
        "R100",
        "R101",
        "R102",
        "R143",
        "R165",
        "R48",
        "R03"
      ],
      "blockedBy": [
        "02",
        "03",
        "04",
        "06"
      ],
      "wave": 4,
      "zone": [
        "agents/finance.py",
        "core/excel/",
        "bot/handlers/finance.py"
      ],
      "status": "pending",
      "retries": 0,
      "repairs": 0,
      "handoffs": 0
    },
    {
      "id": "10",
      "title": "Агент 2. Сторож скрытых расходов: алерты и динамика",
      "requirements": [
        "R103",
        "R104",
        "R105",
        "R106",
        "R107",
        "R108",
        "R109",
        "R110",
        "R111",
        "R43"
      ],
      "blockedBy": [
        "09"
      ],
      "wave": 5,
      "zone": [
        "agents/watchdog.py",
        "bot/handlers/dynamics.py"
      ],
      "status": "pending",
      "retries": 0,
      "repairs": 0,
      "handoffs": 0
    },
    {
      "id": "11",
      "title": "Агент 3. Прибыльность артикулов",
      "requirements": [
        "R112",
        "R112.1",
        "R113",
        "R114",
        "R115",
        "R116",
        "R117",
        "R118",
        "R35",
        "R168"
      ],
      "blockedBy": [
        "06",
        "09"
      ],
      "wave": 5,
      "zone": [
        "agents/profit.py",
        "bot/handlers/profit.py"
      ],
      "status": "pending",
      "retries": 0,
      "repairs": 0,
      "handoffs": 0
    },
    {
      "id": "12",
      "title": "Агент 4. РНП: план-факт, остатки, ежедневный отчёт",
      "requirements": [
        "R119",
        "R120",
        "R121",
        "R122",
        "R123",
        "R124",
        "R125",
        "R143",
        "R48"
      ],
      "blockedBy": [
        "02",
        "03",
        "04"
      ],
      "wave": 3,
      "zone": [
        "agents/rnp.py",
        "bot/handlers/rnp.py"
      ],
      "status": "pending",
      "retries": 0,
      "repairs": 0,
      "handoffs": 0
    },
    {
      "id": "13",
      "title": "Бесплатная диагностика: три утечки в рублях",
      "requirements": [
        "R54",
        "R55",
        "R56",
        "R162"
      ],
      "blockedBy": [
        "04",
        "10"
      ],
      "wave": 6,
      "zone": [
        "agents/diagnostic.py",
        "bot/handlers/diagnostic.py"
      ],
      "status": "pending",
      "retries": 0,
      "repairs": 0,
      "handoffs": 0
    },
    {
      "id": "14",
      "title": "Жизненный цикл: рассылки, настройки, продление, удаление данных",
      "requirements": [
        "R42",
        "R43",
        "R44",
        "R45",
        "R69",
        "R70",
        "R71",
        "R28"
      ],
      "blockedBy": [
        "03",
        "04",
        "05",
        "09",
        "12"
      ],
      "wave": 5,
      "zone": [
        "core/lifecycle.py",
        "bot/handlers/settings.py"
      ],
      "status": "pending",
      "retries": 0,
      "repairs": 0,
      "handoffs": 0
    }
  ],
  "singlePass": null,
  "tests": {
    "passed": 57,
    "failed": 0
  },
  "debt": {
    "placeholders": [],
    "assumptions": [],
    "emptyEnv": []
  },
  "additions": [],
  "coverage": {
    "found": 9,
    "fixed": 9,
    "deferred": 0
  },
  "concerns": [
    "bot/texts.py - экран знакомства без кнопки «Подключить кабинет»: её адресат появится в таске 05",
    "bot/handlers/__init__.py - параметр package у register_all существует ради тестов и не заявлен в interfaces.md",
    "core/audit.py - голый ИНН из 12 цифр без слова рядом остаётся в журнале: цена адресной чистки, иначе резались бы артикулы WB",
    "README.md - 12 длинных тире и упоминание удалённого wb_api.py; переписывается в конце сборки"
  ],
  "reviewers": {
    "manifestSpec": "a4b8807859a74a73a",
    "craft": "ae17f2a9e67ca3c5c"
  },
  "blind": null
}
