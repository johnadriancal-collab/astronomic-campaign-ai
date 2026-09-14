"""
Austin Forward Event History backfill (Event History generalization stage,
2026-09-14).

Creates (idempotently) the reserved "Astronomic -- Direct Events"
pseudo-Client, the "Austin Forward VIP Gathering" Engagement under it, and
one EngagementParticipant per resolved attendee from the already-frozen,
already-approved Austin Forward reconciliation cohort
(app/services/austin_forward_reconciliation_cohort.py) -- the exact 166
people (55 existing + 111 new Contacts) that Contact-side Austin Forward
reconciliation already applied. This module never touches CrmContact at
all except a read-only lookup to resolve a new Contact's current
crm_contact_id -- no custom_fields, no suppression, no dinners_attended,
nothing on the Contact record itself is read for writing or written here.

SAFE BY DEFAULT: compute_dry_run_report() never writes anything. Only
apply_backfill() does, and it reuses ClientCrmService's own
create_client()/create_client_engagement()/create_engagement_participant()
verbatim -- no bespoke store-writing logic here, so this gets the exact
same snapshot-population, Activity Log, and Contacts CRM Stage 3B
positive-interest-signal behavior any other EngagementParticipant creation
already gets, for free.

Idempotent by construction, not by a frozen plan-then-apply snapshot:
  - the Client is looked up by exact name (DIRECT_EVENTS_CLIENT_NAME)
    before ever creating one -- a second run finds the same Client.
  - the Engagement is looked up by (client_id, title) before ever creating
    one -- a second run finds the same Engagement.
  - each participant creation is guarded by the store's own real SQLite
    partial unique index on (engagement_id, crm_contact_id) -- a second
    run's create_engagement_participant() call raises
    EngagementParticipantDuplicate for a Contact already linked, caught
    here and reported as ALREADY_LINKED (no-op), never a duplicate row.
  - because idempotency is checked live against the database at write
    time (never a stale frozen snapshot), running this against a
    partially-completed prior run (e.g. one that errored partway through)
    is exactly as safe as running it fresh.

Never creates a new CrmContact and never re-matches identity for the 55
EXISTING_CONTACTS rows (their crm_contact_id is used directly, exactly as
approved). The 111 NEW_CONTACTS rows are re-resolved ONLY by exact
normalized email/LinkedIn match against CURRENT production Contacts (the
same people the Austin Forward Contact reconciliation already created) --
never fuzzy-matched; a row that cannot be resolved to EXACTLY one Contact
is reported as UNRESOLVED_CONTACT rather than guessed or skipped silently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from enum import Enum

from app.models.client_crm import (
    Client,
    DIRECT_EVENTS_CLIENT_NAME,
    Engagement,
    EngagementStatus,
    EngagementType,
    ParticipantRole,
)
from app.services.austin_forward_reconciliation_cohort import EXISTING_CONTACTS, NEW_CONTACTS
from app.services.client_crm_service import ClientCrmService, EngagementParticipantDuplicate

ENGAGEMENT_TITLE = "Austin Forward VIP Gathering"
ENGAGEMENT_DATE = date(2026, 9, 10)
ENGAGEMENT_LOCATION = "Austin, Texas"

# The frozen cohort's own role_in_event strings ("Host"/"Guest"/"Sponsors" --
# see austin_forward_reconciliation_cohort.py) mapped to the permanent,
# Astronomic-wide ParticipantRole taxonomy. Sponsors -> SPONSOR (added this
# same stage after confirming it's a safe, purely additive enum extension --
# see ParticipantRole's own docstring).
ROLE_IN_EVENT_MAP: dict[str, ParticipantRole] = {
    "Host": ParticipantRole.HOST,
    "Guest": ParticipantRole.GUEST,
    "Sponsors": ParticipantRole.SPONSOR,
}


def _normalize_email(email: str | None) -> str | None:
    if not email:
        return None
    e = email.strip().lower()
    return e or None


def _normalize_linkedin(url: str | None) -> str | None:
    if not url:
        return None
    u = url.strip().lower()
    if not u:
        return None
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    u = u.rstrip("/").split("?")[0]
    return u or None


class RowOutcome(str, Enum):
    WOULD_LINK = "would_link"
    LINKED = "linked"
    ALREADY_LINKED = "already_linked"
    UNRESOLVED_CONTACT = "unresolved_contact"
    ERROR = "error"


@dataclass
class RowResult:
    full_name: str
    role_in_event: str
    crm_contact_id: str | None
    outcome: RowOutcome
    detail: str = ""


@dataclass
class BackfillCounts:
    cohort_total: int = 0
    would_link: int = 0
    linked: int = 0
    already_linked: int = 0
    unresolved_contact: int = 0
    errors: int = 0


@dataclass
class BackfillReport:
    client_id: str | None
    engagement_id: str | None
    counts: BackfillCounts
    results: list[RowResult] = field(default_factory=list)


def _resolve_new_contact_id(
    email: str | None, linkedin_url: str | None, email_index: dict[str, str], linkedin_index: dict[str, str]
) -> str | None:
    ne = _normalize_email(email)
    nl = _normalize_linkedin(linkedin_url)
    ids: set[str] = set()
    if ne and ne in email_index:
        ids.add(email_index[ne])
    if nl and nl in linkedin_index:
        ids.add(linkedin_index[nl])
    if len(ids) == 1:
        return next(iter(ids))
    return None


async def _find_existing_client(service: ClientCrmService) -> Client | None:
    for c in await service.client_store.list():
        if c.name == DIRECT_EVENTS_CLIENT_NAME and not c.archived:
            return c
    return None


async def _find_existing_engagement(service: ClientCrmService, client_id: str) -> Engagement | None:
    for e in await service.engagement_store.list_for_client(client_id):
        if e.title == ENGAGEMENT_TITLE and not e.archived:
            return e
    return None


async def _build_contact_indexes(service: ClientCrmService) -> tuple[dict[str, str], dict[str, str]]:
    """email/linkedin -> crm_contact_id, over every non-archived Contact --
    used ONLY to re-resolve the 111 NEW_CONTACTS rows (which had no
    crm_contact_id at cohort-freeze time, since they didn't exist yet)."""
    email_index: dict[str, str] = {}
    linkedin_index: dict[str, str] = {}
    for c in await service.crm_contact_store.list():
        if c.archived:
            continue
        ne = _normalize_email(c.email)
        nl = _normalize_linkedin(c.linkedin_url)
        if ne:
            email_index.setdefault(ne, c.crm_contact_id)
        if nl:
            linkedin_index.setdefault(nl, c.crm_contact_id)
    return email_index, linkedin_index


async def compute_dry_run_report(service: ClientCrmService) -> BackfillReport:
    """Read-only. Never creates the Client, the Engagement, or any
    participant. If the Client/Engagement don't exist yet, every resolvable
    row is reported WOULD_LINK (they would be created fresh on a real run);
    if they DO already exist, already-linked Contacts are correctly
    reported ALREADY_LINKED by checking the real current participant
    list."""
    email_index, linkedin_index = await _build_contact_indexes(service)

    client = await _find_existing_client(service)
    engagement = await _find_existing_engagement(service, client.client_id) if client else None

    existing_participant_contact_ids: set[str] = set()
    if engagement:
        for p in await service.engagement_participant_store.list_for_engagement(engagement.engagement_id):
            if not p.archived and p.crm_contact_id:
                existing_participant_contact_ids.add(p.crm_contact_id)

    cohort = list(EXISTING_CONTACTS) + list(NEW_CONTACTS)
    counts = BackfillCounts(cohort_total=len(cohort))
    results: list[RowResult] = []

    for row in EXISTING_CONTACTS:
        _classify_row(row.full_name, row.role_in_event, row.crm_contact_id, existing_participant_contact_ids, counts, results)
    for row in NEW_CONTACTS:
        contact_id = _resolve_new_contact_id(row.email, row.linkedin_url, email_index, linkedin_index)
        _classify_row(row.full_name, row.role_in_event, contact_id, existing_participant_contact_ids, counts, results)

    return BackfillReport(
        client_id=client.client_id if client else None,
        engagement_id=engagement.engagement_id if engagement else None,
        counts=counts,
        results=results,
    )


def _classify_row(
    full_name: str,
    role_in_event: str,
    contact_id: str | None,
    existing_participant_contact_ids: set[str],
    counts: BackfillCounts,
    results: list[RowResult],
) -> None:
    if contact_id is None:
        counts.unresolved_contact += 1
        results.append(RowResult(full_name, role_in_event, None, RowOutcome.UNRESOLVED_CONTACT))
        return
    if contact_id in existing_participant_contact_ids:
        counts.already_linked += 1
        results.append(RowResult(full_name, role_in_event, contact_id, RowOutcome.ALREADY_LINKED))
        return
    counts.would_link += 1
    results.append(RowResult(full_name, role_in_event, contact_id, RowOutcome.WOULD_LINK))


async def apply_backfill(service: ClientCrmService) -> BackfillReport:
    """The write path. Gets-or-creates the Client and Engagement, then
    creates one EngagementParticipant per resolvable cohort row, reusing
    ClientCrmService.create_engagement_participant() verbatim (never a
    bespoke store write) -- attendance_status=ATTENDED, source=MANUAL
    (server-set by that method already), role from ROLE_IN_EVENT_MAP.
    A duplicate (already-linked) Contact is caught and reported as a
    no-op, never a crash, never a second row."""
    client = await _find_existing_client(service)
    if client is None:
        client = await service.create_client({"name": DIRECT_EVENTS_CLIENT_NAME})

    engagement = await _find_existing_engagement(service, client.client_id)
    if engagement is None:
        engagement = await service.create_client_engagement(
            client.client_id,
            {
                "title": ENGAGEMENT_TITLE,
                "engagement_type": EngagementType.OTHER,
                "engagement_date": ENGAGEMENT_DATE,
                "location": ENGAGEMENT_LOCATION,
                "status": EngagementStatus.COMPLETED,
            },
        )

    email_index, linkedin_index = await _build_contact_indexes(service)
    cohort_rows = [(r.full_name, r.role_in_event, r.crm_contact_id) for r in EXISTING_CONTACTS] + [
        (r.full_name, r.role_in_event, _resolve_new_contact_id(r.email, r.linkedin_url, email_index, linkedin_index))
        for r in NEW_CONTACTS
    ]

    counts = BackfillCounts(cohort_total=len(cohort_rows))
    results: list[RowResult] = []

    for full_name, role_in_event, contact_id in cohort_rows:
        if contact_id is None:
            counts.unresolved_contact += 1
            results.append(RowResult(full_name, role_in_event, None, RowOutcome.UNRESOLVED_CONTACT))
            continue

        role = ROLE_IN_EVENT_MAP[role_in_event]
        try:
            await service.create_engagement_participant(
                client.client_id,
                engagement.engagement_id,
                {"crm_contact_id": contact_id, "role": role.value, "attendance_status": "attended"},
            )
            counts.linked += 1
            results.append(RowResult(full_name, role_in_event, contact_id, RowOutcome.LINKED))
        except EngagementParticipantDuplicate:
            counts.already_linked += 1
            results.append(RowResult(full_name, role_in_event, contact_id, RowOutcome.ALREADY_LINKED))
        except Exception as exc:  # noqa: BLE001 -- one bad row must never abort the whole batch
            counts.errors += 1
            results.append(RowResult(full_name, role_in_event, contact_id, RowOutcome.ERROR, detail=str(exc)))

    return BackfillReport(client_id=client.client_id, engagement_id=engagement.engagement_id, counts=counts, results=results)
