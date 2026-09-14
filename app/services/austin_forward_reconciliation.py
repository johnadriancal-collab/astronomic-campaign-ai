"""
Austin Forward -- Sept 10, 2026 Contact Reconciliation (Stage AF-2) --
driver for the frozen cohort in austin_forward_reconciliation_cohort.py.

SAFE BY DEFAULT: with no arguments, this is a DRY RUN against the fixed,
approved 166-person cohort. Writes NOTHING (no Contact save, no List
membership, no Activity Log entry).

Real writes require BOTH --write AND --confirm-production-writes (see
scripts/run_austin_forward_reconciliation.py) -- same two-gate convention
as every other operator write CLI in this repo (Stage 3C, Stage 6C). There
is NO target-id argument of any kind -- the write-eligible universe is
always exactly EXISTING_CONTACTS + NEW_CONTACTS from the frozen cohort
module; this command cannot be pointed at any other Contact.

Every row is re-evaluated LIVE at write time -- never a frozen replay of
the investigation's own dry-run snapshot. An EXISTING_CONTACTS row whose
Contact has gone missing, been archived, or had its email/LinkedIn changed
since the cohort was frozen is reported as drift and skipped -- never
forced. A NEW_CONTACTS row that now matches an existing Contact (created by
this driver's own earlier run, or by any other means since) is reported as
already-resolved and skipped -- this, plus every per-field union/fill-only/
provenance-gated check below, is what makes a second run produce zero
mutations.

Field-by-field write rules (all fill-only or union-add, never destructive):
  - dinners_attended / dinner_subscriptions: add the target value only if
    not already present in that Contact's list -- every other existing
    value is untouched.
  - Contact List membership: added via the store's own idempotent add()
    (INSERT OR IGNORE) against the ONE existing target list id -- never
    creates a list.
  - city/state/country: filled only if currently blank; a nonblank
    existing value is never touched, regardless of what Austin Forward's
    location would suggest.
  - role: union-added, existing roles first genuinely already-approved
    Role values from the frozen row's `roles` tuple that this Contact
    doesn't already have -- never removes an existing role.
  - notes: gated entirely by `field_provenance["notes"].source ==
    PROVENANCE_SOURCE` -- if already recorded, notes are left completely
    alone (idempotent even though the visible text itself carries no
    machine-readable marker, per the approved "keep provenance out of the
    visible field" design). Otherwise: blank notes get the accomplishments
    text stored directly (no header -- nothing to separate from); nonblank
    notes get "\\n\\n{NOTES_HEADER}\\n{accomplishments}" appended.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from app.models.activity import ActivityCategory, ActivitySource
from app.models.crm import CrmContact
from app.repositories.crm_contact_list_member_store import CrmContactListMemberStore
from app.repositories.crm_contact_store import CrmContactStore
from app.services.activity_log_service import ActivityLogService
from app.services.austin_forward_reconciliation_cohort import (
    DINNER_ATTENDED_VALUE,
    DINNER_SUBSCRIPTION_VALUE,
    EXISTING_CONTACTS,
    NEW_CONTACTS,
    NOTES_HEADER,
    PROVENANCE_SOURCE,
    TARGET_LIST_ID,
    TARGET_LOCATION,
    ExistingContactRow,
    NewContactRow,
)

FIELD_PROVENANCE_KEY = "field_provenance"


class ExistingRowOutcome(str, Enum):
    WOULD_UPDATE = "would_update"
    UPDATED = "updated"
    ALREADY_SATISFIED = "already_satisfied"
    DRIFT_CONTACT_MISSING = "drift_contact_missing"
    DRIFT_CONTACT_ARCHIVED = "drift_contact_archived"
    DRIFT_IDENTITY_CHANGED = "drift_identity_changed"
    ERROR = "error"


class NewRowOutcome(str, Enum):
    WOULD_CREATE = "would_create"
    CREATED = "created"
    DRIFT_ALREADY_EXISTS = "drift_already_exists_now"
    ERROR = "error"


def _normalize_email(email: str | None) -> str | None:
    if not email:
        return None
    e = email.strip().lower()
    return e or None


def _normalize_linkedin(url: str | None) -> str | None:
    import re

    if not url:
        return None
    u = url.strip().lower()
    if not u:
        return None
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    u = u.rstrip("/")
    u = u.split("?")[0]
    return u or None


def _contact_display_name(contact: CrmContact) -> str:
    name = " ".join(part for part in (contact.first_name, contact.last_name) if part).strip()
    return name or contact.email or contact.crm_contact_id


def _has_notes_provenance(contact: CrmContact) -> bool:
    prov = (contact.custom_fields or {}).get(FIELD_PROVENANCE_KEY) or {}
    notes_prov = prov.get("notes")
    return isinstance(notes_prov, dict) and notes_prov.get("source") == PROVENANCE_SOURCE


def _notes_plan(current_notes: Any, accomplishments: str) -> tuple[str | None, str | None]:
    """Returns (action, new_notes_value). action is None if there is no
    accomplishments text to add. Never called if provenance already
    recorded (see _has_notes_provenance) -- that check happens upstream so
    this function's job is purely "what would the new value be," not
    "should we write at all."
    """
    text = (accomplishments or "").strip()
    if not text:
        return None, None
    current = (current_notes or "").strip() if isinstance(current_notes, str) else ""
    if not current:
        return "insert_blank", text
    return "append", f"{current}\n\n{NOTES_HEADER}\n{text}"


@dataclass
class ExistingRowResult:
    crm_contact_id: str
    full_name: str
    outcome: ExistingRowOutcome
    dinners_attended_add: bool = False
    dinner_subscriptions_add: bool = False
    list_membership_add: bool = False
    city_fill: bool = False
    state_fill: bool = False
    country_fill: bool = False
    roles_added: tuple[str, ...] = ()
    notes_action: str | None = None  # None | "insert_blank" | "append"
    detail: str = ""


@dataclass
class NewRowResult:
    full_name: str
    outcome: NewRowOutcome
    crm_contact_id: str | None = None  # set once actually created (write mode only)
    proposed_roles: tuple[str, ...] = ()
    has_notes: bool = False
    detail: str = ""


@dataclass
class DryRunCounts:
    existing_total: int = 0
    existing_would_update: int = 0
    existing_already_satisfied: int = 0
    existing_drift: int = 0
    new_total: int = 0
    new_would_create: int = 0
    new_drift_already_exists: int = 0
    dinners_attended_additions: int = 0
    dinner_subscriptions_additions: int = 0
    list_membership_additions: int = 0
    city_fills: int = 0
    state_fills: int = 0
    country_fills: int = 0
    role_additions_total: int = 0
    role_additions_by_role: dict[str, int] = field(default_factory=dict)
    notes_blank_inserts: int = 0
    notes_appends: int = 0
    new_contacts_with_notes: int = 0


@dataclass
class DryRunReport:
    counts: DryRunCounts
    existing_results: list[ExistingRowResult]
    new_results: list[NewRowResult]


def _plan_existing_row(row: ExistingContactRow, contact: CrmContact | None) -> ExistingRowResult:
    if contact is None:
        return ExistingRowResult(row.crm_contact_id, row.full_name, ExistingRowOutcome.DRIFT_CONTACT_MISSING)
    if contact.archived:
        return ExistingRowResult(row.crm_contact_id, row.full_name, ExistingRowOutcome.DRIFT_CONTACT_ARCHIVED)

    current_email_norm = _normalize_email(contact.email)
    current_linkedin_norm = _normalize_linkedin(contact.linkedin_url)
    still_matches = (row.matched_email_norm is not None and row.matched_email_norm == current_email_norm) or (
        row.matched_linkedin_norm is not None and row.matched_linkedin_norm == current_linkedin_norm
    )
    if not still_matches:
        return ExistingRowResult(row.crm_contact_id, row.full_name, ExistingRowOutcome.DRIFT_IDENTITY_CHANGED)

    cf = contact.custom_fields or {}
    result = ExistingRowResult(row.crm_contact_id, row.full_name, ExistingRowOutcome.ALREADY_SATISFIED)

    dinners_attended = cf.get("dinners_attended") or []
    result.dinners_attended_add = DINNER_ATTENDED_VALUE not in dinners_attended

    dinner_subscriptions = cf.get("dinner_subscriptions") or []
    result.dinner_subscriptions_add = DINNER_SUBSCRIPTION_VALUE not in dinner_subscriptions

    result.city_fill = not (contact.city or "").strip()
    result.state_fill = not (contact.state or "").strip()
    result.country_fill = not (contact.country or "").strip()

    existing_roles = set(cf.get("role") or [])
    result.roles_added = tuple(r for r in row.roles if r not in existing_roles)

    if not _has_notes_provenance(contact):
        action, _ = _notes_plan(cf.get("notes"), row.accomplishments)
        result.notes_action = action

    any_change = (
        result.dinners_attended_add
        or result.dinner_subscriptions_add
        or result.city_fill
        or result.state_fill
        or result.country_fill
        or bool(result.roles_added)
        or result.notes_action is not None
    )
    # list membership is checked by the caller (needs the member store), folded in below
    result.outcome = ExistingRowOutcome.WOULD_UPDATE if any_change else ExistingRowOutcome.ALREADY_SATISFIED
    return result


async def compute_dry_run_report(
    contact_store: CrmContactStore,
    list_member_store: CrmContactListMemberStore,
) -> DryRunReport:
    existing_results: list[ExistingRowResult] = []
    counts = DryRunCounts(existing_total=len(EXISTING_CONTACTS), new_total=len(NEW_CONTACTS))

    existing_member_ids = set(await list_member_store.list_contact_ids_for_list(TARGET_LIST_ID))

    for row in EXISTING_CONTACTS:
        contact = await contact_store.get(row.crm_contact_id)
        result = _plan_existing_row(row, contact)
        if result.outcome in (ExistingRowOutcome.WOULD_UPDATE, ExistingRowOutcome.ALREADY_SATISFIED):
            result.list_membership_add = row.crm_contact_id not in existing_member_ids
            if result.list_membership_add and result.outcome == ExistingRowOutcome.ALREADY_SATISFIED:
                result.outcome = ExistingRowOutcome.WOULD_UPDATE

        if result.outcome == ExistingRowOutcome.WOULD_UPDATE:
            counts.existing_would_update += 1
            counts.dinners_attended_additions += int(result.dinners_attended_add)
            counts.dinner_subscriptions_additions += int(result.dinner_subscriptions_add)
            counts.list_membership_additions += int(result.list_membership_add)
            counts.city_fills += int(result.city_fill)
            counts.state_fills += int(result.state_fill)
            counts.country_fills += int(result.country_fill)
            counts.role_additions_total += len(result.roles_added)
            for r in result.roles_added:
                counts.role_additions_by_role[r] = counts.role_additions_by_role.get(r, 0) + 1
            if result.notes_action == "insert_blank":
                counts.notes_blank_inserts += 1
            elif result.notes_action == "append":
                counts.notes_appends += 1
        elif result.outcome == ExistingRowOutcome.ALREADY_SATISFIED:
            counts.existing_already_satisfied += 1
        else:
            counts.existing_drift += 1

        existing_results.append(result)

    new_results: list[NewRowResult] = []
    # live re-check against ALL current contacts (including ones this same run's
    # existing-row pass might have just seen, and any created by an EARLIER
    # invocation of this driver) -- never trust the frozen investigation-time
    # "no match" verdict blindly at write time.
    all_contacts = await contact_store.list()
    email_index: dict[str, list[str]] = {}
    linkedin_index: dict[str, list[str]] = {}
    for c in all_contacts:
        ne = _normalize_email(c.email)
        nl = _normalize_linkedin(c.linkedin_url)
        if ne:
            email_index.setdefault(ne, []).append(c.crm_contact_id)
        if nl:
            linkedin_index.setdefault(nl, []).append(c.crm_contact_id)

    for row in NEW_CONTACTS:
        ne = _normalize_email(row.email)
        nl = _normalize_linkedin(row.linkedin_url)
        now_matches = bool((ne and email_index.get(ne)) or (nl and linkedin_index.get(nl)))
        if now_matches:
            new_results.append(NewRowResult(row.full_name, NewRowOutcome.DRIFT_ALREADY_EXISTS))
            counts.new_drift_already_exists += 1
            continue

        has_notes = bool((row.accomplishments or "").strip())
        new_results.append(
            NewRowResult(
                row.full_name,
                NewRowOutcome.WOULD_CREATE,
                proposed_roles=row.roles,
                has_notes=has_notes,
            )
        )
        counts.new_would_create += 1
        counts.role_additions_total += len(row.roles)
        for r in row.roles:
            counts.role_additions_by_role[r] = counts.role_additions_by_role.get(r, 0) + 1
        if has_notes:
            counts.new_contacts_with_notes += 1
            counts.notes_blank_inserts += 1  # a brand-new Contact's notes always starts blank

    return DryRunReport(counts=counts, existing_results=existing_results, new_results=new_results)


def _new_provenance_entry() -> dict[str, Any]:
    return {"source": PROVENANCE_SOURCE, "imported_at": datetime.now(timezone.utc).isoformat()}


async def apply_frozen_cohort_reconciliation(
    contact_store: CrmContactStore,
    list_member_store: CrmContactListMemberStore,
    activity_log: ActivityLogService,
) -> DryRunReport:
    """The write path. Reuses the exact same per-row planning as
    compute_dry_run_report() (never a separate/diverging code path), then
    actually persists each planned change. A row whose plan has zero
    changes (ALREADY_SATISFIED) is never saved and never logged -- true
    no-op, matching this codebase's own established idempotency contract
    (see Stage 6C's identical convention)."""
    report = await compute_dry_run_report(contact_store, list_member_store)

    for result in report.existing_results:
        if result.outcome != ExistingRowOutcome.WOULD_UPDATE:
            continue
        contact = await contact_store.get(result.crm_contact_id)
        if contact is None:
            continue  # drifted away between planning and write within this same call -- skip, don't force

        updates: dict[str, Any] = {}
        cf = dict(contact.custom_fields or {})
        touched_field_keys: list[str] = []

        if result.dinners_attended_add:
            cf["dinners_attended"] = [*(cf.get("dinners_attended") or []), DINNER_ATTENDED_VALUE]
            touched_field_keys.append("custom:dinners_attended")
        if result.dinner_subscriptions_add:
            cf["dinner_subscriptions"] = [*(cf.get("dinner_subscriptions") or []), DINNER_SUBSCRIPTION_VALUE]
            touched_field_keys.append("custom:dinner_subscriptions")
        if result.roles_added:
            cf["role"] = [*(cf.get("role") or []), *result.roles_added]
            touched_field_keys.append("custom:role")

        if result.city_fill or result.state_fill or result.country_fill:
            prov = dict(cf.get(FIELD_PROVENANCE_KEY) or {})
            if result.city_fill:
                updates["city"] = TARGET_LOCATION["city"]
                prov["city"] = _new_provenance_entry()
                touched_field_keys.append("city")
            if result.state_fill:
                updates["state"] = TARGET_LOCATION["state"]
                prov["state"] = _new_provenance_entry()
                touched_field_keys.append("state")
            if result.country_fill:
                updates["country"] = TARGET_LOCATION["country"]
                prov["country"] = _new_provenance_entry()
                touched_field_keys.append("country")
            cf[FIELD_PROVENANCE_KEY] = prov

        if result.notes_action:
            row = next(r for r in EXISTING_CONTACTS if r.crm_contact_id == result.crm_contact_id)
            _, new_notes = _notes_plan(cf.get("notes"), row.accomplishments)
            cf["notes"] = new_notes
            prov = dict(cf.get(FIELD_PROVENANCE_KEY) or {})
            prov["notes"] = _new_provenance_entry()
            cf[FIELD_PROVENANCE_KEY] = prov
            touched_field_keys.append("custom:notes")

        updates["custom_fields"] = cf
        updates["updated_at"] = datetime.now(timezone.utc)
        updated_contact = contact.model_copy(update=updates)
        await contact_store.save(updated_contact)
        result.outcome = ExistingRowOutcome.UPDATED

        if result.list_membership_add:
            from app.models.crm import CrmContactListMembership

            await list_member_store.add(
                CrmContactListMembership(
                    list_id=TARGET_LIST_ID,
                    crm_contact_id=result.crm_contact_id,
                    added_at=datetime.now(timezone.utc),
                )
            )
            touched_field_keys.append("list_membership")

        name = _contact_display_name(updated_contact)
        await activity_log.record(
            event_type="austin_forward.contact_enriched",
            category=ActivityCategory.CONTACTS,
            source=ActivitySource.CSV_IMPORT,
            summary=f"{name} was enriched from the Austin Forward guest list.",
            entity_type="contact",
            entity_id=result.crm_contact_id,
            entity_name=name,
            metadata={"fields_updated": touched_field_keys, "source": PROVENANCE_SOURCE},
        )

    for result in report.new_results:
        if result.outcome != NewRowOutcome.WOULD_CREATE:
            continue
        row = next(r for r in NEW_CONTACTS if r.full_name == result.full_name)

        cf: dict[str, Any] = {
            "dinners_attended": [DINNER_ATTENDED_VALUE],
            "dinner_subscriptions": [DINNER_SUBSCRIPTION_VALUE],
        }
        touched_field_keys = ["custom:dinners_attended", "custom:dinner_subscriptions"]
        if row.roles:
            cf["role"] = list(row.roles)
            touched_field_keys.append("custom:role")

        prov: dict[str, Any] = {"city": _new_provenance_entry(), "state": _new_provenance_entry(), "country": _new_provenance_entry()}
        touched_field_keys += ["city", "state", "country"]

        if (row.accomplishments or "").strip():
            cf["notes"] = row.accomplishments.strip()
            prov["notes"] = _new_provenance_entry()
            touched_field_keys.append("custom:notes")

        cf[FIELD_PROVENANCE_KEY] = prov

        now = datetime.now(timezone.utc)
        import uuid

        new_contact = CrmContact(
            crm_contact_id=str(uuid.uuid4()),
            created_at=now,
            updated_at=now,
            first_name=row.first_name,
            last_name=row.last_name,
            email=row.email,
            linkedin_url=row.linkedin_url,
            city=TARGET_LOCATION["city"],
            state=TARGET_LOCATION["state"],
            country=TARGET_LOCATION["country"],
            custom_fields=cf,
        )
        # create(), never save() -- save() is UPDATE-only (raises
        # CrmContactNotFoundError for a crm_contact_id it's never seen), see
        # sqlite_crm_contact_store.py. create() also enforces the store's own
        # email/LinkedIn uniqueness constraint as a last-line duplicate guard
        # beyond this function's own live re-check above.
        await contact_store.create(new_contact)
        result.crm_contact_id = new_contact.crm_contact_id
        result.outcome = NewRowOutcome.CREATED

        from app.models.crm import CrmContactListMembership

        await list_member_store.add(
            CrmContactListMembership(
                list_id=TARGET_LIST_ID, crm_contact_id=new_contact.crm_contact_id, added_at=now
            )
        )
        touched_field_keys.append("list_membership")

        name = _contact_display_name(new_contact)
        await activity_log.record(
            event_type="austin_forward.contact_created",
            category=ActivityCategory.CONTACTS,
            source=ActivitySource.CSV_IMPORT,
            summary=f"{name} was created from the Austin Forward guest list.",
            entity_type="contact",
            entity_id=new_contact.crm_contact_id,
            entity_name=name,
            metadata={"fields_updated": touched_field_keys, "source": PROVENANCE_SOURCE},
        )

    return report
