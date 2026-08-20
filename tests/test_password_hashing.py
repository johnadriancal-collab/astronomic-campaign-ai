import pytest

from app.services.password_hashing import hash_password, verify_password


def test_hash_then_verify_round_trips():
    stored = hash_password("correct horse battery staple")

    assert verify_password("correct horse battery staple", stored) is True


def test_verify_rejects_wrong_password():
    stored = hash_password("the-real-password")

    assert verify_password("a-wrong-guess", stored) is False


def test_two_hashes_of_the_same_password_differ():
    """Different random salts -- proves the hash isn't a bare digest an
    attacker could precompute a rainbow table against."""
    first = hash_password("same-password")
    second = hash_password("same-password")

    assert first != second
    assert verify_password("same-password", first) is True
    assert verify_password("same-password", second) is True


def test_stored_hash_never_contains_the_plaintext_password():
    stored = hash_password("SuperSecretPassphrase123")

    assert "SuperSecretPassphrase123" not in stored


@pytest.mark.parametrize(
    "malformed",
    [
        "not-a-real-hash",
        "pbkdf2_sha256$notanumber$abcd$abcd",
        "pbkdf2_sha256$600000$nothex$abcd",
        "wrong_algorithm$600000$abcd$abcd",
        "",
        "pbkdf2_sha256$600000$abcd",  # missing a segment
    ],
)
def test_verify_never_crashes_on_a_malformed_stored_hash(malformed):
    assert verify_password("anything", malformed) is False


def test_hash_format_is_self_describing():
    stored = hash_password("x", iterations=1000)
    algorithm, iterations, salt_hex, hash_hex = stored.split("$")

    assert algorithm == "pbkdf2_sha256"
    assert iterations == "1000"
    assert len(bytes.fromhex(salt_hex)) == 16
    assert len(bytes.fromhex(hash_hex)) == 32  # SHA-256 digest size
