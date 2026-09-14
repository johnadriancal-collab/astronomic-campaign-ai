"""
Austin Forward Event History backfill runner (Event History
generalization stage, 2026-09-14) -- see
app/services/austin_forward_event_history_backfill.py for the actual
driver, and app/services/austin_forward_reconciliation_cohort.py for the
frozen 166-person cohort this command is permanently scoped to.

SAFE BY DEFAULT: with no arguments, this is a DRY RUN. Writes NOTHING (no
Client, no Engagement, no EngagementParticipant, no Activity Log entry).

Real writes require BOTH --write AND --confirm-production-writes,
together -- same two-gate convention as every other operator write CLI in
this repo. There is NO target-id argument -- the write-eligible universe
is always exactly the frozen Austin Forward cohort.

Usage (run as a module from the repo root, so `app.*` imports resolve):
    python3 -m scripts.run_austin_forward_event_history_backfill
    python3 -m scripts.run_austin_forward_event_history_backfill --database-path /app/data/campaigns.db
    python3 -m scripts.run_austin_forward_event_history_backfill --write --confirm-production-writes
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.config import settings
from app.repositories.sqlite_activity_event_store import SQLiteActivityEventStore
from app.repositories.sqlite_client_contact_store import SQLiteClientContactStore
from app.repositories.sqlite_client_store import SQLiteClientStore
from app.repositories.sqlite_client_touchpoint_store import SQLiteClientTouchpointStore
from app.repositories.sqlite_crm_contact_store import SQLiteCrmContactStore
from app.repositories.sqlite_engagement_closeout_store import SQLiteEngagementCloseoutStore
from app.repositories.sqlite_engagement_participant_store import SQLiteEngagementParticipantStore
from app.repositories.sqlite_engagement_store import SQLiteEngagementStore
from app.repositories.sqlite_luma_event_store import SQLiteLumaEventStore
from app.services.activity_log_service import ActivityLogService
from app.services.austin_forward_event_history_backfill import (
    BackfillReport,
    RowOutcome,
    apply_backfill,
    compute_dry_run_report,
)
from app.services.austin_forward_reconciliation_cohort import TOTAL_COHORT_SIZE
from app.services.client_crm_service import ClientCrmService
from app.services.contact_engagement_signal_service import ContactEngagementSignalService


def _print_report(report: BackfillReport, write_mode: bool) -> None:
    c = report.counts
    mode = "WRITE MODE" if write_mode else "DRY RUN (zero writes)"
    print(f"=== Austin Forward Event History Backfill -- {mode} ===\n")
    print(f"frozen cohort:              {TOTAL_COHORT_SIZE}")
    print(f"Client:                     {report.client_id or '(would be created)'}")
    print(f"Engagement:                 {report.engagement_id or '(would be created)'}\n")

    verb = "linked" if write_mode else "would link"
    print(f"Contacts {verb}:             {c.linked if write_mode else c.would_link}")
    print(f"already linked:             {c.already_linked}")
    print(f"unresolved contact:         {c.unresolved_contact}")
    print(f"errors:                     {c.errors}\n")

    print("--- Unresolved / error rows only ---")
    for r in report.results:
        if r.outcome in (RowOutcome.UNRESOLVED_CONTACT, RowOutcome.ERROR):
            print(f"  {r.full_name}  {r.outcome.value}  {r.detail}")


async def _run(db_path: str, write_mode: bool) -> BackfillReport:
    client_store = SQLiteClientStore(db_path)
    client_contact_store = SQLiteClientContactStore(db_path)
    crm_contact_store = SQLiteCrmContactStore(db_path)
    engagement_store = SQLiteEngagementStore(db_path)
    engagement_closeout_store = SQLiteEngagementCloseoutStore(db_path)
    engagement_participant_store = SQLiteEngagementParticipantStore(db_path)
    luma_event_store = SQLiteLumaEventStore(db_path)
    client_touchpoint_store = SQLiteClientTouchpointStore(db_path)
    activity_store = SQLiteActivityEventStore(db_path)

    for store in (
        client_store,
        client_contact_store,
        crm_contact_store,
        engagement_store,
        engagement_closeout_store,
        engagement_participant_store,
        luma_event_store,
        client_touchpoint_store,
        activity_store,
    ):
        await store.connect()

    try:
        activity_log = ActivityLogService(store=activity_store)
        contact_engagement_signal_service = ContactEngagementSignalService(
            crm_contact_store=crm_contact_store, activity_log=activity_log
        )
        service = ClientCrmService(
            client_store=client_store,
            activity_log=activity_log,
            client_contact_store=client_contact_store,
            crm_contact_store=crm_contact_store,
            engagement_store=engagement_store,
            engagement_closeout_store=engagement_closeout_store,
            engagement_participant_store=engagement_participant_store,
            luma_event_store=luma_event_store,
            client_touchpoint_store=client_touchpoint_store,
            contact_engagement_signal_service=contact_engagement_signal_service,
        )
        if not write_mode:
            return await compute_dry_run_report(service)
        return await apply_backfill(service)
    finally:
        for store in (
            client_store,
            client_contact_store,
            crm_contact_store,
            engagement_store,
            engagement_closeout_store,
            engagement_participant_store,
            luma_event_store,
            client_touchpoint_store,
            activity_store,
        ):
            await store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Explicit no-op flag -- dry-run is already the default behavior.")
    parser.add_argument(
        "--write", action="store_true",
        help="Attempt REAL Client/Engagement/EngagementParticipant creation. Requires --confirm-production-writes too.",
    )
    parser.add_argument(
        "--confirm-production-writes", action="store_true",
        help="Required alongside --write. Confirms you intend this run to mutate production data.",
    )
    parser.add_argument("--database-path", default=None, help="Override the DB path (defaults to the app's own configured database_path).")
    args = parser.parse_args()

    if args.write and not args.confirm_production_writes:
        print("Refusing to run: --write requires --confirm-production-writes as well. Nothing was read or written.", file=sys.stderr)
        sys.exit(2)
    if args.confirm_production_writes and not args.write:
        print("Refusing to run: --confirm-production-writes has no effect without --write. Nothing was read or written.", file=sys.stderr)
        sys.exit(2)

    write_mode = args.write and args.confirm_production_writes
    db_path = args.database_path or settings.database_path

    if write_mode:
        print(f"Proceeding with WRITE MODE against the fixed, {TOTAL_COHORT_SIZE}-person frozen Austin Forward cohort.\n", file=sys.stderr)
    report = asyncio.run(_run(db_path, write_mode))
    _print_report(report, write_mode)


if __name__ == "__main__":
    main()
