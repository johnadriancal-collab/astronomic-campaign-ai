"""
One-time, idempotent data migration for the Client CRM Engagement taxonomy
correction (Stage 1E.1, 2026-09-08).

Stage 1E shipped `engagement_type` values `investor_dinner`/`customer_dinner`
plus a separate `dinner_program` field (`supernova`/`galaxy`/`aurora`/`other`)
naming Astronomic's now-retired internal dinner-program brands. Stage 1E.1
collapses `engagement_type` to `dinner`/`sponsorship`/`other` and replaces
`dinner_program` with `dinner_type` (`investor_dinner`/`fireside_dinner`/
`bizdev_dinner`), naming the underlying dinner kind directly.

This is a genuine hard cutover: the OLD Engagement model has no `dinner`
member in its EngagementType enum, and the NEW Engagement model has no
`investor_dinner`/`customer_dinner` members -- neither model can read data
shaped for the other. There is no safe way to time "migrate the data" and
"deploy the new model" as two separate steps in either order; whichever
runs second would find data it can't read. This module is therefore called
synchronously from SQLiteEngagementStore.connect(), BEFORE the app finishes
starting up and begins serving requests -- closing that window entirely
within one deploy, rather than attempting a staged deploy (which cannot
actually be made safe here -- see this stage's own STOP report).

WHAT IS TRUSTED TO MIGRATE, AND WHY (tightened after production review):
Stage 1E's own DinnerProgram was an independently-valued field with no
schema-enforced correspondence to engagement_type -- the fact that the old
Pydantic model technically PERMITTED e.g. "customer_dinner" + "galaxy" is
not evidence that any such row, if one ever existed, represents a real,
coherent historical event. This migration does not assume a schema-
permitted combination is a legitimate one merely because it would validate.

Only `engagement_type == "investor_dinner"` is trusted -- it is the exact
engagement_type of the one confirmed real production row (Engagement
5f89a109-3192-40a2-97d4-040828fc52b1). `customer_dinner` is refused
outright (fails closed), regardless of its `dinner_program`, since no real
example of it is known and its historical meaning is unconfirmed.

The `dinner_program` -> `dinner_type` rename itself IS explicitly and
unconditionally justified -- Astronomic confirmed directly (not inferred
from old code comments) that these are pure retired-label correspondences,
independent of engagement_type: Supernova was the retired name for an
Investor Dinner, Galaxy for a Fireside Dinner, Aurora for a BizDev Dinner.
A missing `dinner_program` (or the old `"other"` value, which itself named
no specific kind) carries no positive claim to lose by becoming `None` --
that is a preserved absence, not a guess. Any OTHER `dinner_program` value
(unrecognized/garbled) has no justified mapping and fails closed rather
than being silently dropped to None, since a value existing there IS a
positive claim we can't safely discard without understanding it.

Deliberately operates on raw JSON dicts (`json.loads`/`json.dumps`), never
on the `Engagement` Pydantic model, for exactly this reason: it must be
able to read OLD-shaped rows that the current (new) Engagement model
cannot validate at all.

Idempotent and safe to run on every boot forever: a row is only ever
touched if its own `engagement_type` is literally one of the two retired
values below. Once every row has been migrated once, every future boot's
migration pass is a single cheap SELECT that matches nothing and writes
nothing. This is not a permanent compatibility shim -- the Engagement
model itself is never loosened to accept old values again, and no API
caller is ever able to observe old-shaped data -- it is a one-time data
correction that happens to be re-checked defensively on every boot.

Narrowly scoped: touches ONLY the `engagements` table, via whatever
connection SQLiteEngagementStore already owns. Never opens, queries, or
writes any other table -- Campaign Manager, mail, Contacts, ClientContact,
and Luma data are structurally unreachable from this module.

FAIL-CLOSED BY DESIGN: any row that doesn't match a justified rule below
raises UnjustifiedLegacyEngagementError, which propagates uncaught out of
SQLiteEngagementStore.connect() and out of the app's lifespan startup --
this deliberately aborts the app before it serves a single request, rather
than booting successfully with a row this migration wasn't confident
enough to touch. See this stage's own STOP report for confirmation that
nothing in the startup path catches this exception.
"""

import json

import aiosqlite
from loguru import logger

from app.models.client_crm import Engagement
from app.repositories.sqlite_txn import sqlite_write

# The two Stage 1E EngagementType values that no longer exist. Both are
# recognized as "this row needs migrating" -- but only one is trusted
# enough to actually migrate; see _JUSTIFIED_LEGACY_ENGAGEMENT_TYPE below.
_RETIRED_DINNER_ENGAGEMENT_TYPES = frozenset({"investor_dinner", "customer_dinner"})

# Only this old engagement_type is trusted to migrate -- see this module's
# own docstring for why "customer_dinner" is refused rather than guessed.
_JUSTIFIED_LEGACY_ENGAGEMENT_TYPE = "investor_dinner"

# Direct, explicitly-confirmed retired-label renames (Stage 1E.1
# correction) -- a pure terminology correspondence, independent of
# engagement_type. `"other"` is deliberately NOT a key here: it named no
# specific kind even under the old taxonomy, so it's handled as a
# preserved absence (-> None), not a mapped value.
_LEGACY_DINNER_PROGRAM_TO_DINNER_TYPE = {
    "supernova": "investor_dinner",
    "galaxy": "fireside_dinner",
    "aurora": "bizdev_dinner",
}

# dinner_program values that carry no positive claim to lose -- these
# become dinner_type=None (a preserved absence), never treated as a guess.
_NO_CLAIM_DINNER_PROGRAM_VALUES = frozenset({None, "other"})


class UnjustifiedLegacyEngagementError(Exception):
    """Raised when a Stage-1E-shaped Engagement row can't be safely
    migrated because its (engagement_type, dinner_program) combination
    isn't one this migration has explicit, textual justification to
    trust. Raising here (rather than guessing) is deliberate: it aborts
    app startup before any request is served, surfacing the row for
    manual/human review instead of silently serving broken or
    incorrectly-guessed data."""


def _migrate_row_dict(data: dict) -> dict | None:
    """Returns a corrected copy of `data` if it's Stage-1E-shaped AND
    justified to migrate, or None if this row needs no migration at all
    (already Stage 1E.1-shaped, or some unrelated future shape). Raises
    UnjustifiedLegacyEngagementError if the row IS Stage-1E-shaped but its
    specific combination isn't one this migration trusts -- see this
    module's own docstring."""
    engagement_type = data.get("engagement_type")
    if engagement_type not in _RETIRED_DINNER_ENGAGEMENT_TYPES:
        return None  # not Stage-1E dinner-shaped; nothing to migrate

    engagement_id = data.get("engagement_id", "<unknown>")

    if engagement_type != _JUSTIFIED_LEGACY_ENGAGEMENT_TYPE:
        raise UnjustifiedLegacyEngagementError(
            f"Engagement {engagement_id}: engagement_type={engagement_type!r} has no confirmed, "
            "justified migration mapping (only 'investor_dinner' is trusted -- see "
            "engagement_taxonomy_migration.py's own module docstring). Refusing to guess."
        )

    dinner_program = data.get("dinner_program")
    if dinner_program in _NO_CLAIM_DINNER_PROGRAM_VALUES:
        dinner_type = None
    elif dinner_program in _LEGACY_DINNER_PROGRAM_TO_DINNER_TYPE:
        dinner_type = _LEGACY_DINNER_PROGRAM_TO_DINNER_TYPE[dinner_program]
    else:
        raise UnjustifiedLegacyEngagementError(
            f"Engagement {engagement_id}: dinner_program={dinner_program!r} is not a recognized "
            "retired program name -- refusing to guess a dinner_type."
        )

    corrected = dict(data)
    corrected["engagement_type"] = "dinner"
    corrected.pop("dinner_program", None)
    corrected["dinner_type"] = dinner_type
    return corrected


async def migrate_legacy_dinner_taxonomy_rows(conn: aiosqlite.Connection) -> int:
    """Scans every row in `engagements` and rewrites any justified
    Stage-1E-shaped row's `data` blob in place to Stage 1E.1's shape.
    Every field other than `engagement_type`/`dinner_program`/
    `dinner_type` is preserved exactly as it was -- this never touches
    `engagement_id`, `client_id`, `title`, `engagement_date`, `location`,
    `status`, `owner`, `fee`, `contract_status`/`contract_url`/
    `signed_date`/`payment_status`, `luma_event_id`, `created_at`,
    `updated_at`, or `archived`. The `data` column is the only column
    written -- `client_id`/`created_at`/`updated_at` SQL columns are left
    untouched since none of them change.

    Returns the number of rows migrated (0 on a clean/already-migrated
    database, including every re-run after the first).

    Fails loudly and writes nothing for ANY row, migrated or not, the
    moment an unjustified row is found (see _migrate_row_dict) -- a
    partial migration that silently skips the row it couldn't trust
    would still boot the app with that row broken, defeating the entire
    point of migrating before startup completes."""
    cursor = await conn.execute("SELECT engagement_id, data FROM engagements")
    rows = await cursor.fetchall()
    await cursor.close()

    to_migrate: list[tuple[str, str]] = []
    for row in rows:
        corrected = _migrate_row_dict(json.loads(row["data"]))
        if corrected is None:
            continue
        # Fail loudly, before writing anything, if the correction somehow
        # doesn't produce a valid Engagement -- never write unvalidated data.
        Engagement.model_validate(corrected)
        to_migrate.append((row["engagement_id"], json.dumps(corrected)))

    if not to_migrate:
        return 0

    async with sqlite_write(conn):
        for engagement_id, corrected_json in to_migrate:
            await conn.execute(
                "UPDATE engagements SET data = ? WHERE engagement_id = ?",
                (corrected_json, engagement_id),
            )

    for engagement_id, _ in to_migrate:
        logger.info(f"Migrated Engagement {engagement_id} from Stage 1E to Stage 1E.1 dinner taxonomy.")

    return len(to_migrate)
