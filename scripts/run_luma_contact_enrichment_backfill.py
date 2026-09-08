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

--exclude (backfill-only precaution, repeatable, comma-separable): skips
the given Contacts ENTIRELY for this one run -- see
run_luma_contact_enrichment_backfill's own docstring for exactly what
"entirely" means. Each value is a short ID PREFIX, matched against every
CrmContact's real `crm_contact_id` -- NEVER assume/reconstruct a full
UUID from a shortened display ID. Resolution FAILS CLOSED: a prefix that
matches zero or more than one Contact aborts the ENTIRE run (dry-run
included) before anything is read or written, rather than silently
proceeding with a partial/ambiguous exclusion list. Only the resolved
FULL Contact IDs are ever passed to the driver.

Usage (run as a module from the repo root, so `app.*` imports resolve):
    python3 -m scripts.run_luma_contact_enrichment_backfill
    python3 -m scripts.run_luma_contact_enrichment_backfill --dry-run
    python3 -m scripts.run_luma_contact_enrichment_backfill --write --confirm-production-writes
    python3 -m scripts.run_luma_contact_enrichment_backfill --database-path /app/data/campaigns.db --example-cap 25
    python3 -m scripts.run_luma_contact_enrichment_backfill --exclude 1176e159 --exclude 9dfe0207,f39ef8c6
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.config import settings
from app.repositories.crm_contact_store import CrmContactStore
from app.repositories.sqlite_crm_contact_store import SQLiteCrmContactStore
from app.repositories.sqlite_luma_event_store import SQLiteLumaEventStore
from app.repositories.sqlite_luma_registration_store import SQLiteLumaRegistrationStore
from app.services.luma_contact_enrichment_backfill import BackfillReport, run_luma_contact_enrichment_backfill


async def _resolve_exclusion_prefixes(contact_store: CrmContactStore, prefixes: list[str]) -> tuple[list[str], list[str]]:
    """Each prefix must match EXACTLY ONE CrmContact.crm_contact_id
    (startswith) -- fails closed (returns an error, never a guess) on
    zero or multiple matches. Never assumes/reconstructs a full UUID from
    a shortened display ID."""
    if not prefixes:
        return [], []
    all_contacts = await contact_store.list()
    resolved: list[str] = []
    errors: list[str] = []
    for prefix in prefixes:
        matches = sorted({c.crm_contact_id for c in all_contacts if c.crm_contact_id.startswith(prefix)})
        if len(matches) == 0:
            errors.append(f"{prefix!r} matched ZERO Contacts")
        elif len(matches) > 1:
            errors.append(f"{prefix!r} matched MULTIPLE Contacts ({len(matches)}): {matches}")
        else:
            resolved.append(matches[0])
    return resolved, errors


def _print_report(report: BackfillReport) -> None:
    mode = "DRY RUN (zero writes)" if report.dry_run else "WRITE MODE -- Contacts were saved"
    print(f"=== Luma Contact Enrichment Backfill -- {mode} ===\n")
    counts = report.counts
    for field_name in counts.__dataclass_fields__:
        print(f"  {field_name:42s} {getattr(counts, field_name)}")

    print(f"\n=== Excluded Contacts (backfill-only precaution, this run only) -- {len(report.excluded_contact_ids)} ===")
    for contact_id in report.excluded_contact_ids:
        print(f"  {contact_id}")
    if report.excluded_contact_ids_not_found:
        print(f"  NOTE: {len(report.excluded_contact_ids_not_found)} requested exclusion ID(s) had no registrations at all -- nothing to skip:")
        for contact_id in report.excluded_contact_ids_not_found:
            print(f"    {contact_id}")

    print(f"\n=== Ambiguous Contacts (unknown-recency conflict, capped) -- {len(report.ambiguous_contact_ids)} ===")
    for contact_id in report.ambiguous_contact_ids:
        print(f"  {contact_id}")

    print(f"\n=== Existing Website Flagged For Review (Company changed, website PRESERVED not cleared, capped) -- {len(report.website_review_needed_contact_ids)} ===")
    for contact_id in report.website_review_needed_contact_ids:
        print(f"  {contact_id}")

    print(f"\n=== Tier 1 Website Ambiguous Examples (capped) -- {len(report.tier1_ambiguous_examples)} ===")
    for example in report.tier1_ambiguous_examples:
        print(f"  {example}")

    print(f"\n=== Representative Proposed Changes (capped) -- {len(report.examples)} ===")
    header = (
        "crm_contact_id | old_company -> new_company (recency) | old_title -> new_title (recency) | "
        "old_website -> new (Tier 1 only, blank->filled) | tier2_candidate_fyi (never written) | flags"
    )
    print(header)
    for e in report.examples:
        company_recency = f"{e.company_recency_tier}/{e.company_recency_at}" if e.company_recency_tier else "-"
        title_recency = f"{e.title_recency_tier}/{e.title_recency_at}" if e.title_recency_tier else "-"
        flags = ", ".join(e.flags) if e.flags else "-"
        # "(unchanged)" is used instead of a bare None whenever this
        # field was NOT part of the round's changed_field_keys -- a None
        # here never means "cleared", since blank Luma values never erase
        # and an existing website is never auto-cleared either (V1).
        company_display = repr(e.new_company) if e.new_company is not None else "(unchanged)"
        title_display = repr(e.new_title) if e.new_title is not None else "(unchanged)"
        website_display = repr(e.new_website) if e.new_website is not None else "(unchanged)"
        print(
            f"  {e.crm_contact_id} | {e.old_company!r} -> {company_display} ({company_recency}) | "
            f"{e.old_title!r} -> {title_display} ({title_recency}) | "
            f"{e.old_website!r} -> {website_display} | fyi:{e.tier2_candidate_fyi!r} | {flags}"
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
        resolved_exclusions, errors = await _resolve_exclusion_prefixes(contact_store, args.exclude)
        if errors:
            print("Refusing to run: exclusion prefix resolution failed closed. Nothing was read or written beyond this lookup.", file=sys.stderr)
            for error in errors:
                print(f"  {error}", file=sys.stderr)
            sys.exit(2)
        if args.exclude:
            print(f"Resolved {len(args.exclude)} exclusion prefix(es) to {len(resolved_exclusions)} full Contact ID(s).", file=sys.stderr)

        return await run_luma_contact_enrichment_backfill(
            contact_store,
            registration_store,
            event_store,
            dry_run=args.dry_run,
            example_cap=args.example_cap,
            excluded_contact_ids=set(resolved_exclusions),
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
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="PREFIX",
        help="Backfill-only precaution: skip a Contact entirely for this run. Repeatable; each value may also be "
        "comma-separated. Short ID PREFIX, resolved against the real Contact store -- fails closed if a prefix "
        "matches zero or multiple Contacts.",
    )
    args = parser.parse_args()
    # Flatten repeatable + comma-separated values into one list of raw prefixes.
    args.exclude = [p.strip() for raw in args.exclude for p in raw.split(",") if p.strip()]

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
