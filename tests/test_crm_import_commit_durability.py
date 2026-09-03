"""
CrmImportService.commit() durability/idempotency/resumability (Stage 4A,
2026-09-03). Kept separate from test_crm_import_service.py (which owns
matching/mapping/classification POLICY coverage, untouched by this stage)
-- this file is purely about the NEW guarantee: commit() is safe to call
any number of times against the same batch, and a crash partway through a
large commit never re-creates/re-updates a row whose outcome already
landed durably.
"""

import pytest

from app.models.crm import CrmImportBatchStatus, CrmImportRowStatus
from app.repositories.crm_import_batch_store import MemoryCrmImportBatchStore
from app.repositories.sqlite_crm_import_batch_store import SQLiteCrmImportBatchStore
from app.services.crm_import_service import CrmImportService
from app.services.crm_service import CrmService

pytestmark = pytest.mark.asyncio


@pytest.fixture
def import_service():
    crm = CrmService()
    return CrmImportService(crm_service=crm, batch_store=MemoryCrmImportBatchStore())


def csv_bytes(text: str) -> bytes:
    return text.encode("utf-8")


async def _upload_and_preview(import_service, csv_text: str, mapping: dict[str, str]):
    batch = await import_service.upload("p.csv", csv_bytes(csv_text))
    return await import_service.preview(batch.import_batch_id, mapping)


# --- Requirement 1: per-row resolution persisted -----------------------


async def test_commit_create_outcome_is_persisted_with_resolved_contact_id(import_service):
    batch = await _upload_and_preview(import_service, "Email\nnew@example.com\n", {"Email": "email"})
    await import_service.commit(batch.import_batch_id)

    final = await import_service.get_batch(batch.import_batch_id)
    row = final.preview[0]
    assert row.commit_outcome == "created"
    assert row.resolved_contact_id is not None
    contact = await import_service.crm_service.get_contact(row.resolved_contact_id)
    assert contact.email == "new@example.com"


async def test_commit_update_outcome_is_persisted_with_resolved_contact_id(import_service):
    existing = await import_service.crm_service.create_contact({"email": "known@example.com"})
    batch = await _upload_and_preview(import_service, "Email,Company\nknown@example.com,Acme\n", {"Email": "email", "Company": "company"})
    await import_service.commit(batch.import_batch_id)

    final = await import_service.get_batch(batch.import_batch_id)
    row = final.preview[0]
    assert row.commit_outcome == "updated"
    assert row.resolved_contact_id == existing.crm_contact_id


async def test_commit_skip_decision_persists_skipped_outcome_with_no_contact_id(import_service):
    batch = await _upload_and_preview(import_service, "Email\nnew@example.com\n", {"Email": "email"})
    await import_service.commit(batch.import_batch_id, decisions={0: "skip"})

    final = await import_service.get_batch(batch.import_batch_id)
    row = final.preview[0]
    assert row.commit_outcome == "skipped"
    assert row.resolved_contact_id is None


async def test_commit_default_skip_of_unreviewed_possible_duplicate_persists_skipped_outcome(import_service):
    await import_service.crm_service.create_contact({"first_name": "Ada", "last_name": "Lovelace", "company": "Acme"})
    batch = await _upload_and_preview(
        import_service, "First Name,Last Name,Company\nAda,Lovelace,Acme\n",
        {"First Name": "first_name", "Last Name": "last_name", "Company": "company"},
    )
    await import_service.commit(batch.import_batch_id)

    final = await import_service.get_batch(batch.import_batch_id)
    row = final.preview[0]
    assert row.commit_outcome == "skipped"
    assert row.resolved_contact_id is None


async def test_commit_preview_time_error_row_persists_skipped_outcome_matching_pre_existing_aggregate(import_service):
    """Preserves the pre-Stage-4A quirk exactly: a row that failed
    CLASSIFICATION at preview time counts toward the `skipped` bucket in
    the final report, not `errors` -- confirmed durably per-row too now."""
    batch = await import_service.upload("p.csv", csv_bytes("Email\nnew@example.com\n"))
    previewed = await import_service.preview(batch.import_batch_id, {"Email": "email"})
    previewed.preview[0] = previewed.preview[0].model_copy(
        update={"status": CrmImportRowStatus.ERROR, "error": "simulated preview-time failure"}
    )
    await import_service.batch_store.save(previewed)

    report = await import_service.commit(batch.import_batch_id)
    assert report.skipped == 1
    assert report.errors == 0

    final = await import_service.get_batch(batch.import_batch_id)
    assert final.preview[0].commit_outcome == "skipped"
    assert final.preview[0].resolved_contact_id is None


async def test_commit_human_override_of_possible_duplicate_to_create_persists_created_outcome(import_service):
    await import_service.crm_service.create_contact({"first_name": "Ada", "last_name": "Lovelace", "company": "Acme"})
    batch = await _upload_and_preview(
        import_service, "First Name,Last Name,Company\nAda,Lovelace,Acme\n",
        {"First Name": "first_name", "Last Name": "last_name", "Company": "company"},
    )
    await import_service.commit(batch.import_batch_id, decisions={0: "create"})

    final = await import_service.get_batch(batch.import_batch_id)
    row = final.preview[0]
    assert row.commit_outcome == "created"
    assert row.resolved_contact_id is not None
    assert (await import_service.crm_service.list_contacts()).total == 2


async def test_commit_human_override_of_possible_duplicate_to_update_persists_updated_outcome(import_service):
    existing = await import_service.crm_service.create_contact(
        {"first_name": "Ada", "last_name": "Lovelace", "company": "Acme"}
    )
    batch = await _upload_and_preview(
        import_service, "First Name,Last Name,Company\nAda,Lovelace,Acme\n",
        {"First Name": "first_name", "Last Name": "last_name", "Company": "company"},
    )
    await import_service.commit(batch.import_batch_id, decisions={0: "update"})

    final = await import_service.get_batch(batch.import_batch_id)
    row = final.preview[0]
    assert row.commit_outcome == "updated"
    assert row.resolved_contact_id == existing.crm_contact_id


async def test_duplicate_csv_rows_resolving_to_one_contact_share_the_same_resolved_contact_id(import_service):
    batch = await _upload_and_preview(
        import_service, "Email,Company\nsame@example.com,\nsame@example.com,New Co\n", {"Email": "email", "Company": "company"}
    )
    await import_service.commit(batch.import_batch_id)

    final = await import_service.get_batch(batch.import_batch_id)
    row0, row1 = final.preview
    assert row0.commit_outcome == "created"
    assert row1.commit_outcome == "updated"
    assert row0.resolved_contact_id is not None
    assert row0.resolved_contact_id == row1.resolved_contact_id


# --- Requirement 2: commit() idempotency --------------------------------


async def test_second_commit_after_committed_creates_no_new_contacts(import_service):
    batch = await _upload_and_preview(import_service, "Email\nnew@example.com\n", {"Email": "email"})
    await import_service.commit(batch.import_batch_id)
    before = (await import_service.crm_service.list_contacts()).total

    await import_service.commit(batch.import_batch_id)
    after = (await import_service.crm_service.list_contacts()).total

    assert before == after == 1


async def test_second_commit_after_committed_returns_the_same_deterministic_report(import_service):
    batch = await _upload_and_preview(
        import_service, "Email\nnew@example.com\nanother@example.com\n", {"Email": "email"}
    )
    first = await import_service.commit(batch.import_batch_id)
    second = await import_service.commit(batch.import_batch_id)

    assert first == second
    assert second.created == 2


async def test_second_commit_after_committed_updates_nothing(import_service):
    existing = await import_service.crm_service.create_contact({"email": "known@example.com"})
    batch = await _upload_and_preview(import_service, "Email,Company\nknown@example.com,Acme\n", {"Email": "email", "Company": "company"})
    await import_service.commit(batch.import_batch_id)
    once_updated = await import_service.crm_service.get_contact(existing.crm_contact_id)

    await import_service.commit(batch.import_batch_id)
    twice_called = await import_service.crm_service.get_contact(existing.crm_contact_id)

    assert once_updated.company == twice_called.company == "Acme"
    assert once_updated.updated_at == twice_called.updated_at  # never re-saved the second time


async def test_second_commit_after_committed_does_not_re_log_activity(import_service):
    batch = await _upload_and_preview(import_service, "Email\nnew@example.com\n", {"Email": "email"})
    await import_service.commit(batch.import_batch_id)
    await import_service.commit(batch.import_batch_id)
    await import_service.commit(batch.import_batch_id)

    events = [e for e in await import_service.crm_service.activity_log.store.list() if e.event_type == "import.completed"]
    assert len(events) == 1


async def test_commit_outcomes_are_never_altered_by_a_later_commit_call(import_service):
    batch = await _upload_and_preview(import_service, "Email\nnew@example.com\n", {"Email": "email"})
    await import_service.commit(batch.import_batch_id)
    first_snapshot = (await import_service.get_batch(batch.import_batch_id)).preview[0]

    await import_service.commit(batch.import_batch_id)
    second_snapshot = (await import_service.get_batch(batch.import_batch_id)).preview[0]

    assert first_snapshot == second_snapshot


# --- Requirement 3: crash/retry resumability ----------------------------


async def test_crash_retry_resumes_after_row_0_of_n_without_recreating_it(import_service):
    """Simulates a crash that landed row 0's own commit durably (contact
    created, outcome persisted, exactly as a real interrupted-but-
    partially-checkpointed commit() would leave things) but never got to
    rows 1/2 -- status still COMMITTING. A fresh commit() call must finish
    rows 1/2 without touching row 0 again."""
    batch = await _upload_and_preview(
        import_service, "Email\na@example.com\nb@example.com\nc@example.com\n", {"Email": "email"}
    )

    row0_contact = await import_service.crm_service.create_contact_from_import(batch.preview[0].mapped_fields)
    batch.preview[0] = batch.preview[0].model_copy(
        update={"commit_outcome": "created", "resolved_contact_id": row0_contact.crm_contact_id}
    )
    batch.status = CrmImportBatchStatus.COMMITTING
    await import_service.batch_store.save(batch)

    report = await import_service.commit(batch.import_batch_id)

    assert report.created == 3
    contacts = (await import_service.crm_service.list_contacts()).items
    assert len(contacts) == 3
    assert {c.email for c in contacts} == {"a@example.com", "b@example.com", "c@example.com"}

    final = await import_service.get_batch(batch.import_batch_id)
    assert final.status == CrmImportBatchStatus.COMMITTED
    assert final.preview[0].resolved_contact_id == row0_contact.crm_contact_id  # untouched


async def test_crash_retry_midway_through_multiple_creates_does_not_duplicate_earlier_ones(import_service):
    batch = await _upload_and_preview(
        import_service, "Email\na@example.com\nb@example.com\nc@example.com\nd@example.com\n", {"Email": "email"}
    )

    # Simulate rows 0 and 1 already durably committed by a crashed attempt.
    resolved_ids = []
    for i in (0, 1):
        contact = await import_service.crm_service.create_contact_from_import(batch.preview[i].mapped_fields)
        resolved_ids.append(contact.crm_contact_id)
        batch.preview[i] = batch.preview[i].model_copy(update={"commit_outcome": "created", "resolved_contact_id": contact.crm_contact_id})
    batch.status = CrmImportBatchStatus.COMMITTING
    await import_service.batch_store.save(batch)

    create_calls = []
    original_create = import_service.crm_service.create_contact_from_import

    async def counting_create(mapped_fields):
        create_calls.append(mapped_fields.get("email"))
        return await original_create(mapped_fields)

    import_service.crm_service.create_contact_from_import = counting_create
    try:
        report = await import_service.commit(batch.import_batch_id)
    finally:
        import_service.crm_service.create_contact_from_import = original_create

    assert create_calls == ["c@example.com", "d@example.com"]  # rows 0/1 never re-created
    assert report.created == 4
    assert (await import_service.crm_service.list_contacts()).total == 4

    final = await import_service.get_batch(batch.import_batch_id)
    assert final.preview[0].resolved_contact_id == resolved_ids[0]
    assert final.preview[1].resolved_contact_id == resolved_ids[1]


async def test_crash_retry_midway_through_updates_does_not_reapply_the_already_applied_update(import_service):
    contact_a = await import_service.crm_service.create_contact({"email": "a@example.com"})
    contact_b = await import_service.crm_service.create_contact({"email": "b@example.com"})
    batch = await _upload_and_preview(
        import_service, "Email,Company\na@example.com,Acme\nb@example.com,Globex\n", {"Email": "email", "Company": "company"}
    )

    # Row 0's update already landed durably (crashed attempt); row 1 not yet.
    merged = import_service.crm_service.apply_import_mapping(contact_a, batch.preview[0].mapped_fields, is_new=False)
    await import_service.crm_service.contact_store.save(merged)
    batch.preview[0] = batch.preview[0].model_copy(update={"commit_outcome": "updated", "resolved_contact_id": contact_a.crm_contact_id})
    batch.status = CrmImportBatchStatus.COMMITTING
    await import_service.batch_store.save(batch)

    save_calls = []
    original_save = import_service.crm_service.contact_store.save

    async def counting_save(contact):
        save_calls.append(contact.crm_contact_id)
        await original_save(contact)

    import_service.crm_service.contact_store.save = counting_save
    try:
        report = await import_service.commit(batch.import_batch_id)
    finally:
        import_service.crm_service.contact_store.save = original_save

    assert save_calls == [contact_b.crm_contact_id]  # contact_a never re-saved
    assert report.updated == 2

    final_a = await import_service.crm_service.get_contact(contact_a.crm_contact_id)
    final_b = await import_service.crm_service.get_contact(contact_b.crm_contact_id)
    assert final_a.company == "Acme"
    assert final_b.company == "Globex"


async def test_batch_status_is_committing_while_partially_resolved(import_service):
    batch = await _upload_and_preview(import_service, "Email\na@example.com\nb@example.com\n", {"Email": "email"})

    contact = await import_service.crm_service.create_contact_from_import(batch.preview[0].mapped_fields)
    batch.preview[0] = batch.preview[0].model_copy(update={"commit_outcome": "created", "resolved_contact_id": contact.crm_contact_id})
    batch.status = CrmImportBatchStatus.COMMITTING
    await import_service.batch_store.save(batch)

    mid_flight = await import_service.get_batch(batch.import_batch_id)
    assert mid_flight.status == CrmImportBatchStatus.COMMITTING

    await import_service.commit(batch.import_batch_id)
    finished = await import_service.get_batch(batch.import_batch_id)
    assert finished.status == CrmImportBatchStatus.COMMITTED


async def test_resuming_from_committing_logs_activity_exactly_once(import_service):
    batch = await _upload_and_preview(import_service, "Email\na@example.com\nb@example.com\n", {"Email": "email"})

    contact = await import_service.crm_service.create_contact_from_import(batch.preview[0].mapped_fields)
    batch.preview[0] = batch.preview[0].model_copy(update={"commit_outcome": "created", "resolved_contact_id": contact.crm_contact_id})
    batch.status = CrmImportBatchStatus.COMMITTING
    await import_service.batch_store.save(batch)

    await import_service.commit(batch.import_batch_id)

    events = [e for e in await import_service.crm_service.activity_log.store.list() if e.event_type == "import.completed"]
    assert len(events) == 1
    assert events[0].metadata["created"] == 2  # reflects BOTH rows, not just the one processed this call


async def _sqlite_import_service(tmp_path):
    """Real SQLite-backed batch_store, unlike the `import_service` fixture
    above -- required for these two tests specifically, since
    MemoryCrmImportBatchStore holds a live reference to the same mutable
    CrmImportBatch object commit() mutates in place, so a "failed" save()
    there would still leave the row's outcome visible in the store,
    masking the exact crash-window behavior being tested here. A real
    store only reflects what was actually, successfully written."""
    crm = CrmService()
    batch_store = SQLiteCrmImportBatchStore(str(tmp_path / "import.db"))
    await batch_store.connect()
    return CrmImportService(crm_service=crm, batch_store=batch_store), batch_store


async def test_recovery_closes_the_gap_for_a_row_with_a_confident_identifier(tmp_path):
    """The 2026-09-03 post-review fix: even if the checkpoint save AFTER a
    row's CrmContact mutation fails (the row is left durably marked
    "creating", not yet "created"), a row carrying a confident identifier
    (email here) is fully protected -- a retry recovers the contact
    _find_existing_by_confident_identifiers() finds, rather than creating
    a second one."""
    import_service, batch_store = await _sqlite_import_service(tmp_path)
    try:
        batch = await _upload_and_preview(import_service, "Email\nonly@example.com\n", {"Email": "email"})

        save_call_count = 0
        original_save = batch_store.save

        async def failing_after_the_mutation(saved_batch):
            nonlocal save_call_count
            save_call_count += 1
            # call 1 = COMMITTING write; call 2 = the "creating" marker
            # (written BEFORE the mutation); call 3 = the post-mutation
            # "created" checkpoint -- this is the one we fail.
            if save_call_count == 3:
                raise RuntimeError("simulated crash after the contact was created but before its outcome landed")
            await original_save(saved_batch)

        import_service.batch_store.save = failing_after_the_mutation
        try:
            with pytest.raises(RuntimeError):
                await import_service.commit(batch.import_batch_id)
        finally:
            import_service.batch_store.save = original_save

        # The contact WAS created; the row is durably stuck at "creating".
        assert (await import_service.crm_service.list_contacts()).total == 1
        mid_flight = await import_service.get_batch(batch.import_batch_id)
        assert mid_flight.preview[0].commit_outcome == "creating"

        report = await import_service.commit(batch.import_batch_id)

        # Recovered, not duplicated.
        assert (await import_service.crm_service.list_contacts()).total == 1
        assert report.created == 1
        final = await import_service.get_batch(batch.import_batch_id)
        assert final.preview[0].commit_outcome == "created"
        assert final.preview[0].resolved_contact_id is not None
    finally:
        await batch_store.close()


async def test_documented_residual_gap_for_a_row_with_no_confident_identifier_at_all(tmp_path):
    """Honest documentation of the ONE gap that remains, precisely
    quantified by commit()'s own docstring: a row with NO confident
    identifier (no email, no apollo_contact_id, no linkedin_url) has
    nothing for CrmContactStore's UNIQUE constraints OR
    _find_existing_by_confident_identifiers() to key off. If the
    post-mutation checkpoint save fails for such a row, a retry finds no
    durable marker and no recoverable match, and legitimately creates a
    second contact. This is NOT a silent surprise -- it's the exact,
    narrow, documented boundary of what Stage 4A guarantees."""
    import_service, batch_store = await _sqlite_import_service(tmp_path)
    try:
        batch = await _upload_and_preview(
            import_service, "First Name,Last Name\nGhost,NoIdentifier\n", {"First Name": "first_name", "Last Name": "last_name"}
        )
        assert batch.preview[0].status == CrmImportRowStatus.NEW  # nothing to match against in an empty CRM

        save_call_count = 0
        original_save = batch_store.save

        async def failing_after_the_mutation(saved_batch):
            nonlocal save_call_count
            save_call_count += 1
            if save_call_count == 3:
                raise RuntimeError("simulated crash after the contact was created but before its outcome landed")
            await original_save(saved_batch)

        import_service.batch_store.save = failing_after_the_mutation
        try:
            with pytest.raises(RuntimeError):
                await import_service.commit(batch.import_batch_id)
        finally:
            import_service.batch_store.save = original_save

        assert (await import_service.crm_service.list_contacts()).total == 1

        # A retry, finding no durable outcome AND no recoverable match
        # (no confident identifier to look up by), legitimately reprocesses
        # this row -- producing a second contact. This is the accepted,
        # narrow residual gap.
        await import_service.commit(batch.import_batch_id)
        assert (await import_service.crm_service.list_contacts()).total == 2
    finally:
        await batch_store.close()


# --- preview() guard against discarding commit progress ------------------


async def test_preview_is_refused_once_committing(import_service):
    batch = await _upload_and_preview(import_service, "Email\na@example.com\n", {"Email": "email"})
    batch.status = CrmImportBatchStatus.COMMITTING
    await import_service.batch_store.save(batch)

    with pytest.raises(ValueError):
        await import_service.preview(batch.import_batch_id, {"Email": "email"})


async def test_preview_is_refused_once_committed(import_service):
    batch = await _upload_and_preview(import_service, "Email\na@example.com\n", {"Email": "email"})
    await import_service.commit(batch.import_batch_id)

    with pytest.raises(ValueError):
        await import_service.preview(batch.import_batch_id, {"Email": "email"})


# --- Requirement 5: durable resolved-contact retrieval --------------------


async def test_list_resolved_contact_ids_excludes_skipped_error_and_dedupes(import_service):
    await import_service.crm_service.create_contact({"first_name": "Ada", "last_name": "Lovelace", "company": "Acme"})
    batch = await import_service.upload(
        "p.csv",
        csv_bytes(
            "Email,First Name,Last Name,Company\n"
            "created@example.com,,,\n"
            "dup@example.com,,,\n"
            "dup@example.com,,,\n"
            ",Ada,Lovelace,Acme\n"
        ),
    )
    previewed = await import_service.preview(
        batch.import_batch_id, {"Email": "email", "First Name": "first_name", "Last Name": "last_name", "Company": "company"}
    )
    await import_service.commit(previewed.import_batch_id, decisions={0: "create", 1: "create", 2: "update"})
    # row 3 (Ada/Lovelace/Acme, no email) defaults to POSSIBLE_DUPLICATE -> skip

    ids = await import_service.list_resolved_contact_ids(batch.import_batch_id)
    contacts = (await import_service.crm_service.list_contacts()).items
    dup_contact = next(c for c in contacts if c.email == "dup@example.com")
    created_contact = next(c for c in contacts if c.email == "created@example.com")

    assert set(ids) == {created_contact.crm_contact_id, dup_contact.crm_contact_id}
    assert len(ids) == 2  # rows 1 and 2 both resolve to the SAME contact -- one id, not two


async def test_list_resolved_contact_ids_requires_fully_committed_batch(import_service):
    batch = await _upload_and_preview(import_service, "Email\na@example.com\n", {"Email": "email"})

    with pytest.raises(ValueError):
        await import_service.list_resolved_contact_ids(batch.import_batch_id)  # still MAPPED

    contact = await import_service.crm_service.create_contact_from_import(batch.preview[0].mapped_fields)
    batch.preview[0] = batch.preview[0].model_copy(update={"commit_outcome": "created", "resolved_contact_id": contact.crm_contact_id})
    batch.status = CrmImportBatchStatus.COMMITTING
    await import_service.batch_store.save(batch)

    with pytest.raises(ValueError):
        await import_service.list_resolved_contact_ids(batch.import_batch_id)  # COMMITTING, not COMMITTED yet


async def test_human_override_to_create_a_possible_duplicate_is_never_reinterpreted_on_retry(import_service):
    """The core safety guarantee behind the "creating" marker: a human
    explicitly overriding a POSSIBLE_DUPLICATE (name+company fallback
    match) row to "create" -- meaning a deliberately SEPARATE new contact
    -- must still get that separate contact even after a simulated
    crash/retry, and recovery must find THAT new contact (via this row's
    OWN email), never silently fall back to merging into the ORIGINAL
    flagged contact just because it happens to match on name+company."""
    original = await import_service.crm_service.create_contact({"first_name": "Ada", "last_name": "Lovelace", "company": "Acme"})
    batch = await _upload_and_preview(
        import_service, "First Name,Last Name,Company,Email\nAda,Lovelace,Acme,newada@example.com\n",
        {"First Name": "first_name", "Last Name": "last_name", "Company": "company", "Email": "email"},
    )
    assert batch.preview[0].status == CrmImportRowStatus.POSSIBLE_DUPLICATE
    assert batch.preview[0].matched_contact_id == original.crm_contact_id

    # Simulate the human's explicit "create anyway" decision having
    # already reached the durable "creating" marker (e.g. a crash right
    # after this call started, mirroring what commit() itself would have
    # durably written).
    batch.preview[0] = batch.preview[0].model_copy(update={"commit_outcome": "creating"})
    batch.status = CrmImportBatchStatus.COMMITTING
    await import_service.batch_store.save(batch)

    report = await import_service.commit(batch.import_batch_id, decisions={0: "create"})

    assert report.created == 1
    contacts = (await import_service.crm_service.list_contacts()).items
    assert len(contacts) == 2  # the original AND the deliberately separate new one

    final = await import_service.get_batch(batch.import_batch_id)
    new_contact_id = final.preview[0].resolved_contact_id
    assert new_contact_id is not None
    assert new_contact_id != original.crm_contact_id  # never merged into the flagged match

    new_contact = await import_service.crm_service.get_contact(new_contact_id)
    assert new_contact.email == "newada@example.com"


# --- Activity Log ordering (verified per the reviewer's explicit ask) ------


async def test_committed_status_is_durable_even_if_the_completion_event_is_never_written(import_service, monkeypatch):
    """Verifies the exact ordering commit()'s own docstring documents:
    batch.status is saved as COMMITTED strictly BEFORE the
    import.completed event is recorded. Simulates the event write
    silently never landing (matching ActivityLogService.record()'s own
    real contract -- it never raises to its caller, it just may not
    persist anything on failure) and confirms: the batch ends up
    correctly, durably COMMITTED regardless, and a LATER commit() call
    (the fast path) does not retroactively -- or redundantly -- attempt
    to log it. At most once, never duplicated, occasionally lost -- by
    design, matching ActivityLogService's own documented contract."""

    async def swallowing_record(*args, **kwargs):
        return None  # exactly what record() itself does internally on a failed underlying write

    monkeypatch.setattr(import_service.crm_service.activity_log, "record", swallowing_record)

    batch = await _upload_and_preview(import_service, "Email\na@example.com\n", {"Email": "email"})
    report = await import_service.commit(batch.import_batch_id)

    assert report.created == 1
    final = await import_service.get_batch(batch.import_batch_id)
    assert final.status == CrmImportBatchStatus.COMMITTED  # durable regardless of the log write's fate

    events = [e for e in await import_service.crm_service.activity_log.store.list() if e.event_type == "import.completed"]
    assert events == []  # lost, not duplicated

    monkeypatch.undo()  # restore the real record() for the retry below
    await import_service.commit(batch.import_batch_id)
    events_after_retry = [e for e in await import_service.crm_service.activity_log.store.list() if e.event_type == "import.completed"]
    assert events_after_retry == []  # the COMMITTED fast path never re-enters the logging code -- still zero, not two


async def test_list_resolved_contact_ids_after_full_commit(import_service):
    batch = await _upload_and_preview(import_service, "Email\na@example.com\nb@example.com\n", {"Email": "email"})
    await import_service.commit(batch.import_batch_id)

    ids = await import_service.list_resolved_contact_ids(batch.import_batch_id)
    assert len(ids) == 2


# --- Backward compatibility with pre-Stage-4A production data ------------


async def test_commit_on_a_pre_stage_4a_committed_batch_refuses_rather_than_misreporting(import_service):
    """A real production shape: a CrmImportBatch already COMMITTED before
    commit_outcome tracking existed has every row's commit_outcome as
    None. Calling commit() again on it must not silently return a
    misleading all-zero report (and must not re-run the whole commit,
    which is what the pre-Stage-4A code would have done, creating
    duplicates) -- it must refuse loudly instead."""
    batch = await _upload_and_preview(import_service, "Email\nlegacy@example.com\n", {"Email": "email"})
    committed = batch.model_copy(update={"status": CrmImportBatchStatus.COMMITTED})  # no commit_outcome ever set
    await import_service.batch_store.save(committed)

    with pytest.raises(ValueError):
        await import_service.commit(batch.import_batch_id)

    # No contact was created by this refused attempt.
    assert (await import_service.crm_service.list_contacts()).total == 0
