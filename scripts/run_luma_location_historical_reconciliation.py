"""
Stage 6C (2026-09-14) -- historical Luma location reconciliation runner
(see app/services/luma_location_historical_reconciliation.py for the
actual driver, which reuses Stage 6B's
apply_luma_event_location_enrichment() verbatim).

SAFE BY DEFAULT: with no arguments, this is a DRY RUN against the fixed,
approved cohort in app/services/luma_location_historical_cohort.py.
Writes NOTHING (no Contact save, no Activity Log entry).

Real writes require BOTH --write AND --confirm-production-writes,
together (same two-gate convention as
scripts/run_contact_engagement_stage_backfill.py). UNLIKE that script,
there is NO target-id argument of any kind here -- the write-eligible
universe is always exactly the fixed FROZEN_COHORT; this command cannot
be pointed at any other Contact/registration, by design.

Each frozen row is re-evaluated LIVE at write time -- never a frozen
replay of an earlier dry-run manifest. A row whose approved fields
already got filled (by this driver's own earlier run, or by any other
means), or whose underlying data has drifted since the cohort was
approved, is reported as already-satisfied or drift respectively --
never silently skipped without a reason, never substituted with a
different registration/event.

Usage (run as a module from the repo root, so `app.*` imports resolve):
    python3 -m scripts.run_luma_location_historical_reconciliation
    python3 -m scripts.run_luma_location_historical_reconciliation --database-path /app/data/campaigns.db
    python3 -m scripts.run_luma_location_historical_reconciliation --write --confirm-production-writes
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.config import settings
from app.repositories.sqlite_activity_event_store import SQLiteActivityEventStore
from app.repositories.sqlite_crm_contact_store import SQLiteCrmContactStore
from app.repositories.sqlite_luma_event_store import SQLiteLumaEventStore
from app.repositories.sqlite_luma_registration_store import SQLiteLumaRegistrationStore
from app.services.activity_log_service import ActivityLogService
from app.services.luma_location_historical_cohort import FROZEN_COHORT
from app.services.luma_location_historical_reconciliation import (
    DryRunReport,
    WriteReport,
    apply_frozen_cohort_reconciliation,
    compute_dry_run_report,
)


def _print_dry_run_report(report: DryRunReport) -> None:
    c = report.counts
    print("=== Stage 6C -- Historical Luma Location Reconciliation -- DRY RUN (zero writes) ===\n")
    print(f"frozen rows:                       {c.frozen_rows}")
    print(f"eligible now:                      {c.eligible_now}")
    print(f"already satisfied (idempotent):    {c.already_satisfied}")
    print(f"drifted / skipped:                 {c.drifted_or_skipped}")
    print(f"Contacts that would change:        {c.contacts_that_would_change}")
    print(f"total fields that would change:    {c.total_fields_that_would_change}")
    print(f"  city:    {c.city}")
    print(f"  state:   {c.state}")
    print(f"  country: {c.country}\n")

    print("--- Drifted/skipped rows (structural IDs + reason only) ---")
    for row in report.drifted:
        print(f"  {row.crm_contact_id}  event={row.luma_event_id}  fields={list(row.fields)}  reason={row.outcome.value}")

    print("\n--- Eligible-now rows (would write) ---")
    for row in report.rows:
        if row.outcome.value == "would_fill":
            print(f"  {row.crm_contact_id}  event={row.luma_event_id}  fields={list(row.fields)}")


def _print_write_report(report: WriteReport) -> None:
    print("=== Stage 6C -- Historical Luma Location Reconciliation -- WRITE MODE ===\n")
    for result in report.results:
        print(f"  {result.crm_contact_id}  {result.outcome.value:32s}  {result.detail}")
    print("\n--- Summary ---")
    for outcome, n in sorted(report.summary().items()):
        print(f"  {outcome:32s} {n}")


async def _run_dry_run(db_path: str) -> DryRunReport:
    contact_store = SQLiteCrmContactStore(db_path)
    registration_store = SQLiteLumaRegistrationStore(db_path)
    event_store = SQLiteLumaEventStore(db_path)
    await contact_store.connect()
    await registration_store.connect()
    await event_store.connect()
    try:
        return await compute_dry_run_report(contact_store, registration_store, event_store)
    finally:
        await contact_store.close()
        await registration_store.close()
        await event_store.close()


async def _run_write(db_path: str) -> WriteReport:
    contact_store = SQLiteCrmContactStore(db_path)
    registration_store = SQLiteLumaRegistrationStore(db_path)
    event_store = SQLiteLumaEventStore(db_path)
    activity_event_store = SQLiteActivityEventStore(db_path)
    await contact_store.connect()
    await registration_store.connect()
    await event_store.connect()
    await activity_event_store.connect()
    activity_log = ActivityLogService(store=activity_event_store)
    try:
        return await apply_frozen_cohort_reconciliation(contact_store, registration_store, event_store, activity_log)
    finally:
        await contact_store.close()
        await registration_store.close()
        await event_store.close()
        await activity_event_store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Explicit no-op flag -- dry-run is already the default behavior.")
    parser.add_argument(
        "--write", action="store_true",
        help="Attempt REAL Contact location writes against the fixed FROZEN_COHORT. Requires --confirm-production-writes too.",
    )
    parser.add_argument(
        "--confirm-production-writes", action="store_true",
        help="Required alongside --write. Confirms you intend this run to mutate production Contacts.",
    )
    parser.add_argument("--database-path", default=None, help="Override the DB path (defaults to the app's own configured database_path).")
    args = parser.parse_args()

    if args.write and not args.confirm_production_writes:
        print(
            "Refusing to run: --write requires --confirm-production-writes as well. Nothing was read or written.",
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

    print(f"Proceeding with WRITE MODE against the fixed, {len(FROZEN_COHORT)}-row frozen cohort.\n", file=sys.stderr)
    write_report = asyncio.run(_run_write(db_path))
    _print_write_report(write_report)
    if write_report.summary().get("error"):
        sys.exit(1)


if __name__ == "__main__":
    main()
