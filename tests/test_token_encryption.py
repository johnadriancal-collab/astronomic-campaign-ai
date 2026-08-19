"""
Tests for app/services/token_encryption.py -- the ONLY thing standing
between a stored Google refresh token and plaintext. Uses monkeypatch to
set/unset settings.mailbox_token_encryption_key per test rather than real
environment variables, matching this suite's existing convention.
"""

import pytest

from app.services import token_encryption
from app.services.token_encryption import (
    TokenDecryptionError,
    TokenEncryptionNotConfiguredError,
    decrypt_refresh_token,
    encrypt_refresh_token,
)


def test_encrypt_then_decrypt_round_trips(monkeypatch):
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    monkeypatch.setattr(token_encryption.settings, "mailbox_token_encryption_key", key)

    ciphertext = encrypt_refresh_token("a-real-google-refresh-token")

    assert ciphertext != "a-real-google-refresh-token"
    assert decrypt_refresh_token(ciphertext) == "a-real-google-refresh-token"


def test_ciphertext_never_contains_the_plaintext_substring(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setattr(token_encryption.settings, "mailbox_token_encryption_key", Fernet.generate_key().decode())

    ciphertext = encrypt_refresh_token("1//0gSuperSecretRefreshTokenValue")

    assert "SuperSecretRefreshTokenValue" not in ciphertext


def test_encrypt_raises_when_key_not_configured(monkeypatch):
    monkeypatch.setattr(token_encryption.settings, "mailbox_token_encryption_key", None)

    with pytest.raises(TokenEncryptionNotConfiguredError):
        encrypt_refresh_token("some-token")


def test_decrypt_raises_when_key_not_configured(monkeypatch):
    monkeypatch.setattr(token_encryption.settings, "mailbox_token_encryption_key", None)

    with pytest.raises(TokenEncryptionNotConfiguredError):
        decrypt_refresh_token("anything")


def test_invalid_key_format_raises_not_configured_not_a_crash(monkeypatch):
    monkeypatch.setattr(token_encryption.settings, "mailbox_token_encryption_key", "not-a-valid-fernet-key")

    with pytest.raises(TokenEncryptionNotConfiguredError):
        encrypt_refresh_token("some-token")


def test_decrypt_with_wrong_key_raises_decryption_error_not_the_plaintext(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setattr(token_encryption.settings, "mailbox_token_encryption_key", Fernet.generate_key().decode())
    ciphertext = encrypt_refresh_token("a-token")

    monkeypatch.setattr(token_encryption.settings, "mailbox_token_encryption_key", Fernet.generate_key().decode())

    with pytest.raises(TokenDecryptionError):
        decrypt_refresh_token(ciphertext)
