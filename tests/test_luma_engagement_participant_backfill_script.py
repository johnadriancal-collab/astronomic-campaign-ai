"""
End-to-end tests for scripts/run_luma_engagement_participant_backfill.py --
the CLI safety guard (refuse in write mode unless BOTH --write and
--confirm-production-writes are given, --engagement-id always required)
and the real SQLite wiring, exercised as an actual subprocess against a
throwaway temp database (never the real app database). Same convention as
test_luma_contact_enrichment_backfill_script.py.
"""

import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.models.client_crm import Engagement, EngagementType
from app.models.crm import CrmContact
from app.models.luma import LumaApprovalStatus, LumaMatchStatus, LumaRegistration
from app.repositories.sqlite_crm_contact_store import SQLiteCrmContactStore
from app.repositories.sqlite_engagement_participant_store import SQLiteEngagementParticipantStore
from app.repositories.sqlite_engagement_store import SQLiteEngagementStore
from app.repositories.sqlite_luma_registration_store import SQLiteLumaRegistrationStore

pytestmark = pytest.mark.asyncio

REPO_ROOT = Path(__file__).resolve().parent.parent


async def _seed_db(db_path: str, *, luma_event_id: str = "evt-1", linked: bool = True) -> tuple[str, str]:
    """Seeds one Engagement (linked to luma_event_id unless linked=False),
    one Client-less Engagement row (Client isn't read by this backfill at
    all), one CrmContact, and one matched LumaRegistration for that event.
    Returns (engagement_id, crm_contact_id)."""
    engagement_store = SQLiteEngagementStore(db_path)
    contact_store = SQLiteCrmContactStore(db_path)
    registration_store = SQLiteLumaRegistrationStore(db_path)
    await engagement_store.connect()
    await contact_store.connect()
    await registration_store.connect()
    now = datetime.now(timezone.utc)

    engagement_id = str(uuid.uuid4())
    engagement = Engagement(
        engagement_id=engagement_id,
        client_id="client-1",
        title="Test Dinner",
        engagement_type=EngagementType.DINNER,
        luma_event_id=luma_event_id if linked else None,
        created_at=now,
        updated_at=now,
    )
    await engagement_store.create(engagement)

    contact = CrmContact(crm_contact_id=str(uuid.uuid4()), first_name="Jane", last_name="Doe", created_at=now, updated_at=now)
    await contact_store.create(contact)

    await registration_store.save(
        LumaRegistration(
            luma_guest_id=str(uuid.uuid4()),
            luma_event_id=luma_event_id,
            crm_contact_id=contact.crm_contact_id,
            match_status=LumaMatchStatus.MATCHED,
            approval_status=LumaApprovalStatus.APPROVED,
            registered_at=now,
            synced_at=now,
            updated_at=now,
        )
    )

    await engagement_store.close()
    await contact_store.close()
    await registration_store.close()
    return engagement_id, contact.crm_contact_id


def _run_script(db_path: str, *extra_args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "scripts.run_luma_engagement_participant_backfill", "--database-path", db_path, *extra_args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )


async def _participant_count(db_path: str, engagement_id: str) -> int:
    store = SQLiteEngagementParticipantStore(db_path)
    await store.connect()
    participants = await store.list_for_engagement(engagement_id)
    await store.close()
    return len(participants)


async def test_engagement_id_is_required(tmp_path):
    db_path = str(tmp_path / "test.db")
    await _seed_db(db_path)

    result = subprocess.run(
        [sys.executable, "-m", "scripts.run_luma_engagement_participant_backfill", "--database-path", db_path],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 2
    assert "engagement-id" in result.stderr.lower() or "required" in result.stderr.lower()


async def test_default_invocation_is_a_dry_run_and_makes_zero_writes(tmp_path):
    db_path = str(tmp_path / "test.db")
    engagement_id, _contact_id = await _seed_db(db_path)

    result = _run_script(db_path, "--engagement-id", engagement_id)

    assert result.returncode == 0
    assert "DRY RUN" in result.stdout
    assert "created                                    1" in result.stdout
    assert await _participant_count(db_path, engagement_id) == 0  # nothing actually written


async def test_write_without_confirmation_refuses_and_touches_nothing(tmp_path):
    db_path = str(tmp_path / "test.db")
    engagement_id, _contact_id = await _seed_db(db_path)

    result = _run_script(db_path, "--engagement-id", engagement_id, "--write")

    assert result.returncode == 2
    assert "Refusing to run" in result.stderr
    assert await _participant_count(db_path, engagement_id) == 0


async def test_confirmation_without_write_flag_also_refuses(tmp_path):
    db_path = str(tmp_path / "test.db")
    engagement_id, _contact_id = await _seed_db(db_path)

    result = _run_script(db_path, "--engagement-id", engagement_id, "--confirm-production-writes")

    assert result.returncode == 2
    assert "Refusing to run" in result.stderr


async def test_both_flags_together_actually_writes(tmp_path):
    db_path = str(tmp_path / "test.db")
    engagement_id, _contact_id = await _seed_db(db_path)

    result = _run_script(db_path, "--engagement-id", engagement_id, "--write", "--confirm-production-writes")

    assert result.returncode == 0
    assert "WRITE MODE" in result.stdout
    assert await _participant_count(db_path, engagement_id) == 1


async def test_write_mode_is_idempotent_on_rerun(tmp_path):
    db_path = str(tmp_path / "test.db")
    engagement_id, _contact_id = await _seed_db(db_path)

    first = _run_script(db_path, "--engagement-id", engagement_id, "--write", "--confirm-production-writes")
    assert first.returncode == 0
    second = _run_script(db_path, "--engagement-id", engagement_id, "--write", "--confirm-production-writes")
    assert second.returncode == 0

    assert "created                                    0" in second.stdout
    assert await _participant_count(db_path, engagement_id) == 1  # still exactly one, not duplicated


async def test_nonexistent_engagement_is_rejected(tmp_path):
    db_path = str(tmp_path / "test.db")
    await _seed_db(db_path)

    result = _run_script(db_path, "--engagement-id", "does-not-exist")

    assert result.returncode == 2
    assert "Refusing to run" in result.stderr


async def test_unlinked_engagement_is_rejected(tmp_path):
    db_path = str(tmp_path / "test.db")
    engagement_id, _contact_id = await _seed_db(db_path, linked=False)

    result = _run_script(db_path, "--engagement-id", engagement_id)

    assert result.returncode == 2
    assert "Refusing to run" in result.stderr
    assert "luma_event_id is null" in result.stderr
