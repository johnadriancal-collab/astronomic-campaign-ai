"""
Contacts CRM Stage 3C -- historical Engagement-Stage reconciliation runner
(see app/services/contact_engagement_stage_backfill.py for the actual
driver, which reuses Stage 3B's ContactEngagementSignalService verbatim).

SAFE BY DEFAULT: with no arguments, this is a full-cohort DRY RUN -- it
scans every current EngagementParticipant across every Engagement/Client,
classifies every Contact with a qualifying historical signal, and prints
the manifest + summary counts. Writes NOTHING (no Contact save, no
EngagementParticipant mutation, no Activity Log entry).

Real writes require ALL of the following, together:
  1. --write AND --confirm-production-writes (same two-gate convention as
     scripts/run_luma_engagement_participant_backfill.py -- a single
     boolean flag is too easy to flip by accident).
  2. An EXPLICIT target set: --contact-id <id> (repeatable) and/or
     --contact-ids-file <path> (one crm_contact_id per line, '#'-prefixed
     lines and blank lines ignored). There is NO mode that reconciles
     "every qualifying Contact the dry run found" automatically -- write
     mode always operates on exactly the ids you name, nothing else, even
     if a newer dry run would now propose more.

Each named Contact is re-evaluated LIVE at write time (current stage,
current qualifying participants) -- never a frozen replay of an earlier
dry-run manifest. A Contact who already advanced, was manually moved to a
protected stage, or whose only qualifying participant is no longer
positive since the manifest was generated, is correctly reported as a
no-op, not silently skipped or forced.

Usage (run as a module from the repo root, so `app.*` imports resolve):
    python3 -m scripts.run_contact_engagement_stage_backfill
    python3 -m scripts.run_contact_engagement_stage_backfill --database-path /app/data/campaigns.db
    python3 -m scripts.run_contact_engagement_stage_backfill \\
        --write --confirm-production-writes --contact-id abc-123 --contact-id def-456
    python3 -m scripts.run_contact_engagement_stage_backfill \\
        --write --confirm-production-writes --contact-ids-file approved_cohort.txt
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from app.config import settings
from app.repositories.sqlite_activity_event_store import SQLiteActivityEventStore
from app.repositories.sqlite_client_store import SQLiteClientStore
from app.repositories.sqlite_crm_contact_store import SQLiteCrmContactStore
from app.repositories.sqlite_engagement_participant_store import SQLiteEngagementParticipantStore
from app.repositories.sqlite_engagement_store import SQLiteEngagementStore
from app.services.activity_log_service import ActivityLogService
from app.services.contact_engagement_signal_service import ContactEngagementSignalService
from app.services.contact_engagement_stage_backfill import (
    HistoricalReconciliationReport,
    HistoricalWriteReport,
    apply_historical_reconciliation,
    compute_historical_reconciliation_report,
)


def _print_dry_run_report(report: HistoricalReconciliationReport) -> None:
    c = report.counts
    print("=== Contacts CRM Stage 3C -- Historical Reconciliation -- DRY RUN (zero writes) ===\n")
    print(f"total active EngagementParticipants:      {c.total_active_participants}")
    print(f"total Contacts with a positive signal:     {c.total_positive_signal_contacts}")
    print(f"  already Interested:                      {c.already_interested}")
    print(f"  eligible (unset/null):                    {c.eligible_unset}")
    print(f"  eligible (Cold):                          {c.eligible_cold}")
    print(f"  protected (Replied):                      {c.protected_replied}")
    print(f"  protected (Unresponsive):                 {c.protected_unresponsive}")
    print(f"  protected (unexpected value):              {c.protected_unexpected}")
    print(f"\nPROPOSED WRITE COUNT: {c.proposed_write_count}\n")
    print(f"signal type -- RSVP Confirmed: {c.signal_rsvp_confirmed}   Attendance Attended: {c.signal_attendance_attended}")
    print(f"source -- Manual: {c.source_manual}   Luma: {c.source_luma}")
    print(f"Contacts with >1 qualifying participant: {c.multi_signal_contacts}\n")

    print("--- Proposed write cohort ---")
    for row in report.write_candidates:
        print(
            f"  {row.crm_contact_id}  {row.contact_name!r}  {row.current_stage!r} -> {row.proposed_stage!r}  "
            f"via participant={row.trigger_participant_id} engagement={row.trigger_engagement_id} "
            f"({row.client_name} / {row.event_name} / {row.event_date})  role={row.role} "
            f"rsvp={row.rsvp_status} attendance={row.attendance_status} source={row.source} "
            f"signal={row.signal_type} qualifying_count={row.qualifying_participant_count}"
        )

    print("\n--- Protected (never touched) ---")
    for row in report.protected:
        print(f"  {row.crm_contact_id}  {row.contact_name!r}  stage={row.current_stage!r}  bucket={row.bucket}")


def _print_write_report(report: HistoricalWriteReport) -> None:
    print("=== Contacts CRM Stage 3C -- Historical Reconciliation -- WRITE MODE ===\n")
    if report.duplicate_ids_deduped:
        print(f"duplicate target ids deduped: {report.duplicate_ids_deduped}")
    if report.unknown_ids:
        print(f"unknown Contact ids (no such Contact): {report.unknown_ids}")
    print()
    for result in report.results:
        print(f"  {result.crm_contact_id}  {result.outcome:22s}  {result.detail}")
    print("\n--- Summary ---")
    for outcome, n in sorted(report.summary().items()):
        print(f"  {outcome:22s} {n}")


async def _run_dry_run(db_path: str) -> HistoricalReconciliationReport:
    client_store = SQLiteClientStore(db_path)
    engagement_store = SQLiteEngagementStore(db_path)
    engagement_participant_store = SQLiteEngagementParticipantStore(db_path)
    crm_contact_store = SQLiteCrmContactStore(db_path)
    await client_store.connect()
    await engagement_store.connect()
    await engagement_participant_store.connect()
    await crm_contact_store.connect()
    try:
        return await compute_historical_reconciliation_report(
            client_store, engagement_store, engagement_participant_store, crm_contact_store
        )
    finally:
        await client_store.close()
        await engagement_store.close()
        await engagement_participant_store.close()
        await crm_contact_store.close()


async def _run_write(db_path: str, crm_contact_ids: list[str]) -> HistoricalWriteReport:
    engagement_store = SQLiteEngagementStore(db_path)
    engagement_participant_store = SQLiteEngagementParticipantStore(db_path)
    crm_contact_store = SQLiteCrmContactStore(db_path)
    activity_event_store = SQLiteActivityEventStore(db_path)
    await engagement_store.connect()
    await engagement_participant_store.connect()
    await crm_contact_store.connect()
    await activity_event_store.connect()
    activity_log = ActivityLogService(activity_event_store)
    signal_service = ContactEngagementSignalService(crm_contact_store=crm_contact_store, activity_log=activity_log)
    try:
        return await apply_historical_reconciliation(
            crm_contact_ids, engagement_store, engagement_participant_store, crm_contact_store, signal_service
        )
    finally:
        await engagement_store.close()
        await engagement_participant_store.close()
        await crm_contact_store.close()
        await activity_event_store.close()


def _read_ids_file(path: str) -> list[str]:
    ids = []
    for line in Path(path).read_text().splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            ids.append(stripped)
    return ids


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Explicit no-op flag -- dry-run is already the default behavior.")
    parser.add_argument(
        "--write", action="store_true",
        help="Attempt REAL Contact engagement_stage writes. Requires --confirm-production-writes AND an explicit target set too.",
    )
    parser.add_argument(
        "--confirm-production-writes", action="store_true",
        help="Required alongside --write. Confirms you intend this run to mutate production Contacts.",
    )
    parser.add_argument("--contact-id", action="append", default=[], help="One target crm_contact_id. Repeatable.")
    parser.add_argument("--contact-ids-file", default=None, help="Path to a file of crm_contact_ids, one per line.")
    parser.add_argument("--database-path", default=None, help="Override the DB path (defaults to the app's own configured database_path).")
    args = parser.parse_args()

    if args.write and not args.confirm_production_writes:
        print(
            "Refusing to run: --write requires --confirm-production-writes as well. "
            "Nothing was read or written.",
            file=sys.stderr,
        )
        sys.exit(2)
    if args.confirm_production_writes and not args.write:
        print("Refusing to run: --confirm-production-writes has no effect without --write. Nothing was read or written.", file=sys.stderr)
        sys.exit(2)

    write_mode = args.write and args.confirm_production_writes
    db_path = args.database_path or settings.database_path

    if not write_mode:
        report = asyncio.run(_run_dry_run(db_path))
        _print_dry_run_report(report)
        return

    target_ids = list(args.contact_id)
    if args.contact_ids_file:
        target_ids.extend(_read_ids_file(args.contact_ids_file))
    if not target_ids:
        print(
            "Refusing to run: --write --confirm-production-writes requires an explicit target set "
            "(--contact-id and/or --contact-ids-file). There is no mode that reconciles every qualifying "
            "Contact automatically. Nothing was read or written.",
            file=sys.stderr,
        )
        sys.exit(2)

    write_report = asyncio.run(_run_write(db_path, target_ids))
    _print_write_report(write_report)
    if write_report.summary().get("failed"):
        sys.exit(1)


if __name__ == "__main__":
    main()
