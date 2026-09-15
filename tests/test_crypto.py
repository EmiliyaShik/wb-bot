"""Шифрование токенов клиентов на ключе из ENCRYPTION_KEY."""

import pytest

from core import crypto


def test_roundtrip_with_key(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", crypto.generate_key())
    secret = "eyJhbGciOiJFUzI1NiIsImtpZCI6InRlc3QifQ.payload.signature"
    blob = crypto.encrypt(secret)
    assert isinstance(blob, bytes)
    assert secret.encode() not in blob
    assert crypto.decrypt(blob) == secret


def test_two_encryptions_differ_but_decrypt_same(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", crypto.generate_key())
    a = crypto.encrypt("один и тот же текст")
    b = crypto.encrypt("один и тот же текст")
    assert a != b
    assert crypto.decrypt(a) == crypto.decrypt(b) == "один и тот же текст"


def test_missing_key_is_reported_not_crashed(monkeypatch):
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    assert crypto.key_available() is False
    with pytest.raises(crypto.MissingKeyError) as exc:
        crypto.encrypt("токен")
    assert "ENCRYPTION_KEY" in str(exc.value)
    # подсказка должна объяснять, как ключ сгенерировать
    assert "Fernet" in crypto.KEY_HINT


def test_broken_key_is_reported(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", "это-не-ключ")
    assert crypto.key_available() is False
    with pytest.raises(crypto.MissingKeyError):
        crypto.encrypt("токен")


def test_decrypt_with_other_key_fails(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", crypto.generate_key())
    blob = crypto.encrypt("токен")
    monkeypatch.setenv("ENCRYPTION_KEY", crypto.generate_key())
    with pytest.raises(crypto.DecryptError):
        crypto.decrypt(blob)
