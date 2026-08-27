"""
Luma (lu.ma) event-registration sync -- the SAME processing path
(`process_guest_event`) is used by both the live webhook handler and the
historical backfill, so a backfilled registration and a webhook-delivered
one are indistinguishable in behavior (per the explicit requirement: no
separate "backfill import logic" that behaves differently from live
registrations).

Deliberately reuses CrmService.classify_match()/apply_import_mapping()
completely unmodified -- no Luma-specific fuzzy matching, no Luma-specific
overwrite policy. See app/services/crm_service.py's module docstring for
the exact dedup hierarchy (email -> apollo_contact_id -> linkedin_url ->
name+company fallback, never fuzzy) and merge rule (fill-only-if-empty,
union-merge for multi-select custom fields) this module inherits as-is.

A CrmContact is never created/updated on a POSSIBLE_DUPLICATE match --
the LumaRegistration is still saved (never dropping event history), but
`crm_contact_id` stays null and `match_status` is NEEDS_REVIEW, mirroring
EmailIntakeItem's NEEDS_MATCH precedent. This module never guesses.

Provenance: Luma data's provenance IS the LumaRegistration row itself
(its own `registration_answers`/`registered_at`) -- this module never
writes to CrmContact.source_snapshot (that field is a single shared blob
already used by CSV import/ITF; writing Luma's raw payload there would
silently evict whichever pipeline wrote it last, destroying THEIR
provenance). `source` is set (via apply_import_mapping's existing
create-only rule) only on a brand-new contact, to "luma".
"""

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from app.luma.client import LumaClient
from app.models.activity import ActivityCategory, ActivitySource
from app.models.crm import (
    EXTERNAL_FIELD_NAMES,
    THESIS_FIELD_NAMES,
    CrmContact,
    CrmCustomFieldDefinition,
    CrmImportRowStatus,
    CustomFieldType,
    normalize_email,
    normalize_linkedin_url,
)
from app.models.luma import (
    LumaApprovalStatus,
    LumaBackfillCheckpoint,
    LumaBackfillStatus,
    LumaEvent,
    LumaEventBackfillResult,
    LumaEventTicket,
    LumaMatchStatus,
    LumaQuestionMapping,
    LumaRegistration,
    LumaRegistrationAnswer,
    LumaSyncCounts,
)
from app.repositories.luma_backfill_checkpoint_store import LumaBackfillCheckpointStore
from app.repositories.luma_event_store import LumaEventStore
from app.repositories.luma_question_mapping_store import LumaQuestionMappingStore
from app.repositories.luma_registration_store import LumaRegistrationStore
from app.services.activity_log_service import ActivityLogService
from app.services.crm_service import CUSTOM_FIELD_PREFIX, CrmService
from app.services.luma_answer_normalizers import apply_normalizer

# The only webhook event types Phase 1 processes -- event.*/calendar.*
# lifecycle webhooks are deliberately out of scope this phase (see the
# architecture report: Luma doesn't cleanly expose event status/history,
# and only guest/ticket data is needed for the CRM sync).
SUPPORTED_WEBHOOK_TYPES = frozenset({"guest.registered", "guest.updated", "guest.refunded", "ticket.registered"})


class LumaSyncError(Exception):
    """A payload this module genuinely cannot process (e.g. no event data
    and no prior registration to recover one from) -- distinct from a
    transient failure, so callers can decide whether retrying would help."""


class LumaMappingNotFoundError(Exception):
    def __init__(self, luma_question_mapping_id: str):
        self.luma_question_mapping_id = luma_question_mapping_id
        super().__init__(f"Luma question mapping not found: {luma_question_mapping_id}")


@dataclass
class LumaProcessResult:
    registration: LumaRegistration
    contact: CrmContact | None
    registration_is_new: bool
    contact_outcome: str  # "created" | "enriched" | "needs_review" | "unchanged"
    duplicate_delivery: bool = False
    changed_field_keys: list[str] = field(default_factory=list)
    # {field_key: conflicting_contact_id} for every dedup-tier field an
    # enrichment tried to change but was suppressed on -- see
    # _detect_identity_conflicts(). Never includes the attempted VALUE.
    identity_conflicts: dict[str, str] = field(default_factory=dict)


# The exact three confident dedup-tier fields CrmService.classify_match()
# itself trusts (see crm_service.py's dedup hierarchy) -- deliberately the
# SAME set, not a separately-invented list. Each maps to the CrmContactStore
# lookup method + normalizer (if any) used to check whether a field's new
# value already belongs to a DIFFERENT contact.
_IDENTITY_FIELDS: tuple[str, ...] = ("email", "apollo_contact_id", "linkedin_url")


def _derive_location_summary(event_payload: dict) -> str | None:
    """Best-effort only -- Luma's Event schema doesn't expose a single
    canonical location-summary field. Never raises on an unexpected shape."""
    geo = event_payload.get("geo_address_json")
    if isinstance(geo, dict):
        city = geo.get("city") or geo.get("address")
        if city:
            return str(city)
    if event_payload.get("meeting_url"):
        return "Online"
    return None


def _derive_checked_in_at(tickets: list[dict]) -> str | None:
    """Earliest non-null event_tickets[].checked_in_at -- a guest is
    "Checked In" if ANY ticket has one set (Luma's own definition), so the
    earliest such timestamp is the most meaningful single value to store."""
    timestamps = [t.get("checked_in_at") for t in tickets if t.get("checked_in_at")]
    return min(timestamps) if timestamps else None


def _contact_display_name(contact: CrmContact) -> str:
    name = " ".join(part for part in (contact.first_name, contact.last_name) if part).strip()
    return name or contact.email or contact.crm_contact_id


def _wrap_scalar_for_multi_select(value: Any, field_def: CrmCustomFieldDefinition) -> Any:
    """A mapped/normalized answer may still be a bare scalar (e.g. a
    dropdown question) even though its CRM target is a multi_select
    custom field -- wrap it as a one-item list so it reaches
    apply_import_mapping()'s union-merge list handling instead of being
    written as a raw scalar into a list-typed field."""
    if field_def.field_type == CustomFieldType.MULTI_SELECT and not isinstance(value, list):
        return [value]
    return value


def _filter_to_allowed_options(value: Any, field_def: CrmCustomFieldDefinition) -> Any | None:
    """Never permit a value outside a controlled CRM select field's live,
    configured `options` to be written -- drops anything not present.
    For multi_select, filters the list (an empty result after filtering
    returns None, meaning "write nothing"). For single_select, the whole
    value is dropped (None) unless it's an exact allowed option. This is
    what makes an untranslated Luma label (e.g. "Fund Manager / General
    Partner", which has no CRM equivalent) safely skip instead of writing
    an uncontrolled value -- no special-casing needed for it elsewhere."""
    if field_def.field_type == CustomFieldType.MULTI_SELECT:
        candidates = value if isinstance(value, list) else [value]
        allowed = [v for v in candidates if v in field_def.options]
        return allowed or None
    if field_def.field_type == CustomFieldType.SINGLE_SELECT:
        candidate = value[0] if isinstance(value, list) and value else value
        return candidate if candidate in field_def.options else None
    return value


def _enforce_custom_field_constraints(
    target_field_key: str, value: Any, custom_fields_by_key: dict[str, CrmCustomFieldDefinition]
) -> Any | None:
    """Applies the two generic safety rules above to any mapping targeting
    a controlled-option ("custom:<key>" single_select/multi_select) CRM
    field -- scoped entirely to this Luma layer, never touching
    CrmService/apply_import_mapping. Non-custom targets and custom fields
    that aren't select types pass through unconstrained (no options list
    to validate against). Returns None if nothing valid remains, same
    contract as a failed normalizer."""
    if not target_field_key.startswith(CUSTOM_FIELD_PREFIX):
        return value
    field_def = custom_fields_by_key.get(target_field_key[len(CUSTOM_FIELD_PREFIX) :])
    if field_def is None or field_def.field_type not in (CustomFieldType.SINGLE_SELECT, CustomFieldType.MULTI_SELECT):
        return value
    value = _wrap_scalar_for_multi_select(value, field_def)
    return _filter_to_allowed_options(value, field_def)


def _diff_contact_field_keys(before: CrmContact, after: CrmContact) -> list[str]:
    """Field KEYS only, never values -- what luma.contact.enriched's
    Activity Log metadata is allowed to carry."""
    before_core = before.model_dump(exclude={"updated_at", "custom_fields"})
    after_core = after.model_dump(exclude={"updated_at", "custom_fields"})
    changed = [k for k in after_core if before_core.get(k) != after_core[k]]
    changed += [
        f"custom:{k}" for k in after.custom_fields if before.custom_fields.get(k) != after.custom_fields[k]
    ]
    return sorted(changed)


def _accumulate_counts(counts: LumaSyncCounts, result: LumaProcessResult) -> None:
    if result.duplicate_delivery:
        return
    if result.registration_is_new:
        counts.registrations_created += 1
    else:
        counts.registrations_updated += 1
    if result.contact_outcome == "created":
        counts.contacts_created += 1
    elif result.contact_outcome == "enriched":
        counts.contacts_enriched += 1
    elif result.contact_outcome == "needs_review":
        counts.needs_review += 1


class LumaSyncService:
    def __init__(
        self,
        crm_service: CrmService,
        event_store: LumaEventStore,
        registration_store: LumaRegistrationStore,
        mapping_store: LumaQuestionMappingStore,
        activity_log: ActivityLogService,
        checkpoint_store: LumaBackfillCheckpointStore | None = None,
        luma_client: LumaClient | None = None,
    ):
        self.crm_service = crm_service
        self.event_store = event_store
        self.registration_store = registration_store
        self.mapping_store = mapping_store
        self.activity_log = activity_log
        self.checkpoint_store = checkpoint_store
        self.luma_client = luma_client
        # Per-guest-id concurrency guard -- see process_guest_event()'s
        # docstring for the race this closes. Deliberately per-guest, not a
        # single global lock: two DIFFERENT guests' webhooks still process
        # fully in parallel, only deliveries for the SAME guest serialize.
        # Grows one entry per unique guest_id ever seen for the life of the
        # process (never pruned) -- an accepted, documented tradeoff at
        # this app's scale (an asyncio.Lock is a few dozen bytes; even tens
        # of thousands of guests is negligible memory), same spirit as
        # AstroExportStore's single-instance-only tradeoff elsewhere in
        # this app. dict.setdefault() below is a synchronous, non-`await`
        # operation, so it's atomic with respect to other coroutines on
        # this event loop -- no meta-lock is needed to protect it.
        self._guest_locks: dict[str, asyncio.Lock] = {}

    # --- question mapping management ---------------------------------------
    #
    # Deliberately reachable ONLY from app/api/luma.py's mapping_router
    # (session-authenticated, never in PUBLIC_PATHS) -- process_guest_event()/
    # handle_webhook() above never call any of these; Luma's webhook payload
    # has no path to reach them, by construction (no code in the webhook
    # path references mapping_store.create/save, only mapping_store.list).

    async def list_question_mappings(self, include_inactive: bool = True) -> list[LumaQuestionMapping]:
        return await self.mapping_store.list(include_inactive=include_inactive)

    async def create_question_mapping(self, data: dict[str, Any]) -> LumaQuestionMapping:
        await self._validate_target_field_key(data["target_field_key"])
        now = datetime.now(timezone.utc)
        mapping = LumaQuestionMapping(
            luma_question_mapping_id=str(uuid.uuid4()),
            question_label=data["question_label"],
            question_type=data.get("question_type"),
            target_field_key=data["target_field_key"],
            extract_key=data.get("extract_key"),
            normalizer=data.get("normalizer"),
            active=data.get("active", True),
            created_at=now,
            updated_at=now,
        )
        await self.mapping_store.create(mapping)
        return mapping

    async def update_question_mapping(self, luma_question_mapping_id: str, patch: dict[str, Any]) -> LumaQuestionMapping:
        """`patch` should come from `SomeUpdateRequest.model_dump(exclude_unset=True)`
        -- only keys the caller actually sent are applied; a key present
        with value None (e.g. explicitly clearing extract_key) is honored,
        distinct from a key never mentioned at all."""
        existing = await self.mapping_store.get(luma_question_mapping_id)
        if existing is None:
            raise LumaMappingNotFoundError(luma_question_mapping_id)
        if "target_field_key" in patch and patch["target_field_key"] is not None:
            await self._validate_target_field_key(patch["target_field_key"])
        updated = existing.model_copy(update={**patch, "updated_at": datetime.now(timezone.utc)})
        await self.mapping_store.save(updated)
        return updated

    async def deactivate_question_mapping(self, luma_question_mapping_id: str) -> LumaQuestionMapping:
        """Deactivate, never delete -- preserves configuration history
        (per the explicit product decision), same "archive not delete"
        instinct as CrmContact/EmailSequence elsewhere in this app."""
        return await self.update_question_mapping(luma_question_mapping_id, {"active": False})

    async def _validate_target_field_key(self, target_field_key: str) -> None:
        """Rejects any target_field_key apply_import_mapping() would
        silently ignore -- validated against the EXACT SAME field
        registries that method itself checks (EXTERNAL_FIELD_NAMES/
        THESIS_FIELD_NAMES for core/thesis fields, the live custom-field
        registry for "custom:<key>"), so a mapping that validates here is
        guaranteed to actually take effect."""
        if not target_field_key:
            raise ValueError("target_field_key is required.")
        if target_field_key.startswith(CUSTOM_FIELD_PREFIX):
            key = target_field_key[len(CUSTOM_FIELD_PREFIX) :]
            custom_fields = await self.crm_service.list_custom_fields(include_inactive=True)
            if not any(f.field_key == key for f in custom_fields):
                raise ValueError(f"Unknown custom CRM field: {target_field_key}")
            return
        if target_field_key not in EXTERNAL_FIELD_NAMES and target_field_key not in THESIS_FIELD_NAMES:
            raise ValueError(f"Unknown CRM field: {target_field_key}")

    # --- shared core path: webhook AND backfill both call this -----------

    def _lock_for_guest(self, guest_id: str) -> asyncio.Lock:
        lock = self._guest_locks.get(guest_id)
        if lock is None:
            lock = asyncio.Lock()
            self._guest_locks[guest_id] = lock
        return lock

    async def _detect_identity_conflicts(self, matched_contact: CrmContact, updated: CrmContact) -> dict[str, str]:
        """For each confident dedup-tier field (_IDENTITY_FIELDS) whose
        value `updated` would actually CHANGE relative to `matched_contact`,
        checks whether that new value already belongs to a DIFFERENT
        existing contact -- via the exact same normalized store lookups
        CrmService.classify_match() itself uses, no new matching logic
        invented here. Returns {field_key: conflicting_contact_id} for
        every field that conflicts; never includes the attempted value."""
        conflicts: dict[str, str] = {}
        contact_store = self.crm_service.contact_store

        if updated.email != matched_contact.email:
            normalized = normalize_email(updated.email)
            if normalized:
                other = await contact_store.get_by_email(normalized)
                if other is not None and other.crm_contact_id != matched_contact.crm_contact_id:
                    conflicts["email"] = other.crm_contact_id

        if updated.apollo_contact_id != matched_contact.apollo_contact_id:
            if updated.apollo_contact_id:
                other = await contact_store.get_by_apollo_contact_id(updated.apollo_contact_id)
                if other is not None and other.crm_contact_id != matched_contact.crm_contact_id:
                    conflicts["apollo_contact_id"] = other.crm_contact_id

        if updated.linkedin_url != matched_contact.linkedin_url:
            normalized = normalize_linkedin_url(updated.linkedin_url)
            if normalized:
                other = await contact_store.get_by_linkedin_url(normalized)
                if other is not None and other.crm_contact_id != matched_contact.crm_contact_id:
                    conflicts["linkedin_url"] = other.crm_contact_id

        return conflicts

    async def _enrich_existing_contact(
        self, matched_contact: CrmContact, mapped_fields: dict[str, Any]
    ) -> tuple[CrmContact, list[str], dict[str, str], str]:
        """Applies apply_import_mapping() (unmodified), then detects and
        suppresses any identity-field conflict BEFORE saving -- conflict
        detection is the primary control flow; the store's own UNIQUE
        constraint (the try/except below) is only a defensive backstop for
        a genuine residual race between this check and the save, not the
        normal path. A conflicting field is left at its CURRENT value
        (never overwritten, never used to merge the two contacts) while
        every other, non-conflicting enrichment still applies. Returns
        (contact, changed_field_keys, identity_conflicts, outcome)."""
        updated = self.crm_service.apply_import_mapping(matched_contact, mapped_fields, is_new=False)
        identity_conflicts = await self._detect_identity_conflicts(matched_contact, updated)
        if identity_conflicts:
            reverted = {field_key: getattr(matched_contact, field_key) for field_key in identity_conflicts}
            updated = updated.model_copy(update=reverted)

        changed_field_keys = _diff_contact_field_keys(matched_contact, updated)
        outcome = "unchanged"
        if changed_field_keys:
            try:
                await self.crm_service.contact_store.save(updated)
                outcome = "enriched"
            except ValueError as e:
                # A true residual race (another write landed between the
                # proactive check above and this save) -- never crash the
                # webhook over it. Nothing was actually persisted, so report
                # no change rather than a false "enriched".
                logger.error(
                    f"Luma sync: contact save failed despite proactive conflict check (residual race): {type(e).__name__}: {e}"
                )
                updated = matched_contact
                changed_field_keys = []
        return updated, changed_field_keys, identity_conflicts, outcome

    async def process_guest_event(
        self, event_payload: dict, guest_payload: dict, webhook_delivery_id: str | None = None
    ) -> LumaProcessResult:
        """
        Concurrency-safe entry point. Luma can and does deliver multiple
        near-simultaneous webhook requests for the SAME guest (observed
        directly in production: 3 deliveries ~150 microseconds apart for
        one registration) -- without serializing per guest_id, each
        concurrent request would independently read "no existing
        registration yet" before any of them had saved one, and each would
        conclude it's creating a brand-new registration (and, absent the
        contact-side hardening in _process_guest_event_locked below, could
        race on contact creation too). Root cause: the "is this new"
        decision was a check-then-act read separated from the eventual
        write by several awaited I/O calls, with no lock between them.

        Fix: an asyncio.Lock keyed by guest_id (see _lock_for_guest above)
        wraps the ENTIRE per-guest critical section -- concurrent
        deliveries for the same guest now queue and process one at a time,
        so the second one's "existing" read correctly observes the first
        one's already-committed registration and takes the update path
        instead of racing into "new" again. Deliberately per-guest, not a
        single global lock -- two different guests' webhooks still run
        fully in parallel; only same-guest deliveries serialize. Backfill
        and the webhook handler both call this same method, so both get
        this guarantee identically -- no separate, differently-behaved
        backfill code path.
        """
        guest_id = guest_payload.get("id")
        if not guest_id:
            raise LumaSyncError("Guest payload is missing an id.")
        async with self._lock_for_guest(guest_id):
            return await self._process_guest_event_locked(guest_id, event_payload, guest_payload, webhook_delivery_id)

    async def _process_guest_event_locked(
        self, guest_id: str, event_payload: dict, guest_payload: dict, webhook_delivery_id: str | None
    ) -> LumaProcessResult:
        now = datetime.now(timezone.utc)
        luma_event = self._parse_event(event_payload, now)
        await self.event_store.save(luma_event)

        existing = await self.registration_store.get(guest_id)

        # Exact-duplicate webhook delivery (Luma's own retry-on-timeout
        # behavior) -- pure no-op, never re-touch the CRM or re-log.
        if existing is not None and webhook_delivery_id is not None and existing.last_webhook_delivery_id == webhook_delivery_id:
            return LumaProcessResult(
                registration=existing, contact=None, registration_is_new=False,
                contact_outcome="unchanged", duplicate_delivery=True,
            )

        mapped_fields = await self._build_mapped_fields(guest_payload)

        contact: CrmContact | None = None
        contact_outcome = "unchanged"
        match_status = LumaMatchStatus.MATCHED
        changed_field_keys: list[str] = []
        identity_conflicts: dict[str, str] = {}

        status, matched_contact, _matched_on = await self.crm_service.classify_match(mapped_fields)
        if status == CrmImportRowStatus.NEW:
            try:
                contact = await self.crm_service.create_contact_from_import(mapped_fields)
                contact_outcome = "created"
            except ValueError:
                # Structural race, not a per-guest one: a DIFFERENT guest_id
                # (so the lock above didn't cover it) whose mapped email/
                # apollo_contact_id/linkedin_url collides with this one won
                # the create first -- CrmContactStore's UNIQUE constraint
                # caught it. Per the explicit requirement, this backstop is
                # NOT the primary safety mechanism (the per-guest lock is) --
                # it only exists to resolve this rarer cross-guest overlap
                # gracefully instead of crashing the request. Re-resolve
                # against the now-current state rather than raising.
                status, matched_contact, _matched_on = await self.crm_service.classify_match(mapped_fields)
                if status == CrmImportRowStatus.EXISTING:
                    contact, changed_field_keys, identity_conflicts, contact_outcome = await self._enrich_existing_contact(
                        matched_contact, mapped_fields
                    )
                else:
                    # Extremely unlikely (the conflicting record would have
                    # to vanish between the failed create and this
                    # re-classify) -- never crash the webhook over it.
                    match_status = LumaMatchStatus.NEEDS_REVIEW
                    contact_outcome = "needs_review"
        elif status == CrmImportRowStatus.EXISTING:
            contact, changed_field_keys, identity_conflicts, contact_outcome = await self._enrich_existing_contact(
                matched_contact, mapped_fields
            )
        else:  # POSSIBLE_DUPLICATE -- never attach, never guess, never create another contact
            match_status = LumaMatchStatus.NEEDS_REVIEW
            contact_outcome = "needs_review"

        registration = LumaRegistration(
            luma_guest_id=guest_id,
            luma_event_id=luma_event.luma_event_id,
            crm_contact_id=contact.crm_contact_id if contact else None,
            email_normalized=normalize_email(guest_payload.get("user_email")),
            match_status=match_status,
            approval_status=LumaApprovalStatus(guest_payload.get("approval_status") or LumaApprovalStatus.PENDING_APPROVAL.value),
            registered_at=guest_payload.get("registered_at"),
            invited_at=guest_payload.get("invited_at"),
            joined_at=guest_payload.get("joined_at"),
            checked_in_at=_derive_checked_in_at(guest_payload.get("event_tickets") or []),
            utm_source=guest_payload.get("utm_source"),
            registration_answers=[
                LumaRegistrationAnswer(**a) for a in (guest_payload.get("registration_answers") or [])
            ],
            event_tickets=[LumaEventTicket(**t) for t in (guest_payload.get("event_tickets") or [])],
            last_webhook_delivery_id=webhook_delivery_id or (existing.last_webhook_delivery_id if existing else None),
            synced_at=now,
            updated_at=now,
        )
        await self.registration_store.save(registration)

        result = LumaProcessResult(
            registration=registration,
            contact=contact,
            registration_is_new=existing is None,
            contact_outcome=contact_outcome,
            changed_field_keys=changed_field_keys,
            identity_conflicts=identity_conflicts,
        )
        await self._record_activity(luma_event, existing, result)
        return result

    async def _build_mapped_fields(self, guest: dict) -> dict[str, Any]:
        """Native Luma guest fields + any ACTIVE, configured question
        mapping's answer -- exactly the shape CrmService.classify_match()/
        apply_import_mapping() already expect (core/thesis field name, or
        "custom:<key>"). Never hardcodes a label->field mapping here."""
        mapped_fields: dict[str, Any] = {"source": "luma"}
        if guest.get("user_email"):
            mapped_fields["email"] = guest["user_email"]
        if guest.get("user_first_name"):
            mapped_fields["first_name"] = guest["user_first_name"]
        if guest.get("user_last_name"):
            mapped_fields["last_name"] = guest["user_last_name"]
        if guest.get("phone_number"):
            mapped_fields["phone"] = guest["phone_number"]

        mappings = await self.mapping_store.list(include_inactive=False)
        custom_fields_by_key = {f.field_key: f for f in await self.crm_service.list_custom_fields()}
        for answer in guest.get("registration_answers") or []:
            label = (answer.get("label") or "").strip().lower()
            question_type = answer.get("question_type")
            value = answer.get("value")
            if value is None or label == "":
                continue
            for mapping in mappings:
                if mapping.question_label.strip().lower() != label:
                    continue
                if mapping.question_type and mapping.question_type != question_type:
                    continue
                extracted = value
                if mapping.extract_key:
                    if not isinstance(value, dict):
                        continue
                    extracted = value.get(mapping.extract_key)
                if extracted is None or extracted == "":
                    continue
                if mapping.normalizer is not None:
                    extracted = apply_normalizer(mapping.normalizer, extracted)
                    if extracted is None or extracted == "":
                        # Normalization failed (blank/unrelated/invalid input) --
                        # never populate the target field from a bad transform.
                        continue
                extracted = _enforce_custom_field_constraints(mapping.target_field_key, extracted, custom_fields_by_key)
                if extracted is None:
                    # Nothing survived the CRM option-allowlist filter --
                    # never write a value outside the field's configured options.
                    continue
                mapped_fields[mapping.target_field_key] = extracted
        return mapped_fields

    def _parse_event(self, event_payload: dict, now: datetime) -> LumaEvent:
        event_id = event_payload.get("id")
        if not event_id:
            raise LumaSyncError("Event payload is missing an id.")
        return LumaEvent(
            luma_event_id=event_id,
            calendar_id=event_payload.get("calendar_id"),
            name=event_payload.get("name") or "(untitled Luma event)",
            start_at=event_payload.get("start_at"),
            end_at=event_payload.get("end_at"),
            status=event_payload.get("status"),
            location_summary=_derive_location_summary(event_payload),
            url=event_payload.get("url"),
            synced_at=now,
            updated_at=now,
        )

    async def _record_activity(
        self, luma_event: LumaEvent, existing: LumaRegistration | None, result: LumaProcessResult
    ) -> None:
        """Diff-based: only fires an event when something actually
        happened, and never carries registration answers/contact field
        VALUES -- only counts, statuses, and field KEYS."""
        registration = result.registration
        if existing is None:
            await self.activity_log.record(
                event_type="luma.registration.created",
                category=ActivityCategory.LUMA,
                source=ActivitySource.LUMA_SYNC,
                summary=f"New Luma registration received for {luma_event.name}.",
                entity_type="luma_registration",
                entity_id=registration.luma_guest_id,
                entity_name=luma_event.name,
                metadata={"approval_status": registration.approval_status.value, "match_status": registration.match_status.value},
            )
        else:
            if existing.approval_status != registration.approval_status:
                await self.activity_log.record(
                    event_type="luma.registration.updated",
                    category=ActivityCategory.LUMA,
                    source=ActivitySource.LUMA_SYNC,
                    summary=f"Luma registration status changed for {luma_event.name}.",
                    entity_type="luma_registration",
                    entity_id=registration.luma_guest_id,
                    entity_name=luma_event.name,
                    metadata={"from": existing.approval_status.value, "to": registration.approval_status.value},
                )
            if existing.checked_in_at is None and registration.checked_in_at is not None:
                await self.activity_log.record(
                    event_type="luma.registration.checked_in",
                    category=ActivityCategory.LUMA,
                    source=ActivitySource.LUMA_SYNC,
                    summary=f"Checked in at {luma_event.name}.",
                    entity_type="luma_registration",
                    entity_id=registration.luma_guest_id,
                    entity_name=luma_event.name,
                )

        contact = result.contact
        if result.contact_outcome == "created" and contact is not None:
            name = _contact_display_name(contact)
            await self.activity_log.record(
                event_type="luma.contact.created",
                category=ActivityCategory.LUMA,
                source=ActivitySource.LUMA_SYNC,
                summary=f"{name} was created in the CRM from a Luma registration.",
                entity_type="contact",
                entity_id=contact.crm_contact_id,
                entity_name=name,
            )
        elif contact is not None and result.changed_field_keys:
            name = _contact_display_name(contact)
            await self.activity_log.record(
                event_type="luma.contact.enriched",
                category=ActivityCategory.LUMA,
                source=ActivitySource.LUMA_SYNC,
                summary=f"{name} was enriched from a Luma registration.",
                entity_type="contact",
                entity_id=contact.crm_contact_id,
                entity_name=name,
                metadata={"fields_updated": result.changed_field_keys},
            )

        # Independent of the created/enriched branching above -- a conflict
        # can co-occur with a successful partial enrichment (some fields
        # applied, one identity field suppressed) or stand alone (every
        # attempted change conflicted). Metadata is deliberately structural
        # only: field keys and contact IDs, never the attempted value, the
        # real email, or the real LinkedIn URL.
        if contact is not None and result.identity_conflicts:
            name = _contact_display_name(contact)
            await self.activity_log.record(
                event_type="luma.contact.identity_conflict",
                category=ActivityCategory.LUMA,
                source=ActivitySource.LUMA_SYNC,
                summary=f"A Luma enrichment for {name} conflicted with another CRM contact's identity field and was not applied.",
                entity_type="contact",
                entity_id=contact.crm_contact_id,
                entity_name=name,
                metadata={
                    "conflicting_fields": sorted(result.identity_conflicts.keys()),
                    "matched_contact_id": contact.crm_contact_id,
                    "conflicting_contact_ids": result.identity_conflicts,
                },
            )

    # --- webhook entry point ----------------------------------------------

    async def handle_webhook(self, event_type: str, data: dict, webhook_delivery_id: str) -> LumaProcessResult | None:
        """Returns None for a webhook type outside Phase 1 scope (ignored,
        not an error) -- e.g. event.created/calendar.person.subscribed."""
        if event_type not in SUPPORTED_WEBHOOK_TYPES:
            return None

        event_payload = data.get("event")
        guest_id = data.get("id")
        if event_payload is None and guest_id:
            # guest.refunded's documented shape doesn't clearly include an
            # embedded event in every case -- recover it from whatever we
            # already stored for this guest on an earlier delivery.
            existing = await self.registration_store.get(guest_id)
            if existing is not None:
                stored_event = await self.event_store.get(existing.luma_event_id)
                if stored_event is not None:
                    event_payload = {
                        "id": stored_event.luma_event_id,
                        "calendar_id": stored_event.calendar_id,
                        "name": stored_event.name,
                        "start_at": stored_event.start_at,
                        "end_at": stored_event.end_at,
                        "status": stored_event.status,
                        "url": stored_event.url,
                    }
        if event_payload is None:
            raise LumaSyncError(
                "Webhook payload has no embedded event and no prior registration to recover one from."
            )

        return await self.process_guest_event(event_payload, data, webhook_delivery_id=webhook_delivery_id)

    # --- historical backfill ----------------------------------------------

    async def run_backfill(self, resume: bool = True) -> LumaBackfillCheckpoint:
        """Iterates every event on the configured calendar, every guest on
        every event, through the SAME process_guest_event() path webhooks
        use. Durable, page-granularity checkpointing (see LumaBackfillCheckpoint's
        docstring for exactly what "resumable" means here); per-guest
        failures are isolated and counted, never abort the run."""
        if self.luma_client is None or self.checkpoint_store is None:
            raise LumaSyncError("Backfill requires a configured LumaClient and checkpoint store.")

        now = datetime.now(timezone.utc)
        existing_checkpoint = await self.checkpoint_store.get() if resume else None
        if existing_checkpoint is not None and existing_checkpoint.status in (
            LumaBackfillStatus.RUNNING, LumaBackfillStatus.FAILED,
        ):
            checkpoint = existing_checkpoint
            checkpoint.status = LumaBackfillStatus.RUNNING
            checkpoint.error_message = None
        else:
            checkpoint = LumaBackfillCheckpoint(status=LumaBackfillStatus.RUNNING, started_at=now)
        checkpoint.updated_at = now
        await self.checkpoint_store.save(checkpoint)

        try:
            await self._run_backfill_loop(checkpoint)
            checkpoint.status = LumaBackfillStatus.COMPLETED
            checkpoint.completed_at = datetime.now(timezone.utc)
            checkpoint.updated_at = checkpoint.completed_at
            await self.checkpoint_store.save(checkpoint)
            await self.activity_log.record(
                event_type="luma.sync.completed",
                category=ActivityCategory.LUMA,
                source=ActivitySource.LUMA_SYNC,
                summary=f"Luma historical backfill completed: {checkpoint.counts.events_processed} events processed.",
                metadata=checkpoint.counts.model_dump(),
            )
            return checkpoint
        except Exception as e:
            checkpoint.status = LumaBackfillStatus.FAILED
            checkpoint.error_message = f"{type(e).__name__}: {e}"
            checkpoint.updated_at = datetime.now(timezone.utc)
            await self.checkpoint_store.save(checkpoint)
            logger.error(f"Luma backfill failed: {type(e).__name__}: {e}")
            await self.activity_log.record(
                event_type="luma.sync.failed",
                category=ActivityCategory.ERRORS,
                source=ActivitySource.LUMA_SYNC,
                summary="Luma historical backfill failed.",
                metadata={"error": type(e).__name__, **checkpoint.counts.model_dump()},
            )
            raise

    async def _run_backfill_loop(self, checkpoint: LumaBackfillCheckpoint) -> None:
        event_cursor = checkpoint.event_cursor
        while True:
            events_page = await self.luma_client.list_calendar_events(cursor=event_cursor)
            entries = events_page.get("entries") or []
            has_more_events = bool(events_page.get("has_more"))
            next_event_cursor = events_page.get("next_cursor")

            resume_event_id = checkpoint.in_progress_event_id
            for entry in entries:
                # CONFIRMED against the real production API (2026-08-27):
                # GET /v1/calendars/events/list's entries are the event
                # object DIRECTLY (id/name/start_at/... alongside sibling
                # tags/submitted_by keys) -- NOT nested under an "event"
                # key as originally assumed from documentation alone.
                # _parse_event() only reads specific known keys via
                # .get(...), so the extra sibling keys are harmless.
                event_payload = entry
                event_id = event_payload.get("id")
                if not event_id:
                    continue
                if resume_event_id is not None and event_id != resume_event_id:
                    continue  # skip events on this page already fully processed before a resume
                resume_event_id = None

                guest_cursor = checkpoint.in_progress_guest_cursor if event_id == checkpoint.in_progress_event_id else None
                await self._backfill_one_event(checkpoint, event_payload, event_id, guest_cursor)
                checkpoint.counts.events_processed += 1

            if not has_more_events:
                checkpoint.event_cursor = next_event_cursor
                checkpoint.in_progress_event_id = None
                checkpoint.in_progress_guest_cursor = None
                checkpoint.updated_at = datetime.now(timezone.utc)
                await self.checkpoint_store.save(checkpoint)
                break

            checkpoint.event_cursor = next_event_cursor
            checkpoint.in_progress_event_id = None
            checkpoint.in_progress_guest_cursor = None
            checkpoint.updated_at = datetime.now(timezone.utc)
            await self.checkpoint_store.save(checkpoint)
            event_cursor = next_event_cursor

    async def _backfill_one_event(
        self, checkpoint: LumaBackfillCheckpoint, event_payload: dict, event_id: str, guest_cursor: str | None
    ) -> None:
        while True:
            guests_page = await self.luma_client.list_event_guests(event_id, cursor=guest_cursor)
            for guest_entry in guests_page.get("entries") or []:
                # CONFIRMED against the real production API (2026-08-27):
                # GET /v1/events/guests/list's entries are the guest object
                # DIRECTLY -- same flat shape as the events-list endpoint,
                # not nested under a "guest" key.
                guest_payload = guest_entry
                try:
                    result = await self.process_guest_event(event_payload, guest_payload, webhook_delivery_id=None)
                    _accumulate_counts(checkpoint.counts, result)
                except Exception as e:  # noqa: BLE001 -- one bad guest must never abort the whole backfill
                    checkpoint.counts.errors += 1
                    logger.error(
                        f"Luma backfill: failed to process guest in event {event_id}: {type(e).__name__}: {e}"
                    )

            has_more_guests = bool(guests_page.get("has_more"))
            next_guest_cursor = guests_page.get("next_cursor")
            checkpoint.in_progress_event_id = event_id if has_more_guests else None
            checkpoint.in_progress_guest_cursor = next_guest_cursor if has_more_guests else None
            checkpoint.updated_at = datetime.now(timezone.utc)
            await self.checkpoint_store.save(checkpoint)

            if not has_more_guests:
                return
            guest_cursor = next_guest_cursor

    # --- targeted, single-event backfill -----------------------------------

    async def _find_calendar_event(self, event_id: str) -> dict | None:
        """Locates ONE event's full payload by id. Luma's API has no
        single-event GET, only the paginated calendar list, so this pages
        through calendar events (bounded by the small number of events on
        one calendar) until it finds a match, then stops -- it never pages
        through any event's GUEST list looking for this, which is the
        expensive traversal event-scoped backfill exists to avoid."""
        cursor = None
        while True:
            page = await self.luma_client.list_calendar_events(cursor=cursor)
            for entry in page.get("entries") or []:
                if entry.get("id") == event_id:
                    return entry
            if not page.get("has_more"):
                return None
            cursor = page.get("next_cursor")

    async def run_event_backfill(
        self, event_id: str, approval_status: LumaApprovalStatus | None = None
    ) -> LumaEventBackfillResult:
        """One-time targeted backfill for exactly ONE Luma event -- reuses
        process_guest_event() (the exact same path webhooks and the
        full-calendar backfill both use), so every existing safety
        guarantee (guest-id idempotency, CRM matching/dedup,
        POSSIBLE_DUPLICATE handling, identity-conflict protection,
        question mappings, raw-answer preservation, Activity Log
        behavior) applies unchanged -- nothing here re-implements or
        bypasses any of it.

        Deliberately does NOT use the durable LumaBackfillCheckpoint this
        class's full-calendar run_backfill() depends on: that checkpoint's
        cursor/resume state is designed for a long, multi-event,
        possibly-interrupted job, and sharing it with this small,
        single-event call would risk corrupting state a LATER
        full-calendar backfill needs to resume correctly. Safe to rerun
        from scratch instead -- every guest goes through the same
        idempotent, dedup-aware path a resumed run would use anyway (a
        guest already enriched with unchanged data simply produces no
        new CRM write and no new Activity Log entry), so no persisted
        cursor is needed for correctness here, only for pagination within
        this one call.

        approval_status, when given, is an exact-match filter against each
        guest's OWN `approval_status` -- a guest that doesn't match is
        never passed to process_guest_event() at all: no registration, no
        contact, no Activity Log entry for it. Guests on OTHER events are
        never even fetched -- only this one event's guest list is paged.
        """
        if self.luma_client is None:
            raise LumaSyncError("Backfill requires a configured LumaClient.")

        event_payload = await self._find_calendar_event(event_id)
        if event_payload is None:
            raise LumaSyncError(f"No calendar event found with id {event_id!r}.")

        counts = LumaSyncCounts(events_processed=1)
        guests_seen = 0
        guests_matched = 0
        cursor = None
        while True:
            guests_page = await self.luma_client.list_event_guests(event_id, cursor=cursor)
            for guest_payload in guests_page.get("entries") or []:
                guests_seen += 1
                if approval_status is not None and guest_payload.get("approval_status") != approval_status.value:
                    continue
                guests_matched += 1
                try:
                    result = await self.process_guest_event(event_payload, guest_payload, webhook_delivery_id=None)
                    _accumulate_counts(counts, result)
                except Exception as e:  # noqa: BLE001 -- one bad guest must never abort the rest of this event
                    counts.errors += 1
                    logger.error(
                        f"Luma event backfill: failed to process guest in event {event_id}: {type(e).__name__}: {e}"
                    )

            if not guests_page.get("has_more"):
                break
            cursor = guests_page.get("next_cursor")

        event_name = event_payload.get("name") or "(untitled Luma event)"

        # Emitted exactly once per invocation (never per-guest) -- only
        # reached if the run above completed without an unhandled
        # exception; a per-guest failure is already isolated and counted
        # in counts.errors above, so this "completed" event is accurate
        # even when some guests failed. Aggregate counts and structural
        # identifiers only -- no guest name, email, registration answer,
        # contact payload, or credential ever reaches this metadata.
        await self.activity_log.record(
            event_type="luma.event_backfill.completed",
            category=ActivityCategory.LUMA,
            source=ActivitySource.LUMA_SYNC,
            summary=f"Targeted Luma backfill completed for {event_name}: {guests_matched} of {guests_seen} guests processed.",
            entity_type="luma_event",
            entity_id=event_id,
            entity_name=event_name,
            metadata={
                "event_id": event_id,
                "event_name": event_name,
                "approval_status_filter": approval_status.value if approval_status is not None else None,
                "guests_seen": guests_seen,
                "guests_matching_filter": guests_matched,
                "registrations_created": counts.registrations_created,
                "registrations_updated": counts.registrations_updated,
                "contacts_created": counts.contacts_created,
                "contacts_enriched": counts.contacts_enriched,
                "needs_review": counts.needs_review,
                "errors": counts.errors,
            },
        )

        return LumaEventBackfillResult(
            event_id=event_id,
            event_name=event_name,
            guests_seen=guests_seen,
            guests_matching_filter=guests_matched,
            counts=counts,
        )
