"""
Notes / Personal Notes Consolidation (Stage NP-2) -- the EXPLICIT, frozen,
human-reviewed migration cohort. This is data, not logic: every row below
is a Contact that, at Stage NP-1 audit time, had a nonblank `personal_notes`
value -- the exact 9-Contact population NP-1's read-only production audit
found. Nothing here is computed, scanned, or derived at runtime.

This module intentionally has NO function that discovers additional
candidates and NO store/repository/asyncio import of any kind (verified by
a structural test) -- see app/services/notes_personal_notes_merge.py's own
docstring for why the write path can only ever touch these exact 9 rows,
forever, until a human explicitly edits this file and a NEW investigation/
review cycle approves the change (e.g. if a Contact picks up a new
personal_notes value after this audit, it is deliberately NOT swept up by
this one-time migration -- see the driver's own docstring for why an
ever-growing scan would defeat the point of a frozen, reviewed cohort).

`frozen_notes`/`frozen_personal_notes` are the EXACT values observed at
audit time (2026-09-14) -- compared against the LIVE Contact immediately
before any write (see the driver's own drift check), so a row whose
underlying Notes or Personal Notes has since changed is skipped as drift
rather than trusted blindly, per the explicit "fail safe on drift" design.

None of the 9 rows below carry any `field_provenance["notes"]` entry today
(confirmed at audit time) -- none happen to overlap with the 161 Austin
Forward-sourced Contacts. The driver's own provenance design (a separate
`field_provenance["personal_notes_merge"]` key, never touching
`field_provenance["notes"]`) is still fully exercised by the test suite
against a synthetic Contact that DOES carry Austin Forward provenance, so
this is proven correct for the general case, not just today's specific
(non-overlapping) production reality.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class NotesMergeCohortRow:
    crm_contact_id: str
    frozen_notes: str | None  # None means Notes was blank at audit time
    frozen_personal_notes: str  # always nonblank -- this is the cohort's own defining criterion


COHORT: tuple[NotesMergeCohortRow, ...] = (
    NotesMergeCohortRow(
        crm_contact_id="43fd228c-2b02-4a20-ba49-850935e85b40",
        frozen_notes=None,
        frozen_personal_notes="we have an 8 figure family office of mine that is being run by our wealth managers.Sometimes I do a personal small checks too",
    ),
    NotesMergeCohortRow(
        crm_contact_id="8424e905-2c2c-4f91-926e-da5fec2b9e1d",
        frozen_notes=None,
        frozen_personal_notes="https://www.benjamindavidnovak.com/",
    ),
    NotesMergeCohortRow(
        crm_contact_id="cc43669b-7205-4109-a58a-80cedb92a8ae",
        frozen_notes=None,
        frozen_personal_notes="Liz Guenther and Barbie Bowen are married to each other",
    ),
    NotesMergeCohortRow(
        crm_contact_id="d5ba4412-ef1a-4393-a664-59e21df9c66d",
        frozen_notes=None,
        frozen_personal_notes="\"All dinners\tColony Hills\tSeptember 6\t\tChris\"",
    ),
    NotesMergeCohortRow(
        crm_contact_id="bbe9b6a3-b16a-49af-8613-b5e38dc78566",
        frozen_notes="She is Barbie Bowen's wife. Also illiquid. Love her, but she's not actively deploying capital. - Chris; VacayMyWay",
        frozen_personal_notes="Liz Guenther and Barbie Bowen are married to each other",
    ),
    NotesMergeCohortRow(
        crm_contact_id="acf3964a-8c67-4562-b116-6678a7e1fc42",
        frozen_notes="Legit - Chris",
        frozen_personal_notes="LP in Liveoak Venture Partners here in Austin as well as Alumni Ventures.",
    ),
    NotesMergeCohortRow(
        crm_contact_id="f32fb0e2-3bca-4b7a-9b1c-22ccbee76643",
        frozen_notes=None,
        frozen_personal_notes="He lives in New York. He invests in tech, SaaS, and dev tools. He is a post-exit founder. He's originally from France.",
    ),
    NotesMergeCohortRow(
        crm_contact_id="5b4fd7fb-ee84-46ab-be4d-c9da28c4d869",
        frozen_notes=None,
        frozen_personal_notes="John, Instead of me asking this guy to fill out our thesis form, will you please add him to our database for Dallas and for private real estate investing? - Chris",
    ),
    NotesMergeCohortRow(
        crm_contact_id="798bd67b-63ed-450e-aa56-928bc61b30be",
        frozen_notes="Prefers to invest in tech - Chris",
        frozen_personal_notes="Friends of David Buttross",
    ),
)

# Structural constants a caller/test can assert against without recounting the tuple above.
COHORT_SIZE = 9
PROVENANCE_MERGE_KEY = "personal_notes_merge"
PROVENANCE_MERGE_SOURCE = "personal_notes_merge"
