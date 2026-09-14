"""
Austin Forward -- Sept 10, 2026 Contact Reconciliation (Stage AF-2) runner
(see app/services/austin_forward_reconciliation.py for the actual driver,
and app/services/austin_forward_reconciliation_cohort.py for the frozen,
approved 166-person cohort this command is permanently scoped to).

SAFE BY DEFAULT: with no arguments, this is a DRY RUN. Writes NOTHING (no
Contact save/create, no List membership, no Activity Log entry).

Real writes require BOTH --write AND --confirm-production-writes, together
(same two-gate convention as every other operator write CLI in this repo).
There is NO target-id argument of any kind -- the write-eligible universe
is always exactly the frozen cohort's EXISTING_CONTACTS + NEW_CONTACTS;
this command cannot be pointed at any other Contact.

Usage (run as a module from the repo root, so `app.*` imports resolve):
    python3 -m scripts.run_austin_forward_reconciliation
    python3 -m scripts.run_austin_forward_reconciliation --database-path /app/data/campaigns.db
    python3 -m scripts.run_austin_forward_reconciliation --write --confirm-production-writes
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.config import settings
from app.repositories.sqlite_activity_event_store import SQLiteActivityEventStore
from app.repositories.sqlite_crm_contact_list_member_store import SQLiteCrmContactListMemberStore
from app.repositories.sqlite_crm_contact_store import SQLiteCrmContactStore
from app.services.activity_log_service import ActivityLogService
from app.services.austin_forward_reconciliation import (
    DryRunReport,
    ExistingRowOutcome,
    NewRowOutcome,
    apply_frozen_cohort_reconciliation,
    compute_dry_run_report,
)
from app.services.austin_forward_reconciliation_cohort import EXISTING_CONTACTS_SIZE, NEW_CONTACTS_SIZE, TOTAL_COHORT_SIZE


def _print_report(report: DryRunReport, write_mode: bool) -> None:
    c = report.counts
    mode = "WRITE MODE" if write_mode else "DRY RUN (zero writes)"
    print(f"=== Austin Forward Reconciliation -- {mode} ===\n")
    print(f"frozen cohort:                        {TOTAL_COHORT_SIZE} ({EXISTING_CONTACTS_SIZE} existing + {NEW_CONTACTS_SIZE} new)\n")

    verb = "updated" if write_mode else "would update"
    print(f"existing Contacts {verb}:              {c.existing_would_update}")
    print(f"existing Contacts already satisfied:  {c.existing_already_satisfied}")
    print(f"existing Contacts drifted/skipped:    {c.existing_drift}")
    verb2 = "created" if write_mode else "would create"
    print(f"new Contacts {verb2}:                  {c.new_would_create}")
    print(f"new Contacts already exist now:       {c.new_drift_already_exists}\n")

    print(f"Dinners Attended additions:           {c.dinners_attended_additions}")
    print(f"Dinner Subscriptions additions:       {c.dinner_subscriptions_additions}")
    print(f"List membership additions:            {c.list_membership_additions}")
    print(f"City fills:                           {c.city_fills}")
    print(f"State fills:                          {c.state_fills}")
    print(f"Country fills:                        {c.country_fills}")
    print(f"Role additions total:                 {c.role_additions_total}")
    for role, n in sorted(c.role_additions_by_role.items()):
        print(f"  {role:20s} {n}")
    print(f"Notes -- blank inserts:               {c.notes_blank_inserts}")
    print(f"Notes -- appended to existing:        {c.notes_appends}")
    print(f"New Contacts created with Notes:      {c.new_contacts_with_notes}\n")

    if report.existing_results:
        print("--- Existing Contact rows (structural ID + outcome only) ---")
        for r in report.existing_results:
            if r.outcome not in (ExistingRowOutcome.ALREADY_SATISFIED,):
                print(f"  {r.crm_contact_id}  {r.outcome.value:24s}")

    if report.new_results:
        print("\n--- New Contact rows (outcome only) ---")
        for r in report.new_results:
            if r.outcome == NewRowOutcome.DRIFT_ALREADY_EXISTS:
                print(f"  {r.outcome.value:24s}  (already resolved -- skipped)")


async def _run(db_path: str, write_mode: bool) -> DryRunReport:
    contact_store = SQLiteCrmContactStore(db_path)
    list_member_store = SQLiteCrmContactListMemberStore(db_path)
    await contact_store.connect()
    await list_member_store.connect()
    try:
        if not write_mode:
            return await compute_dry_run_report(contact_store, list_member_store)
        activity_store = SQLiteActivityEventStore(db_path)
        await activity_store.connect()
        activity_log = ActivityLogService(store=activity_store)
        try:
            return await apply_frozen_cohort_reconciliation(contact_store, list_member_store, activity_log)
        finally:
            await activity_store.close()
    finally:
        await contact_store.close()
        await list_member_store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Explicit no-op flag -- dry-run is already the default behavior.")
    parser.add_argument(
        "--write", action="store_true",
        help="Attempt REAL Contact writes/creates against the fixed frozen cohort. Requires --confirm-production-writes too.",
    )
    parser.add_argument(
        "--confirm-production-writes", action="store_true",
        help="Required alongside --write. Confirms you intend this run to mutate production Contacts.",
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
        print(f"Proceeding with WRITE MODE against the fixed, {TOTAL_COHORT_SIZE}-person frozen cohort.\n", file=sys.stderr)
    report = asyncio.run(_run(db_path, write_mode))
    _print_report(report, write_mode)


if __name__ == "__main__":
    main()
