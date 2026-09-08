"""
Historical reconciliation runner for the four investor-related Luma
custom fields (investor_type, check_size_personal, deploying_capital,
investment_industry) affected by the stale question_type mapping bug --
see app/services/luma_investor_fields_reconciliation.py for the actual
driver; this script only wires it to the application's REAL configured
SQLite database and prints the report.

SAFE BY DEFAULT: with no arguments, this is a DRY RUN -- it reads
CrmContact/LumaRegistration, computes what WOULD change, and writes
NOTHING. Makes ZERO Luma API calls. Real writes require BOTH --write AND
--confirm-production-writes together, same fail-closed dual-confirmation
convention as scripts/run_luma_contact_enrichment_backfill.py -- passing
only one refuses to run at all.

Never prints environment variables, secrets, or raw Luma payloads --
only aggregate counts and per-Contact before/after field values (the
whole point of a review sample).

Usage (run as a module from the repo root, so `app.*` imports resolve):
    python3 -m scripts.run_luma_investor_fields_reconciliation
    python3 -m scripts.run_luma_investor_fields_reconciliation --database-path /app/data/campaigns.db --example-cap 200
    python3 -m scripts.run_luma_investor_fields_reconciliation --write --confirm-production-writes
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.config import settings
from app.repositories.activity_event_store import MemoryActivityEventStore
from app.repositories.sqlite_crm_contact_store import SQLiteCrmContactStore
from app.repositories.sqlite_crm_custom_field_store import SQLiteCrmCustomFieldStore
from app.repositories.sqlite_luma_event_store import SQLiteLumaEventStore
from app.repositories.sqlite_luma_question_mapping_store import SQLiteLumaQuestionMappingStore
from app.repositories.sqlite_luma_registration_store import SQLiteLumaRegistrationStore
from app.services.activity_log_service import ActivityLogService
from app.services.crm_service import CrmService
from app.services.luma_investor_fields_reconciliation import ReconciliationReport, run_investor_fields_reconciliation
from app.services.luma_sync_service import LumaSyncService


def _print_report(report: ReconciliationReport) -> None:
    mode = "DRY RUN (zero writes)" if report.dry_run else "WRITE MODE -- Contacts were saved"
    print(f"=== Luma Investor Fields Reconciliation -- {mode} ===\n")
    counts = report.counts
    for field_name in counts.__dataclass_fields__:
        value = getattr(counts, field_name)
        if field_name == "changes_by_field":
            print(f"  {field_name:32s}")
            for key, n in sorted(value.items()):
                print(f"    {key:30s} {n}")
        else:
            print(f"  {field_name:32s} {value}")

    print(f"\n=== Contacts that would change ({len(report.examples)} shown) ===")
    header = "crm_contact_id | field: old -> new (one line per changed field)"
    print(header)
    for example in report.examples:
        print(f"  {example.crm_contact_id}")
        for change in example.changes:
            print(f"    {change.field_key}: {change.old_value!r} -> {change.new_value!r}")


async def _run(args: argparse.Namespace) -> ReconciliationReport:
    db_path = args.database_path or settings.database_path
    contact_store = SQLiteCrmContactStore(db_path)
    registration_store = SQLiteLumaRegistrationStore(db_path)
    event_store = SQLiteLumaEventStore(db_path)
    mapping_store = SQLiteLumaQuestionMappingStore(db_path)
    custom_field_store = SQLiteCrmCustomFieldStore(db_path)
    await contact_store.connect()
    await registration_store.connect()
    await event_store.connect()
    await mapping_store.connect()
    await custom_field_store.connect()
    try:
        crm_service = CrmService(custom_field_store=custom_field_store, contact_store=contact_store)
        # A throwaway, in-memory-only Activity Log -- this reconciliation
        # driver never calls it (see the module's own docstring), but
        # LumaSyncService's constructor requires one; never persisted,
        # never touches the real Activity Log store.
        activity_log = ActivityLogService(MemoryActivityEventStore())
        luma_sync_service = LumaSyncService(
            crm_service=crm_service,
            event_store=event_store,
            registration_store=registration_store,
            mapping_store=mapping_store,
            activity_log=activity_log,
        )
        return await run_investor_fields_reconciliation(
            luma_sync_service, registration_store, dry_run=args.dry_run, example_cap=args.example_cap
        )
    finally:
        await contact_store.close()
        await registration_store.close()
        await event_store.close()
        await mapping_store.close()
        await custom_field_store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Explicit no-op flag -- dry-run is already the default behavior.")
    parser.add_argument(
        "--write",
        action="store_true",
        help="Attempt REAL Contact writes. Requires --confirm-production-writes too, or this refuses to run at all.",
    )
    parser.add_argument(
        "--confirm-production-writes",
        action="store_true",
        help="Required alongside --write. Confirms you intend this run to modify production Contacts.",
    )
    parser.add_argument("--database-path", default=None, help="Override the DB path (defaults to the app's own configured database_path).")
    parser.add_argument("--example-cap", type=int, default=200)
    args = parser.parse_args()

    if args.write and not args.confirm_production_writes:
        print(
            "Refusing to run: --write requires --confirm-production-writes as well. "
            "Nothing was read or written. Rerun with BOTH flags to actually write, or omit --write for a dry run.",
            file=sys.stderr,
        )
        sys.exit(2)
    if args.confirm_production_writes and not args.write:
        print("Refusing to run: --confirm-production-writes has no effect without --write. Nothing was read or written.", file=sys.stderr)
        sys.exit(2)

    args.dry_run = not (args.write and args.confirm_production_writes)

    report = asyncio.run(_run(args))
    _print_report(report)


if __name__ == "__main__":
    main()
