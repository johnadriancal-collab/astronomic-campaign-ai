import pytest

from app.repositories.auth_session_store import MemoryAuthSessionStore
from app.services import auth_service as auth_service_module
from app.services.auth_service import AuthNotConfiguredError, AuthService
from app.services.password_hashing import hash_password

pytestmark = pytest.mark.asyncio

REAL_PASSWORD = "correct horse battery staple"


@pytest.fixture(autouse=True)
def configured_credentials(monkeypatch):
    monkeypatch.setattr(auth_service_module.settings, "auth_email", "team@astronomic.com")
    monkeypatch.setattr(auth_service_module.settings, "auth_password_hash", hash_password(REAL_PASSWORD))


@pytest.fixture
def service():
    return AuthService(session_store=MemoryAuthSessionStore())


# --- verify_credentials -------------------------------------------------


async def test_verify_credentials_accepts_the_correct_email_and_password(service):
    assert service.verify_credentials("team@astronomic.com", REAL_PASSWORD) is True


async def test_verify_credentials_is_case_insensitive_on_email(service):
    assert service.verify_credentials("Team@Astronomic.COM", REAL_PASSWORD) is True


async def test_verify_credentials_rejects_wrong_password(service):
    assert service.verify_credentials("team@astronomic.com", "wrong-password") is False


async def test_verify_credentials_rejects_wrong_email(service):
    assert service.verify_credentials("someone-else@astronomic.com", REAL_PASSWORD) is False


async def test_verify_credentials_raises_when_not_configured(service, monkeypatch):
    monkeypatch.setattr(auth_service_module.settings, "auth_email", None)

    with pytest.raises(AuthNotConfiguredError):
        service.verify_credentials("team@astronomic.com", REAL_PASSWORD)


async def test_verify_credentials_raises_when_only_password_hash_missing(service, monkeypatch):
    monkeypatch.setattr(auth_service_module.settings, "auth_password_hash", None)

    with pytest.raises(AuthNotConfiguredError):
        service.verify_credentials("team@astronomic.com", REAL_PASSWORD)


# --- sessions ------------------------------------------------------------


async def test_create_session_then_validate_succeeds(service):
    raw_token, expires_at = await service.create_session()

    assert await service.validate_session(raw_token) is True
    assert expires_at is not None


async def test_validate_session_rejects_garbage_token(service):
    assert await service.validate_session("this-was-never-issued") is False


async def test_validate_session_rejects_missing_token(service):
    assert await service.validate_session(None) is False
    assert await service.validate_session("") is False


async def test_two_sessions_get_different_tokens(service):
    token1, _ = await service.create_session()
    token2, _ = await service.create_session()

    assert token1 != token2
    assert await service.validate_session(token1) is True
    assert await service.validate_session(token2) is True


async def test_expired_session_is_rejected(service, monkeypatch):
    from datetime import datetime, timedelta, timezone

    raw_token, _ = await service.create_session()

    # Simulate the session having expired 1 second ago.
    session_hash = auth_service_module._hash_token(raw_token)
    stored = await service.session_store.get(session_hash)
    expired = stored.model_copy(update={"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)})
    await service.session_store.create(expired)

    assert await service.validate_session(raw_token) is False


async def test_expired_session_is_cleaned_up_on_validation(service):
    from datetime import datetime, timedelta, timezone

    raw_token, _ = await service.create_session()
    session_hash = auth_service_module._hash_token(raw_token)
    stored = await service.session_store.get(session_hash)
    expired = stored.model_copy(update={"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)})
    await service.session_store.create(expired)

    await service.validate_session(raw_token)

    assert await service.session_store.get(session_hash) is None


# --- logout ----------------------------------------------------------------


async def test_invalidate_session_logs_out(service):
    raw_token, _ = await service.create_session()
    assert await service.validate_session(raw_token) is True

    await service.invalidate_session(raw_token)

    assert await service.validate_session(raw_token) is False


async def test_invalidate_session_with_no_token_is_a_safe_noop(service):
    await service.invalidate_session(None)  # must not raise


async def test_invalidate_session_with_unknown_token_is_a_safe_noop(service):
    await service.invalidate_session("never-issued")  # must not raise


async def test_logging_out_one_session_does_not_affect_another(service):
    token1, _ = await service.create_session()
    token2, _ = await service.create_session()

    await service.invalidate_session(token1)

    assert await service.validate_session(token1) is False
    assert await service.validate_session(token2) is True
