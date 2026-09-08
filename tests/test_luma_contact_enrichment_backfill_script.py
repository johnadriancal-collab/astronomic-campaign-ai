"""
End-to-end tests for scripts/run_luma_contact_enrichment_backfill.py --
the CLI safety guard (refuse in write mode unless BOTH --write and
--confirm-production-writes are given) and the real SQLite wiring,
exercised as an actual subprocess against a throwaway temp database (never
the real app database).
"""

import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.models.crm import CrmContact
from app.models.luma import LumaApprovalStatus, LumaMatchStatus, LumaRegistration, LumaRegistrationAnswer
from app.repositories.sqlite_crm_contact_store import SQLiteCrmContactStore
from app.repositories.sqlite_luma_registration_store import SQLiteLumaRegistrationStore

pytestmark = pytest.mark.asyncio

REPO_ROOT = Path(__file__).resolve().parent.parent


async def _seed_db(db_path: str) -> str:
    contact_store = SQLiteCrmContactStore(db_path)
    registration_store = SQLiteLumaRegistrationStore(db_path)
    await contact_store.connect()
    await registration_store.connect()
    now = datetime.now(timezone.utc)
    contact = CrmContact(crm_contact_id=str(uuid.uuid4()), created_at=now, updated_at=now, company="OldCo", company_website="oldco.com")
    await contact_store.create(contact)
    await registration_store.save(
        LumaRegistration(
            luma_guest_id=str(uuid.uuid4()),
            luma_event_id="e1",
            crm_contact_id=contact.crm_contact_id,
            match_status=LumaMatchStatus.MATCHED,
            approval_status=LumaApprovalStatus.APPROVED,
            registered_at=now,
            synced_at=now,
            updated_at=now,
            registration_answers=[
                LumaRegistrationAnswer(question_id="q1", label="Company", question_type="company", value={"company": "NewCo", "job_title": "CEO"})
            ],
        )
    )
    await contact_store.close()
    await registration_store.close()
    return contact.crm_contact_id


def _run_script(db_path: str, *extra_args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "scripts.run_luma_contact_enrichment_backfill", "--database-path", db_path, *extra_args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )


async def test_default_invocation_is_a_dry_run_and_makes_zero_writes(tmp_path):
    db_path = str(tmp_path / "test.db")
    contact_id = await _seed_db(db_path)

    result = _run_script(db_path)

    assert result.returncode == 0
    assert "DRY RUN" in result.stdout
    contact_store = SQLiteCrmContactStore(db_path)
    await contact_store.connect()
    persisted = await contact_store.get(contact_id)
    await contact_store.close()
    assert persisted.company == "OldCo"  # untouched


async def test_write_without_confirmation_refuses_and_touches_nothing(tmp_path):
    db_path = str(tmp_path / "test.db")
    contact_id = await _seed_db(db_path)

    result = _run_script(db_path, "--write")

    assert result.returncode == 2
    assert "Refusing to run" in result.stderr
    contact_store = SQLiteCrmContactStore(db_path)
    await contact_store.connect()
    persisted = await contact_store.get(contact_id)
    await contact_store.close()
    assert persisted.company == "OldCo"


async def test_confirmation_without_write_flag_also_refuses(tmp_path):
    db_path = str(tmp_path / "test.db")
    await _seed_db(db_path)

    result = _run_script(db_path, "--confirm-production-writes")

    assert result.returncode == 2
    assert "Refusing to run" in result.stderr


async def test_both_flags_together_actually_writes(tmp_path):
    db_path = str(tmp_path / "test.db")
    contact_id = await _seed_db(db_path)

    result = _run_script(db_path, "--write", "--confirm-production-writes")

    assert result.returncode == 0
    assert "WRITE MODE" in result.stdout
    contact_store = SQLiteCrmContactStore(db_path)
    await contact_store.connect()
    persisted = await contact_store.get(contact_id)
    await contact_store.close()
    assert persisted.company == "NewCo"


async def test_write_mode_is_idempotent_on_rerun(tmp_path):
    db_path = str(tmp_path / "test.db")
    await _seed_db(db_path)

    first = _run_script(db_path, "--write", "--confirm-production-writes")
    assert first.returncode == 0
    second = _run_script(db_path, "--write", "--confirm-production-writes")
    assert second.returncode == 0

    assert "contacts_saved                             0" in second.stdout


# --- --exclude: prefix resolution, fail-closed --------------------------------


async def _seed_two_contacts_sharing_a_prefix(db_path: str) -> tuple[str, str]:
    """Two Contact IDs sharing the literal prefix "shared-" -- used to
    exercise the fail-closed multi-match case."""
    contact_store = SQLiteCrmContactStore(db_path)
    registration_store = SQLiteLumaRegistrationStore(db_path)
    await contact_store.connect()
    await registration_store.connect()
    now = datetime.now(timezone.utc)
    c1 = CrmContact(crm_contact_id="shared-aaaa-0000-0000-000000000001", created_at=now, updated_at=now, company="Co1")
    c2 = CrmContact(crm_contact_id="shared-bbbb-0000-0000-000000000002", created_at=now, updated_at=now, company="Co2")
    await contact_store.create(c1)
    await contact_store.create(c2)
    for c, new_val in ((c1, "New1"), (c2, "New2")):
        await registration_store.save(
            LumaRegistration(
                luma_guest_id=str(uuid.uuid4()),
                luma_event_id="e1",
                crm_contact_id=c.crm_contact_id,
                match_status=LumaMatchStatus.MATCHED,
                approval_status=LumaApprovalStatus.APPROVED,
                registered_at=now,
                synced_at=now,
                updated_at=now,
                registration_answers=[LumaRegistrationAnswer(question_id="q1", label="Company", question_type="company", value={"company": new_val})],
            )
        )
    await contact_store.close()
    await registration_store.close()
    return c1.crm_contact_id, c2.crm_contact_id


async def test_exclude_with_a_unique_prefix_skips_only_that_contact(tmp_path):
    db_path = str(tmp_path / "test.db")
    contact_id = await _seed_db(db_path)

    result = _run_script(db_path, "--write", "--confirm-production-writes", "--exclude", contact_id[:8])

    assert result.returncode == 0
    assert "contacts_excluded                          1" in result.stdout
    assert contact_id in result.stdout  # listed under Excluded Contacts
    contact_store = SQLiteCrmContactStore(db_path)
    await contact_store.connect()
    persisted = await contact_store.get(contact_id)
    await contact_store.close()
    assert persisted.company == "OldCo"  # untouched by the write run


async def test_exclude_with_a_zero_match_prefix_fails_closed(tmp_path):
    db_path = str(tmp_path / "test.db")
    contact_id = await _seed_db(db_path)

    result = _run_script(db_path, "--write", "--confirm-production-writes", "--exclude", "zzzzzzzz")

    assert result.returncode == 2
    assert "Refusing to run" in result.stderr
    assert "matched ZERO Contacts" in result.stderr
    contact_store = SQLiteCrmContactStore(db_path)
    await contact_store.connect()
    persisted = await contact_store.get(contact_id)
    await contact_store.close()
    assert persisted.company == "OldCo"  # nothing was written -- refused before running at all


async def test_exclude_with_an_ambiguous_prefix_fails_closed(tmp_path):
    db_path = str(tmp_path / "test.db")
    c1_id, c2_id = await _seed_two_contacts_sharing_a_prefix(db_path)

    result = _run_script(db_path, "--exclude", "shared-")

    assert result.returncode == 2
    assert "Refusing to run" in result.stderr
    assert "matched MULTIPLE Contacts" in result.stderr
    contact_store = SQLiteCrmContactStore(db_path)
    await contact_store.connect()
    c1 = await contact_store.get(c1_id)
    c2 = await contact_store.get(c2_id)
    await contact_store.close()
    assert c1.company == "Co1"  # nothing touched -- refused before reading further
    assert c2.company == "Co2"


async def test_exclude_accepts_comma_separated_and_repeated_flags(tmp_path):
    db_path = str(tmp_path / "test.db")
    c1_id, c2_id = await _seed_two_contacts_sharing_a_prefix(db_path)

    # Unique-enough prefixes for each (their own distinguishing suffix).
    result = _run_script(db_path, "--exclude", f"{c1_id[:11]},{c2_id[:11]}")

    assert result.returncode == 0
    assert "contacts_excluded                          2" in result.stdout
