"""
Historical Luma self-report Company/Job Title enrichment -- backfill
runner (see app/services/luma_contact_enrichment_backfill.py for the
actual driver; this script only wires it to the application's REAL
configured SQLite database and prints the report).

SAFE BY DEFAULT: with no arguments, this is a DRY RUN -- it reads
CrmContact/LumaRegistration/LumaEvent, computes what WOULD change, and
writes NOTHING (no Contact save, no LumaRegistration mutation, no
Activity Log entry -- the backfill driver itself never touches Activity
Log at all; only the live webhook path does). Real writes require BOTH
`--write` AND `--confirm-production-writes` together -- passing only one
REFUSES to run at all (exits nonzero without touching anything), rather
than silently falling back to a dry run. This is deliberate: a single
`--no-dry-run`-style boolean is too easy to flip by accident; two
independently-named, unambiguous flags are not.

Never prints environment variables, secrets, or raw Luma payloads --
only the aggregate counts and a small, capped set of examples (IDs and
the actual before/after field values only).

Usage (run as a module from the repo root, so `app.*` imports resolve):
    python3 -m scripts.run_luma_contact_enrichment_backfill
    python3 -m scripts.run_luma_contact_enrichment_backfill --dry-run
    python3 -m scripts.run_luma_contact_enrichment_backfill --write --confirm-production-writes
    python3 -m scripts.run_luma_contact_enrichment_backfill --database-path /app/data/campaigns.db --example-cap 25
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.config import settings
from app.repositories.sqlite_crm_contact_store import SQLiteCrmContactStore
from app.repositories.sqlite_luma_event_store import SQLiteLumaEventStore
from app.repositories.sqlite_luma_registration_store import SQLiteLumaRegistrationStore
from app.services.luma_contact_enrichment_backfill import BackfillReport, run_luma_contact_enrichment_backfill


def _print_report(report: BackfillReport) -> None:
    mode = "DRY RUN (zero writes)" if report.dry_run else "WRITE MODE -- Contacts were saved"
    print(f"=== Luma Contact Enrichment Backfill -- {mode} ===\n")
    counts = report.counts
    for field_name in counts.__dataclass_fields__:
        print(f"  {field_name:42s} {getattr(counts, field_name)}")

    print(f"\n=== Ambiguous Contacts (unknown-recency conflict, capped) -- {len(report.ambiguous_contact_ids)} ===")
    for contact_id in report.ambiguous_contact_ids:
        print(f"  {contact_id}")

    print(f"\n=== Tier 1 Website Ambiguous Examples (capped) -- {len(report.tier1_ambiguous_examples)} ===")
    for example in report.tier1_ambiguous_examples:
        print(f"  {example}")

    print(f"\n=== Representative Proposed Changes (capped) -- {len(report.examples)} ===")
    header = (
        "crm_contact_id | old_company -> new_company (recency) | old_title -> new_title (recency) | "
        "old_website -> new/candidate | flags"
    )
    print(header)
    for e in report.examples:
        company_recency = f"{e.company_recency_tier}/{e.company_recency_at}" if e.company_recency_tier else "-"
        title_recency = f"{e.title_recency_tier}/{e.title_recency_at}" if e.title_recency_tier else "-"
        flags = ", ".join(e.flags) if e.flags else "-"
        print(
            f"  {e.crm_contact_id} | {e.old_company!r} -> {e.new_company!r} ({company_recency}) | "
            f"{e.old_title!r} -> {e.new_title!r} ({title_recency}) | "
            f"{e.old_website!r} -> {e.new_website_or_tier2_candidate!r} | {flags}"
        )


async def _run(args: argparse.Namespace) -> BackfillReport:
    db_path = args.database_path or settings.database_path
    contact_store = SQLiteCrmContactStore(db_path)
    registration_store = SQLiteLumaRegistrationStore(db_path)
    event_store = SQLiteLumaEventStore(db_path)
    await contact_store.connect()
    await registration_store.connect()
    await event_store.connect()
    try:
        return await run_luma_contact_enrichment_backfill(
            contact_store, registration_store, event_store, dry_run=args.dry_run, example_cap=args.example_cap
        )
    finally:
        await contact_store.close()
        await registration_store.close()
        await event_store.close()


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
    parser.add_argument("--example-cap", type=int, default=20)
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
