"""
Stateless, versioned, opaque unsubscribe tokens -- Astronomic Mail Phase B3.

Fernet (via MultiFernet for rotation) is used rather than a bare HMAC
signature, deliberately: a signature over a plaintext payload only proves
the payload wasn't tampered with, it does not HIDE the payload -- and the
whole point here is that the token must never expose which email it's
for just by looking at it (contrast app/luma/webhook_signature.py's HMAC
scheme, which authenticates a request FROM a party we already trust with
the plaintext body; this is the opposite direction -- WE are both issuer
and verifier of an opaque grant handed to an anonymous recipient).
`cryptography` is already a dependency (see app/services/token_encryption.py,
the OAuth-refresh-token precedent this module deliberately does NOT share
a key with -- different trust domain, different rotation cadence).

STATELESS: no database row is ever created or checked for a token itself.
The `nonce` field exists purely as an identity/versioning seam for later
(attribution, revocation, audit) -- nothing in B3 persists it or reads it
back. This is a deliberate, documented choice: it means a token can never
be individually revoked in B3 (only globally, by rotating the encryption
key -- see PUBLIC_PATHS'/config's own docstrings), which is an acceptable
tradeoff for the smallest robust design the B3 investigation recommended,
not an oversight.

The payload is a small, versioned, structured JSON object -- NOT a bare
"unsub:v1:<email>" string -- specifically so a future schema change (a new
required field, a new purpose sharing this same encryption key for a
different link type) has a real place to branch on `purpose`/`v` rather
than guessing at an undelimited string's shape.

Deliberately contains NO crm_contact_id, campaign_id, or enrollment_id --
see this module's own docstring history (the B3 investigation) for why:
the actual unsubscribe action only ever needs the normalized email, since
suppression in this codebase is global by email, not scoped to a
campaign/list/category (see MailSuppression's own docstring in
app/models/mail.py).
"""

import json
import secrets

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.config import settings
from app.models.crm import normalize_email

UNSUBSCRIBE_TOKEN_PURPOSE = "mail_unsubscribe"
UNSUBSCRIBE_TOKEN_VERSION = 1


class UnsubscribeTokenNotConfiguredError(Exception):
    """UNSUBSCRIBE_TOKEN_ENCRYPTION_KEYS is unset, or contains no valid
    Fernet key -- callers should surface this as a clear 503 (a
    configuration problem, safe to say so plainly), never confuse it with
    UnsubscribeTokenInvalidError below (a public-facing, deliberately
    generic outcome)."""


class UnsubscribeTokenInvalidError(Exception):
    """The token failed to decrypt, or decrypted to something that isn't
    a well-formed unsubscribe-token payload (wrong purpose, wrong
    version, missing/blank email) -- deliberately ONE exception type
    covering every one of those distinct failure modes. A public route
    must never be able to tell a caller WHICH way a token was invalid
    (wrong key vs. tampered vs. wrong purpose vs. malformed JSON) --
    that distinction is exactly the kind of oracle an attacker could use
    to iterate toward a valid-looking token, the same reasoning
    app/luma/webhook_signature.py's module docstring already applies to
    its own single collapsed exception."""


def _multi_fernet() -> MultiFernet:
    raw = settings.unsubscribe_token_encryption_keys
    if not raw:
        raise UnsubscribeTokenNotConfiguredError(
            "UNSUBSCRIBE_TOKEN_ENCRYPTION_KEYS is not configured -- unsubscribe token "
            "generation/verification is unavailable."
        )
    key_strings = [k.strip() for k in raw.split(",") if k.strip()]
    if not key_strings:
        raise UnsubscribeTokenNotConfiguredError(
            "UNSUBSCRIBE_TOKEN_ENCRYPTION_KEYS is set but contains no usable key."
        )
    try:
        return MultiFernet([Fernet(k.encode()) for k in key_strings])
    except (ValueError, TypeError) as e:
        raise UnsubscribeTokenNotConfiguredError(
            "UNSUBSCRIBE_TOKEN_ENCRYPTION_KEYS is set but contains an invalid Fernet key."
        ) from e


def generate_unsubscribe_token(email: str) -> str:
    """Encrypts with the FIRST configured key (see settings.
    unsubscribe_token_encryption_keys' docstring: newest key first).
    Raises UnsubscribeTokenNotConfiguredError if unconfigured, or
    ValueError if `email` doesn't normalize to anything usable -- never
    silently generates a token for a blank/unusable address."""
    normalized = normalize_email(email)
    if not normalized:
        raise ValueError("email must be a non-blank, usable address to generate an unsubscribe token.")

    payload = {
        "purpose": UNSUBSCRIBE_TOKEN_PURPOSE,
        "v": UNSUBSCRIBE_TOKEN_VERSION,
        "email": normalized,
        # Identity/versioning seam only -- see this module's docstring.
        # Never persisted, never read back by anything in B3.
        "nonce": secrets.token_urlsafe(16),
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return _multi_fernet().encrypt(raw).decode("ascii")


def decode_unsubscribe_token(token: str) -> str:
    """Returns the normalized email a valid token was issued for. Raises
    UnsubscribeTokenNotConfiguredError (propagated, NOT caught by the
    generic path below -- a config problem must surface distinctly, as a
    503, never disguised as a public "invalid token" response) or
    UnsubscribeTokenInvalidError for every other failure (see that
    class's own docstring for why they're deliberately collapsed into
    one). No TTL is enforced (Fernet's `ttl=None`) -- see settings.
    unsubscribe_token_encryption_keys' docstring: an old campaign email's
    link must keep working indefinitely; a leaked/compromised key is
    mitigated by rotation, not token expiry."""
    multi_fernet = _multi_fernet()

    try:
        raw = multi_fernet.decrypt(token.encode("ascii"), ttl=None)
        payload = json.loads(raw)
    except (InvalidToken, ValueError, TypeError, UnicodeDecodeError):
        raise UnsubscribeTokenInvalidError("Invalid or tampered unsubscribe token.")

    if not isinstance(payload, dict):
        raise UnsubscribeTokenInvalidError("Invalid unsubscribe token payload.")
    if payload.get("purpose") != UNSUBSCRIBE_TOKEN_PURPOSE or payload.get("v") != UNSUBSCRIBE_TOKEN_VERSION:
        raise UnsubscribeTokenInvalidError("Invalid unsubscribe token payload.")

    email = payload.get("email")
    if not isinstance(email, str):
        raise UnsubscribeTokenInvalidError("Invalid unsubscribe token payload.")
    normalized = normalize_email(email)
    if not normalized:
        raise UnsubscribeTokenInvalidError("Invalid unsubscribe token payload.")
    return normalized
