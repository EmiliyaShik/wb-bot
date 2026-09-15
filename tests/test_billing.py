"""Счета: банковские дни, ИНН, нумерация, статусы, оплата.

Шов один и тот же, что у всего проекта, - путь к базе (фикстура db_path).
Чистые расчёты (банковские дни, контрольная сумма ИНН) проверяются напрямую.
"""

import logging
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from core import access, audit, billing, config, db
from core.billing import bankdays, counterparty
from core.billing import pdf as invoice_pdf

# Опорный момент тестов: пятница 18 сентября 2026 года. Все ожидаемые даты и
# номера счетов посчитаны от него руками, а не этим же кодом.
FRIDAY = datetime(2026, 9, 18, 10, 0, tzinfo=timezone.utc)


class TestBankDays:
    """Банковский день это будний день, минус праздники из конфига."""

    def test_friday_invoice_expires_next_friday(self):
        # Дословно из критерия приёмки: счёт в пятницу истекает в следующую
        # пятницу. Пять банковских дней это понедельник, вторник, среда,
        # четверг, пятница.
        friday = date(2026, 9, 18)
        assert friday.weekday() == 4
        assert bankdays.add_bank_days(friday, 5) == date(2026, 9, 25)

    def test_weekend_does_not_count(self):
        # Вторник плюс один банковский день это среда, а пятница плюс один
        # это понедельник: суббота и воскресенье не считаются.
        assert bankdays.add_bank_days(date(2026, 9, 15), 1) == date(2026, 9, 16)
        assert bankdays.add_bank_days(date(2026, 9, 18), 1) == date(2026, 9, 21)

    def test_holiday_is_skipped(self):
        # 2026-09-21 понедельник. Объявим его нерабочим - срок сдвинется
        # на вторник.
        holidays = (date(2026, 9, 21),)
        assert bankdays.add_bank_days(date(2026, 9, 18), 1, holidays) == date(2026, 9, 22)

    def test_start_on_holiday_still_counts_forward(self):
        # Отсчёт идёт от дня выставления вперёд, сам этот день не считается.
        assert bankdays.add_bank_days(date(2026, 9, 19), 1) == date(2026, 9, 21)

    def test_holidays_from_config_are_empty_by_default(self):
        assert bankdays.holidays() == ()

    def test_valid_days_from_config(self):
        assert bankdays.valid_bank_days() == 5

    def test_a_row_of_holidays_next_to_weekends(self):
        # Новогодние даты, самый честный случай: восемь нерабочих дней подряд,
        # и с обеих сторон к ним примыкают выходные. Ошибка в счётчике именно
        # тут даёт неверный срок, и клиент получает «просрочено» раньше времени.
        #
        # 31 декабря 2025 среда, 1 января 2026 четверг, 3 и 4 января выходные,
        # 5-8 января нерабочие, 9 января пятница и первый рабочий день.
        holidays = [date(2026, 1, day) for day in range(1, 9)]
        start = date(2025, 12, 31)
        assert start.weekday() == 2
        assert date(2026, 1, 9).weekday() == 4

        assert bankdays.add_bank_days(start, 1, holidays) == date(2026, 1, 9)
        # Пять банковских дней это 9, 12, 13, 14 и 15 января: 10 и 11 выходные.
        assert bankdays.add_bank_days(start, 5, holidays) == date(2026, 1, 15)
        # Внутри череды рабочих дней нет ни одного, включая будни.
        assert bankdays.is_bank_day(date(2026, 1, 6), holidays) is False
        assert bankdays.is_bank_day(date(2026, 1, 9), holidays) is True


class TestInnChecksum:
    """Контрольная сумма ИНН считается локально, наружу ничего не уходит.

    Номера синтетические и никому не принадлежат: репозиторий публичный, и
    настоящему ИНН живой организации тут делать нечего. Контрольные цифры
    посчитаны вручную по коэффициентам из приказа, а не этим же кодом.
    1234567894: 1*2+2*4+3*10+4*3+5*5+6*9+7*4+8*6+9*8 = 279, 279 % 11 % 10 = 4.
    9876543210: та же свёртка даёт 231, 231 % 11 % 10 = 0.
    123456789047: первая свёртка 257 даёт 4, вторая 282 даёт 7.
    """

    @pytest.mark.parametrize("inn", ["1234567894", "9876543210", "123456789047"])
    def test_valid(self, inn):
        assert counterparty.inn_is_valid(inn) is True

    @pytest.mark.parametrize(
        "inn",
        [
            "1234567895",      # та же строка, испорчена последняя цифра
            "123456789048",    # 12 знаков, испорчена последняя цифра
            "123456789057",    # 12 знаков, испорчена одиннадцатая цифра
            "123456789",       # девять знаков
            "12345678941",     # одиннадцать знаков
            "",                # пусто
            "abcdefghij",      # не цифры
            "12345O7894",      # латинская O вместо нуля
        ],
    )
    def test_invalid(self, inn):
        assert counterparty.inn_is_valid(inn) is False

    def test_spaces_and_dashes_are_forgiven(self):
        # Селлер копирует ИНН из реквизитов, там бывают пробелы.
        assert counterparty.inn_is_valid(" 1234567894 ") is True

    def test_normalize_keeps_only_digits(self):
        assert counterparty.normalize("123 456 7894") == "1234567894"


class TestLookup:
    """Адаптер DaData. Отсутствие ключа и пустой ответ это штатная ветка."""

    @pytest.mark.asyncio
    async def test_no_key_returns_none_without_network(self, monkeypatch):
        monkeypatch.setenv("DADATA_API_KEY", "")

        async def explode(*args, **kwargs):
            raise AssertionError("без ключа наружу ходить нельзя")

        assert await counterparty.lookup("1234567894", http=explode) is None

    @pytest.mark.asyncio
    async def test_bad_inn_never_reaches_network(self, monkeypatch):
        monkeypatch.setenv("DADATA_API_KEY", "ключ-для-теста")
        assert await counterparty.lookup("1234567895", http=_ExplodingClient()) is None

    @pytest.mark.asyncio
    async def test_found_party_returns_name_and_address(self, monkeypatch):
        monkeypatch.setenv("DADATA_API_KEY", "ключ-для-теста")
        client = _FakeClient(
            {
                "suggestions": [
                    {
                        "value": "ООО Пример",
                        "data": {
                            "inn": "1234567894",
                            "address": {"unrestricted_value": "г Пример, ул Примерная, д 1"},
                        },
                    }
                ]
            }
        )
        found = await counterparty.lookup("1234567894", http=client)
        assert found is not None
        assert found.name == "ООО Пример"
        assert found.address == "г Пример, ул Примерная, д 1"
        assert found.inn == "1234567894"

    @pytest.mark.asyncio
    async def test_empty_answer_returns_none(self, monkeypatch):
        monkeypatch.setenv("DADATA_API_KEY", "ключ-для-теста")
        assert await counterparty.lookup("1234567894", http=_FakeClient({"suggestions": []})) is None

    @pytest.mark.asyncio
    async def test_service_down_returns_none(self, monkeypatch):
        monkeypatch.setenv("DADATA_API_KEY", "ключ-для-теста")
        assert await counterparty.lookup("1234567894", http=_BrokenClient()) is None


class _Response:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    """Минимальный двойник httpx.AsyncClient: только post и закрытие."""

    def __init__(self, payload, status=200):
        self._payload = payload
        self._status = status
        self.calls: list[dict] = []

    async def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return _Response(self._payload, self._status)


class _BrokenClient:
    async def post(self, url, **kwargs):
        raise OSError("сеть недоступна")


class _ExplodingClient:
    async def post(self, url, **kwargs):
        raise AssertionError("неверный ИНН не должен уходить наружу")


SELLER_ENV = {
    "SELLER_NAME": "ИП Тестовый Тест Тестович",
    "SELLER_INN": "123456789047",
    "SELLER_OGRNIP": "300000000000000",
    "SELLER_ADDRESS": "г Тест, ул Тестовая, д 1",
    "SELLER_ACCOUNT": "40802810000000000000",
    "SELLER_BANK": "Тестовый банк",
    "SELLER_BIK": "040000000",
    "SELLER_CORR_ACCOUNT": "30101810000000000000",
}


@pytest.fixture
def seller(monkeypatch):
    """Реквизиты ИП заданы. Значения выдуманы тестом, в коде их нет."""
    for name, value in SELLER_ENV.items():
        monkeypatch.setenv(name, value)
    return SELLER_ENV


@pytest.fixture
def client_id(db_path):
    return db.admin_repo(db_path).ensure_client(555001)


class TestCreateInvoice:
    def test_number_amount_and_due_date(self, db_path, client_id, seller):
        invoice = billing.create_invoice(
            client_id, "finance", 3, inn="1234567894", now=FRIDAY, path=db_path
        )
        assert invoice.number == "WBR-2026-0001"
        assert invoice.status == billing.ISSUED
        # 990 за месяц, три месяца со скидкой 10 процентов: 2673 рубля.
        assert invoice.amount == Decimal("2673")
        assert invoice.amount_kop == 267300
        assert invoice.due_at == date(2026, 9, 25)
        assert invoice.module == "finance"
        assert invoice.period_months == 3

    def test_numbers_do_not_repeat_and_survive_reopen(self, db_path, client_id, seller):
        first = billing.create_invoice(client_id, "finance", 1, now=FRIDAY, path=db_path)
        second = billing.create_invoice(client_id, "rnp", 1, now=FRIDAY, path=db_path)
        assert {first.number, second.number} == {"WBR-2026-0001", "WBR-2026-0002"}
        db.close_all()
        third = billing.create_invoice(client_id, "finance", 1, now=FRIDAY, path=db_path)
        assert third.number == "WBR-2026-0003"

    def test_saved_invoice_is_readable_back(self, db_path, client_id, seller):
        made = billing.create_invoice(
            client_id,
            "finance",
            1,
            inn="1234567894",
            org_name="ООО Пример",
            org_address="г Пример",
            now=FRIDAY,
            path=db_path,
        )
        got = billing.invoice(made.number, path=db_path)
        assert got is not None
        assert got.org_name == "ООО Пример"
        assert got.inn == "1234567894"
        assert got.client_id == client_id

    def test_unknown_period_is_refused(self, db_path, client_id, seller):
        with pytest.raises(ValueError):
            billing.create_invoice(client_id, "finance", 7, now=FRIDAY, path=db_path)

    def test_hidden_module_is_not_sold(self, db_path, client_id, seller):
        with pytest.raises(KeyError):
            billing.create_invoice(client_id, "ads", 1, now=FRIDAY, path=db_path)


class TestMissingSellerDetails:
    def test_invoice_is_not_created_and_number_is_not_burned(
        self, db_path, client_id, monkeypatch
    ):
        for name in SELLER_ENV:
            monkeypatch.setenv(name, "")
        monkeypatch.setenv("SELLER_NAME", "ИП Тестовый Тест Тестович")

        with pytest.raises(billing.DetailsMissing) as caught:
            billing.create_invoice(client_id, "finance", 1, now=FRIDAY, path=db_path)

        # Ровно те переменные, которых не хватает, и ни одной лишней.
        assert "SELLER_NAME" not in caught.value.missing
        assert set(caught.value.missing) == set(SELLER_ENV) - {"SELLER_NAME"}
        assert billing.invoices_of(client_id, path=db_path) == []
        # Номер не израсходован: следующий удачный счёт получит первый номер.
        for name, value in SELLER_ENV.items():
            monkeypatch.setenv(name, value)
        assert billing.create_invoice(
            client_id, "finance", 1, now=FRIDAY, path=db_path
        ).number == "WBR-2026-0001"


class TestVatNote:
    def test_empty_note_is_reported(self, monkeypatch):
        assert billing.vat_note() == ""
        assert billing.vat_note_missing() is True

    def test_empty_note_says_nothing_about_vat_anywhere(self, db_path, client_id, seller):
        # Налоговый режим ИП владелец не сообщал. Ни «Без НДС», ни «НДС не
        # облагается» бот от себя не пишет: назначение платежа уходит клиенту
        # и в банк, а это утверждение о чужой системе налогообложения.
        made = billing.create_invoice(client_id, "finance", 1, now=FRIDAY, path=db_path)
        purpose = made.payment_purpose()
        assert made.number in purpose
        assert "НДС" not in purpose
        assert "ндс" not in purpose.lower()
        assert "НДС" not in invoice_pdf.content(made)["vat"]

    def test_filled_note_goes_into_the_purpose_as_written(
        self, db_path, client_id, seller, monkeypatch
    ):
        import copy

        patched = copy.deepcopy(config.settings())
        patched["invoice"]["vat_note"] = "НДС не облагается, УСН"
        monkeypatch.setattr(config, "settings", lambda: patched)
        made = billing.create_invoice(client_id, "finance", 1, now=FRIDAY, path=db_path)
        assert "НДС не облагается, УСН" in made.payment_purpose()
        assert billing.vat_note_missing() is False


class TestMonths:
    def test_month_length_follows_the_calendar(self):
        # Февраль короче: месяц доступа с 31 января это 28 дней, а не 30.
        assert billing.months_to_days(1, date(2026, 1, 31)) == 28
        assert billing.months_to_days(1, date(2026, 3, 1)) == 31
        assert billing.months_to_days(12, date(2026, 9, 18)) == 365


class TestStatuses:
    def test_overdue_after_due_date_and_only_once(self, db_path, client_id, seller):
        made = billing.create_invoice(client_id, "finance", 1, now=FRIDAY, path=db_path)
        assert made.due_at == date(2026, 9, 25)

        # В день срока счёт ещё жив.
        on_time = datetime(2026, 9, 25, 9, 0, tzinfo=timezone.utc)
        assert billing.expire_overdue(now=on_time, path=db_path) == []
        assert billing.invoice(made.number, path=db_path).status == billing.ISSUED

        later = datetime(2026, 9, 26, 9, 0, tzinfo=timezone.utc)
        expired = billing.expire_overdue(now=later, path=db_path)
        assert [x.number for x in expired] == [made.number]
        assert expired[0].status == billing.OVERDUE
        # Повтор на следующий день ничего не возвращает: напоминание одно.
        assert billing.expire_overdue(now=later, path=db_path) == []

    def test_paid_invoice_is_never_marked_overdue(self, db_path, client_id, seller):
        made = billing.create_invoice(client_id, "finance", 1, now=FRIDAY, path=db_path)
        billing.mark_paid(made.number, now=FRIDAY, path=db_path)
        later = datetime(2026, 10, 30, 9, 0, tzinfo=timezone.utc)
        assert billing.expire_overdue(now=later, path=db_path) == []
        assert billing.invoice(made.number, path=db_path).status == billing.PAID

    def test_cancel_leaves_paid_alone(self, db_path, client_id, seller):
        made = billing.create_invoice(client_id, "finance", 1, now=FRIDAY, path=db_path)
        cancelled = billing.cancel(made.number, path=db_path)
        assert cancelled.status == billing.CANCELLED
        paid_one = billing.create_invoice(client_id, "rnp", 1, now=FRIDAY, path=db_path)
        billing.mark_paid(paid_one.number, now=FRIDAY, path=db_path)
        assert billing.cancel(paid_one.number, path=db_path).status == billing.PAID

    def test_status_words_are_the_four_from_the_brief(self):
        assert set(billing.STATUS_WORDS.values()) == {
            "выставлен",
            "оплачен",
            "просрочен",
            "отменён",
        }


class TestMarkPaid:
    def test_access_turns_on_for_the_paid_period(self, db_path, client_id, seller):
        made = billing.create_invoice(client_id, "finance", 3, now=FRIDAY, path=db_path)
        result = billing.mark_paid(made.number, now=FRIDAY, path=db_path)

        assert result.duplicate is False
        assert result.invoice.status == billing.PAID
        assert access.has_access(client_id, "finance", now=FRIDAY, path=db_path)
        # Три календарных месяца с 18 сентября 2026 это 18 декабря 2026.
        assert result.granted.until.date() == date(2026, 12, 18)

    def test_second_press_does_not_extend_anything(self, db_path, client_id, seller):
        made = billing.create_invoice(client_id, "finance", 1, now=FRIDAY, path=db_path)
        first = billing.mark_paid(made.number, now=FRIDAY, path=db_path)
        later = datetime(2026, 9, 21, 9, 0, tzinfo=timezone.utc)
        second = billing.mark_paid(made.number, now=later, path=db_path)

        assert second.duplicate is True
        assert second.granted.until == first.granted.until
        assert billing.invoice(made.number, path=db_path).paid_at == first.invoice.paid_at

    def test_unknown_number_is_an_error(self, db_path, seller):
        with pytest.raises(KeyError):
            billing.mark_paid("WBR-2026-9999", path=db_path)

    def test_payment_reference_is_the_invoice_number(self, db_path, client_id, seller):
        made = billing.create_invoice(client_id, "finance", 1, now=FRIDAY, path=db_path)
        billing.mark_paid(made.number, now=FRIDAY, path=db_path)
        row = db.repo(client_id, db_path).one("access_log", action="grant")
        assert row["payment_ref"] == made.number
        assert row["method"] == "invoice"


class TestServiceWording:
    def test_wording_comes_from_config(self, db_path):
        line = billing.service_line("finance", 3)
        assert "Финансы" in line
        assert "3 месяца" in line
        # Ни одной формулировки в коде: шаблон целиком из конфига.
        template = billing.config.settings()["invoice"]["service_name"]
        assert line == template.format(module="Финансы", period="3 месяца")

    def test_months_words(self):
        assert billing.months_words(1) == "1 месяц"
        assert billing.months_words(3) == "3 месяца"
        assert billing.months_words(12) == "12 месяцев"


class TestInvoiceDocument:
    """Содержимое счёта проверяется до рисования: тут видно, что в нём есть."""

    def test_every_required_block_is_filled_from_environment(
        self, db_path, client_id, seller, monkeypatch
    ):
        monkeypatch.setenv("OFFER_URL", "https://example.invalid/oferta")
        made = billing.create_invoice(
            client_id,
            "finance",
            3,
            inn="1234567894",
            org_name="ООО Пример",
            org_address="г Пример, ул Примерная, д 1",
            now=FRIDAY,
            path=db_path,
        )
        doc = invoice_pdf.content(made)

        assert doc["number"] == "WBR-2026-0001"
        assert doc["seller"]["SELLER_INN"] == SELLER_ENV["SELLER_INN"]
        assert doc["seller"]["SELLER_ACCOUNT"] == SELLER_ENV["SELLER_ACCOUNT"]
        assert doc["buyer_name"] == "ООО Пример"
        assert doc["buyer_inn"] == "1234567894"
        # Одна строка услуги, формулировка из конфига.
        assert len(doc["rows"]) == 1
        assert doc["rows"][0]["name"] == billing.service_line("finance", 3)
        assert doc["total"] == Decimal("2673")
        # Номер счёта обязан быть в назначении платежа.
        assert made.number in doc["purpose"]
        assert doc["offer"] == "https://example.invalid/oferta"

    def test_empty_vat_note_is_reported_but_does_not_stop_the_invoice(
        self, db_path, client_id, seller
    ):
        made = billing.create_invoice(client_id, "finance", 1, now=FRIDAY, path=db_path)
        doc = invoice_pdf.content(made)
        assert doc["vat"] == ""
        assert billing.vat_note_missing() is True
        assert invoice_pdf.build(made).startswith(b"%PDF")

    def test_no_seller_details_no_pdf(self, db_path, client_id, monkeypatch):
        made = billing.Invoice(
            number="WBR-2026-0001",
            client_id=client_id,
            module="finance",
            period_months=1,
            amount_kop=99000,
            issued_at=FRIDAY,
            due_at=date(2026, 9, 25),
        )
        for name in SELLER_ENV:
            monkeypatch.setenv(name, "")
        with pytest.raises(billing.DetailsMissing):
            invoice_pdf.build(made)

    def test_nothing_is_invented_when_a_field_is_empty(
        self, db_path, client_id, monkeypatch, seller
    ):
        monkeypatch.setenv("OFFER_URL", "")
        made = billing.create_invoice(client_id, "finance", 1, now=FRIDAY, path=db_path)
        doc = invoice_pdf.content(made)
        # Оферты нет - место пустое, а не придуманная ссылка.
        assert doc["offer"] == ""

    def test_pdf_is_a_real_file(self, db_path, client_id, seller):
        made = billing.create_invoice(client_id, "finance", 1, now=FRIDAY, path=db_path)
        data = invoice_pdf.build(made)
        assert data.startswith(b"%PDF")
        assert len(data) > 1000
        assert invoice_pdf.file_name(made) == "WBR-2026-0001.pdf"


class TestConcurrentNumbering:
    def test_two_invoices_at_once_never_share_a_number(self, db_path, seller):
        # Критерий приёмки требует именно параллельной выдачи: счётчик живёт в
        # базе, и если бы он читался и писался двумя запросами, оба потока
        # получили бы один номер.
        #
        # Проверка идёт по самой выдаче номера, а не по create_invoice целиком,
        # намеренно. Общее соединение sqlite при одновременной записи из
        # нескольких потоков поднимает InterfaceError на любой вставке, чьей бы
        # она ни была; это свойство слоя данных, а не нумерации, и лечить его
        # замком в счетах значило бы прятать чужую проблему. Сквозной номер
        # обязан держаться сам, и вот это здесь и проверяется.
        import threading

        admin = db.admin_repo(db_path)
        numbers: list[str] = []
        lock = threading.Lock()
        start = threading.Event()

        def take():
            start.wait()
            number = admin.next_invoice_number(2026)
            with lock:
                numbers.append(number)

        threads = [threading.Thread(target=take) for _ in range(8)]
        for thread in threads:
            thread.start()
        start.set()
        for thread in threads:
            thread.join(timeout=20)

        assert len(numbers) == 8
        assert len(set(numbers)) == 8, f"номера повторились: {sorted(numbers)}"
        assert sorted(numbers) == [f"WBR-2026-{n:04d}" for n in range(1, 9)]

    def test_numbers_stay_unique_across_clients(self, db_path, seller):
        # Тот же номер не должен достаться двум разным клиентам.
        admin = db.admin_repo(db_path)
        first = admin.ensure_client(610100)
        second = admin.ensure_client(610200)
        one = billing.create_invoice(first, "finance", 1, now=FRIDAY, path=db_path)
        two = billing.create_invoice(second, "rnp", 1, now=FRIDAY, path=db_path)
        assert one.number != two.number
        assert billing.invoice(one.number, path=db_path).client_id == first
        assert billing.invoice(two.number, path=db_path).client_id == second


class TestNothingIsInvented:
    def test_empty_environment_leaves_every_field_empty(self, db_path, monkeypatch):
        for name in SELLER_ENV:
            monkeypatch.setenv(name, "")
        monkeypatch.setenv("OFFER_URL", "")
        monkeypatch.setenv("OWNER_CONTACT", "")
        monkeypatch.setenv("DADATA_API_KEY", "")

        # Ни одного значения по умолчанию: пустое остаётся пустым.
        assert set(billing.seller_block().values()) == {""}
        assert billing.offer_url() == ""
        assert billing.owner_contact() == ""
        assert billing.missing_details() == tuple(SELLER_ENV)

        made = billing.Invoice(
            number="WBR-2026-0001",
            client_id=1,
            module="finance",
            period_months=1,
            amount_kop=99000,
            issued_at=FRIDAY,
            due_at=date(2026, 9, 25),
        )
        doc = invoice_pdf.content(made)
        assert [value for _, value in doc["seller_lines"]] == [""] * 8
        assert doc["offer"] == ""
        assert doc["vat"] == ""


class TestSecrets:
    """Ключ DaData уходит в чужой сервис вместе с ИНН клиента.

    Значит он есть в заголовке запроса, и любая библиотека, решившая
    процитировать этот запрос в тексте своей ошибки, унесёт его в лог.
    Проверяем оба выхода наружу: журнал бота и обычное логирование.
    """

    @pytest.mark.asyncio
    async def test_key_is_in_no_log_line_and_no_journal_record(
        self, db_path, monkeypatch, caplog
    ):
        key = "0123456789abcdef0123456789abcdef"
        monkeypatch.setenv("DADATA_API_KEY", key)

        class Leaky:
            async def post(self, url, **kwargs):
                raise RuntimeError(
                    f"connect to {url} failed, headers sent: Authorization Token {key}"
                )

        with caplog.at_level(logging.DEBUG):
            found = await counterparty.lookup("1234567894", http=Leaky(), path=db_path)

        assert found is None
        # Положительный контроль: молчащий код прошёл бы проверку на утечку
        # просто потому, что ничего не написал.
        assert caplog.records, "падение DaData прошло молча мимо лога"
        journal = [str(row["message"]) for row in audit.recent(limit=50, path=db_path)]
        assert any("DaData" in text for text in journal), "падения нет в журнале"

        assert key not in caplog.text
        for text in journal:
            assert key not in text

    @pytest.mark.asyncio
    async def test_key_is_not_in_the_answer_to_the_client(self, db_path, monkeypatch):
        key = "0123456789abcdef0123456789abcdef"
        monkeypatch.setenv("DADATA_API_KEY", key)

        class Broken:
            async def post(self, url, **kwargs):
                raise OSError(f"Token {key}")

        # Клиенту возвращается None, а не текст ошибки: рассказывать ему
        # про ключ нечем.
        assert await counterparty.lookup("1234567894", http=Broken(), path=db_path) is None
