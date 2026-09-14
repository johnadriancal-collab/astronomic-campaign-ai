"""
Historical dinner migration driver (Event History generalization stage,
dinners_attended audit, 2026-09-14).

Reads the frozen manifest in historical_dinner_migration_manifest.py and,
for A_READY rows only, idempotently creates (get-or-create by exact
name/title, same pattern as austin_forward_event_history_backfill.py):

  - the Client (an existing Client reused by exact name when
    existing_client=True, otherwise the "Astronomic — Direct Events"
    pseudo-client for a row whose client_name is None)
  - one Engagement per manifest row (get-or-create by (client_id, title))
  - one EngagementParticipant per contact_id in that row, with
    attendance_status=ATTENDED, source=MANUAL, role=ParticipantRole.GUEST
    (the "normal/default" role -- historical dinners_attended rows carry
    no per-person role information, so nothing is guessed)

Any row not in bucket A_READY is never written by this module --
compute_dry_run_report() reports it as held, and apply_ready_rows()
silently skips it (counted, never acted on). Only a human decision on the
manifest itself (not a flag to this module) can promote a row into
A_READY. As of 2026-09-14 every row in the manifest is A_READY -- the
mechanism is kept generic so a future manifest addition can still be
held back for review before this module acts on it.

custom_fields.dinners_attended itself is never read for writing, never
modified, and never deleted by this module -- it remains the historical
source/audit record, exactly as instructed.

Client.status on a NEWLY CREATED historical Client is explicitly set to
INACTIVE, never left to the model's own default of ACTIVE. These 34+
Clients exist purely to reconstruct past dinner attendance; Client.status
docstring itself describes INACTIVE as "still a real, visible historical
record" (as opposed to `archived`, which hides it) -- the closest thing
this model has to a neutral/historical state, since it has no explicit
"prospect" or "unknown" status. relationship_classification, owner, and
next_action/next_action_due are all left at their own defaults (None) --
never guessed, matching Client's own "a freshly-created Client has not
been through any follow-up cycle yet" rule. An EXISTING Client being
reused (Hive ASMBLD, and Astronomic — Direct Events after its first
creation during the Austin Forward backfill) is looked up and reused
as-is -- create_client() is never called for it, so none of its current
fields (status included) are ever touched here.

SAFE BY DEFAULT: compute_dry_run_report() never writes anything.
apply_ready_rows() is the only write path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum

from app.models.client_crm import (
    Client,
    ClientStatus,
    DIRECT_EVENTS_CLIENT_NAME,
    Engagement,
    EngagementStatus,
    EngagementType,
    ParticipantRole,
)
from app.services.client_crm_service import ClientCrmService, EngagementParticipantDuplicate
from app.services.historical_dinner_migration_manifest import MANIFEST_ROWS

READY_BUCKET = "A_READY"


class ParticipantOutcome(str, Enum):
    WOULD_LINK = "would_link"
    LINKED = "linked"
    ALREADY_LINKED = "already_linked"
    UNRESOLVED_CONTACT = "unresolved_contact"
    ERROR = "error"


@dataclass
class RowReport:
    raw_value: str
    canonical_event_name: str
    client_name: str | None
    bucket: str
    contact_count: int
    client_id: str | None = None
    engagement_id: str | None = None
    linked: int = 0
    would_link: int = 0
    already_linked: int = 0
    unresolved_contact: int = 0
    errors: int = 0


@dataclass
class MigrationReport:
    write_mode: bool
    ready_rows: int = 0
    held_rows: int = 0
    rows: list[RowReport] = field(default_factory=list)


def _row_date(row: dict) -> date | None:
    if not row["event_date"]:
        return None
    y, m, d = row["event_date"].split("-")
    return date(int(y), int(m), int(d))


async def _find_client_by_name(service: ClientCrmService, name: str) -> Client | None:
    for c in await service.client_store.list():
        if c.name == name and not c.archived:
            return c
    return None


async def _find_engagement(service: ClientCrmService, client_id: str, title: str, event_date: date | None) -> Engagement | None:
    """Matches on (title, event_date), never title alone -- several manifest
    rows share the same canonical_event_name (e.g. two distinct "FD SaaS"
    dinners on different dates) and must never collapse into one
    Engagement. Title-only matching silently merged these on first
    implementation; caught by test_fd_series_rows_share_the_direct_events_
    pseudo_client_and_stay_distinct_engagements before this ever ran for
    real."""
    for e in await service.engagement_store.list_for_client(client_id):
        if e.title == title and e.engagement_date == event_date and not e.archived:
            return e
    return None


async def compute_dry_run_report(service: ClientCrmService) -> MigrationReport:
    """Read-only. Never creates a Client, Engagement, or participant."""
    report = MigrationReport(write_mode=False)

    for row in MANIFEST_ROWS:
        if row["bucket"] != READY_BUCKET:
            report.held_rows += 1
            continue
        report.ready_rows += 1

        target_client_name = row["client_name"] or DIRECT_EVENTS_CLIENT_NAME
        client = await _find_client_by_name(service, target_client_name)
        engagement = await _find_engagement(service, client.client_id, row["canonical_event_name"], _row_date(row)) if client else None

        existing_participant_contact_ids: set[str] = set()
        if engagement:
            for p in await service.engagement_participant_store.list_for_engagement(engagement.engagement_id):
                if not p.archived and p.crm_contact_id:
                    existing_participant_contact_ids.add(p.crm_contact_id)

        row_report = RowReport(
            raw_value=row["raw_value"],
            canonical_event_name=row["canonical_event_name"],
            client_name=row["client_name"],
            bucket=row["bucket"],
            contact_count=len(row["contact_ids"]),
            client_id=client.client_id if client else None,
            engagement_id=engagement.engagement_id if engagement else None,
        )
        for contact_id in row["contact_ids"]:
            if contact_id in existing_participant_contact_ids:
                row_report.already_linked += 1
            else:
                row_report.would_link += 1
        report.rows.append(row_report)

    return report


async def apply_ready_rows(service: ClientCrmService) -> MigrationReport:
    """The write path. A_READY rows only -- B/C rows are counted as
    held_rows and never written. Idempotent via get-or-create-by-name for
    Client/Engagement and the store's own unique-index-backed duplicate
    detection for participants (identical mechanism to
    austin_forward_event_history_backfill.apply_backfill)."""
    report = MigrationReport(write_mode=True)

    for row in MANIFEST_ROWS:
        if row["bucket"] != READY_BUCKET:
            report.held_rows += 1
            continue
        report.ready_rows += 1

        target_client_name = row["client_name"] or DIRECT_EVENTS_CLIENT_NAME
        client = await _find_client_by_name(service, target_client_name)
        if client is None:
            # New historical Client: explicitly INACTIVE, never the model's
            # own ACTIVE default -- this is a record of a past dinner, not a
            # present-day relationship. See module docstring.
            client = await service.create_client({"name": target_client_name, "status": ClientStatus.INACTIVE.value})

        engagement = await _find_engagement(service, client.client_id, row["canonical_event_name"], _row_date(row))
        if engagement is None:
            engagement = await service.create_client_engagement(
                client.client_id,
                {
                    "title": row["canonical_event_name"],
                    "engagement_type": EngagementType.OTHER,
                    "engagement_date": _row_date(row),
                    "location": row["location"],
                    "status": EngagementStatus.COMPLETED,
                },
            )

        row_report = RowReport(
            raw_value=row["raw_value"],
            canonical_event_name=row["canonical_event_name"],
            client_name=row["client_name"],
            bucket=row["bucket"],
            contact_count=len(row["contact_ids"]),
            client_id=client.client_id,
            engagement_id=engagement.engagement_id,
        )

        for contact_id in row["contact_ids"]:
            try:
                await service.create_engagement_participant(
                    client.client_id,
                    engagement.engagement_id,
                    {"crm_contact_id": contact_id, "role": ParticipantRole.GUEST.value, "attendance_status": "attended"},
                )
                row_report.linked += 1
            except EngagementParticipantDuplicate:
                row_report.already_linked += 1
            except Exception:  # noqa: BLE001 -- one bad row must never abort the whole batch
                row_report.errors += 1

        report.rows.append(row_report)

    return report
