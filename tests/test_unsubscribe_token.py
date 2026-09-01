"""
app/services/unsubscribe_token.py -- stateless Fernet-encrypted unsubscribe
tokens. Pure, no network, no store. Each test configures its own keys via
monkeypatch so tests never depend on ambient env state.
"""

import pytest
from cryptography.fernet import Fernet

from app.services.unsubscribe_token import (
    UNSUBSCRIBE_TOKEN_PURPOSE,
    UNSUBSCRIBE_TOKEN_VERSION,
    UnsubscribeTokenInvalidError,
    UnsubscribeTokenNotConfiguredError,
    decode_unsubscribe_token,
    generate_unsubscribe_token,
)


def _key() -> str:
    return Fernet.generate_key().decode()


# --- Not configured -----------------------------------------------------------


def test_generate_raises_not_configured_when_keys_unset(monkeypatch):
    monkeypatch.setattr("app.services.unsubscribe_token.settings.unsubscribe_token_encryption_keys", None)
    with pytest.raises(UnsubscribeTokenNotConfiguredError):
        generate_unsubscribe_token("a@example.com")


def test_decode_raises_not_configured_when_keys_unset_not_invalid(monkeypatch):
    """A config problem must surface distinctly -- never disguised as the
    generic public-facing 'invalid token' outcome."""
    monkeypatch.setattr("app.services.unsubscribe_token.settings.unsubscribe_token_encryption_keys", None)
    with pytest.raises(UnsubscribeTokenNotConfiguredError):
        decode_unsubscribe_token("anything")


def test_blank_keys_string_is_also_not_configured(monkeypatch):
    monkeypatch.setattr("app.services.unsubscribe_token.settings.unsubscribe_token_encryption_keys", "   ,  ,")
    with pytest.raises(UnsubscribeTokenNotConfiguredError):
        generate_unsubscribe_token("a@example.com")


def test_invalid_key_shape_is_not_configured(monkeypatch):
    monkeypatch.setattr("app.services.unsubscribe_token.settings.unsubscribe_token_encryption_keys", "not-a-fernet-key")
    with pytest.raises(UnsubscribeTokenNotConfiguredError):
        generate_unsubscribe_token("a@example.com")


# --- Round trip -----------------------------------------------------------------


def test_round_trip_returns_normalized_email(monkeypatch):
    monkeypatch.setattr("app.services.unsubscribe_token.settings.unsubscribe_token_encryption_keys", _key())
    token = generate_unsubscribe_token("  Someone@Example.COM ")
    assert decode_unsubscribe_token(token) == "someone@example.com"


def test_blank_email_is_rejected_before_encryption(monkeypatch):
    monkeypatch.setattr("app.services.unsubscribe_token.settings.unsubscribe_token_encryption_keys", _key())
    with pytest.raises(ValueError):
        generate_unsubscribe_token("   ")


def test_two_tokens_for_the_same_email_are_different_ciphertext(monkeypatch):
    """Each call mints a fresh nonce -- see the module docstring on why
    that's a versioning/identity seam, not a determinism guarantee."""
    monkeypatch.setattr("app.services.unsubscribe_token.settings.unsubscribe_token_encryption_keys", _key())
    t1 = generate_unsubscribe_token("a@example.com")
    t2 = generate_unsubscribe_token("a@example.com")
    assert t1 != t2
    assert decode_unsubscribe_token(t1) == decode_unsubscribe_token(t2) == "a@example.com"


# --- Opacity ----------------------------------------------------------------------


def test_token_contains_no_plaintext_email_substring(monkeypatch):
    """The whole point of encrypting rather than just encoding -- an
    opaque token must not leak the email by inspection."""
    monkeypatch.setattr("app.services.unsubscribe_token.settings.unsubscribe_token_encryption_keys", _key())
    token = generate_unsubscribe_token("victoria@astronomic.com")
    assert "victoria" not in token
    assert "astronomic.com" not in token
    assert "victoria@astronomic.com" not in token


# --- Tampering / wrong key --------------------------------------------------------


def test_tampered_token_is_rejected(monkeypatch):
    monkeypatch.setattr("app.services.unsubscribe_token.settings.unsubscribe_token_encryption_keys", _key())
    token = generate_unsubscribe_token("a@example.com")
    tampered = token[:-4] + ("A" if token[-4] != "A" else "B") + token[-3:]
    with pytest.raises(UnsubscribeTokenInvalidError):
        decode_unsubscribe_token(tampered)


def test_token_encrypted_with_a_different_key_is_rejected(monkeypatch):
    monkeypatch.setattr("app.services.unsubscribe_token.settings.unsubscribe_token_encryption_keys", _key())
    token = generate_unsubscribe_token("a@example.com")
    monkeypatch.setattr("app.services.unsubscribe_token.settings.unsubscribe_token_encryption_keys", _key())
    with pytest.raises(UnsubscribeTokenInvalidError):
        decode_unsubscribe_token(token)


def test_garbage_string_is_rejected(monkeypatch):
    monkeypatch.setattr("app.services.unsubscribe_token.settings.unsubscribe_token_encryption_keys", _key())
    with pytest.raises(UnsubscribeTokenInvalidError):
        decode_unsubscribe_token("not-a-real-token-at-all")


def test_empty_string_is_rejected(monkeypatch):
    monkeypatch.setattr("app.services.unsubscribe_token.settings.unsubscribe_token_encryption_keys", _key())
    with pytest.raises(UnsubscribeTokenInvalidError):
        decode_unsubscribe_token("")


def test_token_from_a_foreign_purpose_is_rejected(monkeypatch):
    """Same key, well-formed Fernet ciphertext, wrong `purpose`/`v` --
    proves the payload's own fields are actually checked, not just
    'did Fernet.decrypt succeed'."""
    import json

    from cryptography.fernet import Fernet as _F

    key = _key()
    monkeypatch.setattr("app.services.unsubscribe_token.settings.unsubscribe_token_encryption_keys", key)
    foreign_payload = json.dumps({"purpose": "something_else", "v": 1, "email": "a@example.com", "nonce": "x"})
    foreign_token = _F(key.encode()).encrypt(foreign_payload.encode()).decode()
    with pytest.raises(UnsubscribeTokenInvalidError):
        decode_unsubscribe_token(foreign_token)


def test_token_with_wrong_version_is_rejected(monkeypatch):
    import json

    from cryptography.fernet import Fernet as _F

    key = _key()
    monkeypatch.setattr("app.services.unsubscribe_token.settings.unsubscribe_token_encryption_keys", key)
    payload = json.dumps({"purpose": UNSUBSCRIBE_TOKEN_PURPOSE, "v": 999, "email": "a@example.com", "nonce": "x"})
    token = _F(key.encode()).encrypt(payload.encode()).decode()
    with pytest.raises(UnsubscribeTokenInvalidError):
        decode_unsubscribe_token(token)


def test_token_missing_email_field_is_rejected(monkeypatch):
    import json

    from cryptography.fernet import Fernet as _F

    key = _key()
    monkeypatch.setattr("app.services.unsubscribe_token.settings.unsubscribe_token_encryption_keys", key)
    payload = json.dumps({"purpose": UNSUBSCRIBE_TOKEN_PURPOSE, "v": UNSUBSCRIBE_TOKEN_VERSION, "nonce": "x"})
    token = _F(key.encode()).encrypt(payload.encode()).decode()
    with pytest.raises(UnsubscribeTokenInvalidError):
        decode_unsubscribe_token(token)


# --- MultiFernet rotation --------------------------------------------------------


def test_old_token_still_decrypts_after_a_new_key_is_added_first(monkeypatch):
    """The exact rotation scenario: encrypt with the OLD sole key, then
    reconfigure with NEW,OLD (newest first) -- the old token must still
    decrypt as long as its key remains present."""
    old_key = _key()
    monkeypatch.setattr("app.services.unsubscribe_token.settings.unsubscribe_token_encryption_keys", old_key)
    token = generate_unsubscribe_token("a@example.com")

    new_key = _key()
    monkeypatch.setattr(
        "app.services.unsubscribe_token.settings.unsubscribe_token_encryption_keys", f"{new_key},{old_key}"
    )
    assert decode_unsubscribe_token(token) == "a@example.com"


def test_new_tokens_are_encrypted_with_the_first_configured_key(monkeypatch):
    """After rotation, a FRESH token must no longer decrypt with only the
    retired key -- proving encryption really switched to the new one,
    not just that decryption is lenient."""
    old_key = _key()
    new_key = _key()
    monkeypatch.setattr(
        "app.services.unsubscribe_token.settings.unsubscribe_token_encryption_keys", f"{new_key},{old_key}"
    )
    token = generate_unsubscribe_token("a@example.com")

    monkeypatch.setattr("app.services.unsubscribe_token.settings.unsubscribe_token_encryption_keys", old_key)
    with pytest.raises(UnsubscribeTokenInvalidError):
        decode_unsubscribe_token(token)


def test_token_from_a_fully_retired_key_is_rejected(monkeypatch):
    """Once a key is dropped from the list entirely (not just
    reordered), tokens it issued must stop working -- this is the actual
    revocation mechanism for this stateless design."""
    retired_key = _key()
    monkeypatch.setattr("app.services.unsubscribe_token.settings.unsubscribe_token_encryption_keys", retired_key)
    token = generate_unsubscribe_token("a@example.com")

    monkeypatch.setattr("app.services.unsubscribe_token.settings.unsubscribe_token_encryption_keys", _key())
    with pytest.raises(UnsubscribeTokenInvalidError):
        decode_unsubscribe_token(token)
