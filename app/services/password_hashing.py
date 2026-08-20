r"""
PBKDF2-HMAC-SHA256 password hashing -- standard library only (`hashlib`),
deliberately not a new dependency (`bcrypt`/`passlib`). PBKDF2 with a high
iteration count is an OWASP-approved algorithm for this exact use case
(a single, rarely-rotated internal shared password, not a large user base
needing bcrypt's memory-hardness) and needs nothing beyond what this app
already ships with.

The stored format is self-describing: `pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>`.
This is what AUTH_PASSWORD_HASH holds in production -- generate one with:

    python3 -c "
import hashlib, os, getpass
password = getpass.getpass('Password: ')
salt = os.urandom(16)
iterations = 600_000
derived = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, iterations)
print(f'pbkdf2_sha256\${iterations}\${salt.hex()}\${derived.hex()}')
"

-- stdlib only, no `cryptography`/`bcrypt` package required to run it.
"""

import hashlib
import hmac
import os

_ALGORITHM = "pbkdf2_sha256"
_DEFAULT_ITERATIONS = 600_000


def hash_password(password: str, *, iterations: int = _DEFAULT_ITERATIONS) -> str:
    salt = os.urandom(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{_ALGORITHM}${iterations}${salt.hex()}${derived.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Never raises on a malformed stored_hash -- returns False instead,
    so a misconfigured AUTH_PASSWORD_HASH fails closed (login rejected)
    rather than crashing the request."""
    try:
        algorithm, iterations_str, salt_hex, expected_hash_hex = stored_hash.split("$")
        if algorithm != _ALGORITHM:
            return False
        iterations = int(iterations_str)
        salt = bytes.fromhex(salt_hex)
    except (ValueError, AttributeError):
        return False

    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(derived.hex(), expected_hash_hex)
