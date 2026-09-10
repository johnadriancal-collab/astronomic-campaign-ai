"""
Client CRM Stage 1H-C -- historical Luma registration -> EngagementParticipant
backfill runner (see app/services/luma_engagement_participant_backfill.py
for the actual driver, which reuses Stage 1H-B's
LumaEngagementParticipantSyncService verbatim; this script only wires it
to the application's REAL configured SQLite database, targets exactly ONE
explicitly-supplied Engagement, and prints the report).

SAFE BY DEFAULT: with no arguments beyond --engagement-id, this is a DRY
RUN -- it reads the target Engagement, its current EngagementParticipants,
its linked Luma event's registrations, and the CrmContacts they reference,
computes what WOULD happen via the real, unmodified sync service run
against a throwaway in-memory replica, and writes NOTHING to production
(no EngagementParticipant create/save, no Activity Log entry). Real writes
require BOTH `--write` AND `--confirm-production-writes` together --
passing only one REFUSES to run at all (exits nonzero without touching
anything), same two-gate convention as
scripts/run_luma_contact_enrichment_backfill.py: a single `--no-dry-run`-
style boolean is too easy to flip by accident.

--engagement-id is ALWAYS required -- there is no "process every linked
Engagement" mode. An Engagement that doesn't exist, or exists but has no
linked Luma event, is rejected before anything else is read or written.
An archived/cancelled (but linked) Engagement is not rejected up front --
every one of its registrations simply reports the matching skip outcome.

Never prints raw registration payloads, Contact PII, or which specific
Contact a duplicate-registration group belongs to -- only aggregate
counts, ids already known to the operator (engagement_id, luma_event_id,
luma_guest_id on an error line), and duplicate-group SIZES.

Usage (run as a module from the repo root, so `app.*` imports resolve):
    python3 -m scripts.run_luma_engagement_participant_backfill --engagement-id <id>
    python3 -m scripts.run_luma_engagement_participant_backfill --engagement-id <id> --dry-run
    python3 -m scripts.run_luma_engagement_participant_backfill --engagement-id <id> --write --confirm-production-writes
    python3 -m scripts.run_luma_engagement_participant_backfill --engagement-id <id> --database-path /app/data/campaigns.db
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.config import settings
from app.repositories.sqlite_activity_event_store import SQLiteActivityEventStore
from app.repositories.sqlite_crm_contact_store import SQLiteCrmContactStore
from app.repositories.sqlite_engagement_participant_store import SQLiteEngagementParticipantStore
from app.repositories.sqlite_engagement_store import SQLiteEngagementStore
from app.repositories.sqlite_luma_registration_store import SQLiteLumaRegistrationStore
from app.services.activity_log_service import ActivityLogService
from app.services.luma_engagement_participant_backfill import (
    ParticipantBackfillInvalidTarget,
    ParticipantBackfillReport,
    run_luma_engagement_participant_backfill,
)


def _print_report(report: ParticipantBackfillReport) -> None:
    mode = "DRY RUN (zero writes)" if report.dry_run else "WRITE MODE -- EngagementParticipants were created/updated"
    print(f"=== Luma Engagement Participant Backfill -- {mode} ===\n")
    print(f"engagement_id: {report.engagement_id}")
    print(f"luma_event_id: {report.luma_event_id}\n")

    counts = report.counts
    for field_name in counts.__dataclass_fields__:
        print(f"  {field_name:42s} {getattr(counts, field_name)}")

    print(f"\nduplicate-registration contact groups (same Contact, >1 registration): {len(report.duplicate_contact_group_sizes)}")
    if report.duplicate_contact_group_sizes:
        print(f"  registration counts per such contact (no identities shown): {report.duplicate_contact_group_sizes}")

    if report.errors:
        print(f"\n=== ERRORS -- {len(report.errors)} registration(s) failed to process, everything else still ran ===")
        for error in report.errors:
            print(f"  {error}")
    else:
        print("\nno per-registration errors.")

    if counts.errors:
        print(f"\nRESULT: PARTIAL -- {counts.errors} error(s) occurred. Review before re-running or escalating.")
    else:
        print("\nRESULT: SUCCESS -- every registration produced a defined outcome, zero errors.")


async def _run(args: argparse.Namespace) -> ParticipantBackfillReport:
    db_path = args.database_path or settings.database_path
    engagement_store = SQLiteEngagementStore(db_path)
    engagement_participant_store = SQLiteEngagementParticipantStore(db_path)
    crm_contact_store = SQLiteCrmContactStore(db_path)
    luma_registration_store = SQLiteLumaRegistrationStore(db_path)
    activity_event_store = SQLiteActivityEventStore(db_path)
    await engagement_store.connect()
    await engagement_participant_store.connect()
    await crm_contact_store.connect()
    await luma_registration_store.connect()
    await activity_event_store.connect()
    activity_log = ActivityLogService(activity_event_store)
    try:
        return await run_luma_engagement_participant_backfill(
            engagement_store,
            engagement_participant_store,
            crm_contact_store,
            luma_registration_store,
            activity_log,
            engagement_id=args.engagement_id,
            dry_run=args.dry_run,
        )
    finally:
        await engagement_store.close()
        await engagement_participant_store.close()
        await crm_contact_store.close()
        await luma_registration_store.close()
        await activity_event_store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--engagement-id", required=True, help="The ONE Engagement to backfill. Must exist and have a non-null luma_event_id.")
    parser.add_argument("--dry-run", action="store_true", help="Explicit no-op flag -- dry-run is already the default behavior.")
    parser.add_argument(
        "--write",
        action="store_true",
        help="Attempt REAL EngagementParticipant writes. Requires --confirm-production-writes too, or this refuses to run at all.",
    )
    parser.add_argument(
        "--confirm-production-writes",
        action="store_true",
        help="Required alongside --write. Confirms you intend this run to create/update production EngagementParticipants.",
    )
    parser.add_argument("--database-path", default=None, help="Override the DB path (defaults to the app's own configured database_path).")
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

    try:
        report = asyncio.run(_run(args))
    except ParticipantBackfillInvalidTarget as e:
        print(f"Refusing to run: {e}", file=sys.stderr)
        sys.exit(2)

    _print_report(report)
    if report.counts.errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
