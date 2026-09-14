"""
Notes / Personal Notes Consolidation (Stage NP-2) runner (see
app/services/notes_personal_notes_merge.py for the actual driver, and
app/services/notes_personal_notes_merge_cohort.py for the frozen, approved
9-Contact cohort this command is permanently scoped to).

SAFE BY DEFAULT: with no arguments, this is a DRY RUN. Writes NOTHING (no
Contact save, no Activity Log entry). Never modifies or clears
`personal_notes` -- that field is left completely untouched by this
command, on every run, always; it remains the temporary rollback copy
during this migration's transition period.

Real writes require BOTH --write AND --confirm-production-writes,
together. There is NO target-id argument -- the write-eligible universe is
always exactly the frozen cohort's 9 rows.

Usage (run as a module from the repo root, so `app.*` imports resolve):
    python3 -m scripts.run_notes_personal_notes_merge
    python3 -m scripts.run_notes_personal_notes_merge --database-path /app/data/campaigns.db
    python3 -m scripts.run_notes_personal_notes_merge --write --confirm-production-writes
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.config import settings
from app.repositories.sqlite_activity_event_store import SQLiteActivityEventStore
from app.repositories.sqlite_crm_contact_store import SQLiteCrmContactStore
from app.services.activity_log_service import ActivityLogService
from app.services.notes_personal_notes_merge import DryRunReport, RowOutcome, apply_frozen_cohort_merge, compute_dry_run_report
from app.services.notes_personal_notes_merge_cohort import COHORT_SIZE


def _print_report(report: DryRunReport, write_mode: bool) -> None:
    c = report.counts
    mode = "WRITE MODE" if write_mode else "DRY RUN (zero writes)"
    print(f"=== Notes / Personal Notes Merge -- {mode} ===\n")
    print(f"frozen cohort:              {COHORT_SIZE}")
    verb = "merged" if write_mode else "would merge"
    print(f"Contacts {verb}:            {c.would_merge}")
    print(f"  copy (Notes was blank):   {c.copy_merges}")
    print(f"  append (Notes populated): {c.append_merges}")
    print(f"already satisfied:         {c.already_satisfied}")
    print(f"drifted / skipped:         {c.drift}\n")

    print("--- Rows (structural ID + outcome only) ---")
    for r in report.results:
        if r.outcome != RowOutcome.ALREADY_SATISFIED:
            print(f"  {r.crm_contact_id}  {r.outcome.value:32s}  {r.merge_type or ''}")


async def _run(db_path: str, write_mode: bool) -> DryRunReport:
    contact_store = SQLiteCrmContactStore(db_path)
    await contact_store.connect()
    try:
        if not write_mode:
            return await compute_dry_run_report(contact_store)
        activity_store = SQLiteActivityEventStore(db_path)
        await activity_store.connect()
        activity_log = ActivityLogService(store=activity_store)
        try:
            return await apply_frozen_cohort_merge(contact_store, activity_log)
        finally:
            await activity_store.close()
    finally:
        await contact_store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Explicit no-op flag -- dry-run is already the default behavior.")
    parser.add_argument(
        "--write", action="store_true",
        help="Attempt REAL Notes merges against the fixed frozen cohort. Requires --confirm-production-writes too.",
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
        print(f"Proceeding with WRITE MODE against the fixed, {COHORT_SIZE}-Contact frozen cohort.\n", file=sys.stderr)
    report = asyncio.run(_run(db_path, write_mode))
    _print_report(report, write_mode)


if __name__ == "__main__":
    main()
