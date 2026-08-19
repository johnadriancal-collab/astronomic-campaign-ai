"""
Fernet-based encryption at rest for Google OAuth refresh tokens -- see
app/models/mailbox.py's MailboxCredential docstring for why this is a
wholly separate, internal-only path from the public Mailbox model.

Fernet (symmetric, authenticated encryption) is used rather than anything
custom: a 32-byte urlsafe-base64 key, generated once and set as
MAILBOX_TOKEN_ENCRYPTION_KEY, is the ONLY thing that can ever decrypt a
stored refresh token. Losing this key means every connected mailbox must be
reconnected -- there is no recovery path, by design (the alternative,
deriving the key from something else recoverable, would weaken the
guarantee that only this one secret controls decryption).
"""

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


class TokenEncryptionNotConfiguredError(Exception):
    """MAILBOX_TOKEN_ENCRYPTION_KEY is unset, or set to something that
    isn't a valid Fernet key -- callers should surface this as a 503,
    exactly like itf_webhook_token's precedent in app/dependencies.py,
    never as an unhandled 500."""


class TokenDecryptionError(Exception):
    """A stored ciphertext couldn't be decrypted with the CURRENT key
    (e.g. the key was rotated/lost) -- distinct from
    TokenEncryptionNotConfiguredError so callers can tell "not configured"
    apart from "configured, but this specific value doesn't decrypt"."""


def _fernet() -> Fernet:
    if not settings.mailbox_token_encryption_key:
        raise TokenEncryptionNotConfiguredError(
            "MAILBOX_TOKEN_ENCRYPTION_KEY is not configured -- Google mailbox connection is unavailable."
        )
    try:
        return Fernet(settings.mailbox_token_encryption_key.encode())
    except (ValueError, TypeError) as e:
        raise TokenEncryptionNotConfiguredError(
            "MAILBOX_TOKEN_ENCRYPTION_KEY is set but is not a valid Fernet key."
        ) from e


def encrypt_refresh_token(raw_refresh_token: str) -> str:
    return _fernet().encrypt(raw_refresh_token.encode()).decode()


def decrypt_refresh_token(encrypted_refresh_token: str) -> str:
    try:
        return _fernet().decrypt(encrypted_refresh_token.encode()).decode()
    except InvalidToken as e:
        raise TokenDecryptionError("Stored refresh token could not be decrypted with the current key.") from e
