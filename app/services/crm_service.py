"""
Core CRM service: manual contact CRUD, search/filter, custom field
definitions, and the merge/dedup logic shared by both manual editing and
CSV import (see crm_import_service.py, which calls into
classify_match()/apply_import_mapping() rather than re-implementing them).

Merge rule (the crux of "the CRM is our own source of truth"):
  - External/source fields, Investor Thesis fields, AND custom fields are
    ALL treated identically: NEVER automatically overwritten if a value
    already exists, whether the incoming value is blank OR a genuinely
    different non-empty value. Only filled in when the existing value is
    currently empty. A conflicting CSV value for an already-populated
    field (e.g. a re-imported contact whose CSV row has a different
    LinkedIn URL or Company than what's already on file) is a source-data
    question for a human, not something an automated import should ever
    resolve by picking a side -- so it's silently left alone rather than
    guessed at. (External fields used to overwrite on any non-empty
    incoming value; changed after a real re-import overwrote a correct
    LinkedIn URL/Company with a mismatched row from a messier CSV export.)
  - `source_snapshot` is never merged -- always fully replaced with the
    latest raw import payload, since it's explicitly not authoritative.

Manual edits (update_contact) go through a DIFFERENT path (direct partial
update, no merge rule) -- a human explicitly editing a field is not an
ambiguous import and is always allowed to set or clear it.
"""

import uuid
from datetime import datetime, timezone
from typing import Any

from app.models.crm import (
    EXTERNAL_FIELD_NAMES,
    THESIS_FIELD_NAMES,
    CrmContact,
    CrmContactList,
    CrmContactListMembership,
    CrmContactListSummary,
    CrmContactPage,
    CrmCustomFieldDefinition,
    CrmImportRowStatus,
    CrmListBulkAddResult,
    CrmListBulkRemoveResult,
    CustomFieldType,
    FilterFieldMeta,
    FilterQuery,
    derive_investor_mode,
    normalize_email,
    normalize_linkedin_url,
    normalize_name_company,
)
from app.repositories.crm_contact_list_member_store import (
    CrmContactListMemberStore,
    MemoryCrmContactListMemberStore,
)
from app.repositories.crm_contact_list_store import CrmContactListNotFoundError, CrmContactListStore, MemoryCrmContactListStore
from app.repositories.crm_contact_store import CrmContactNotFoundError, CrmContactStore, MemoryCrmContactStore
from app.repositories.crm_custom_field_store import (
    CrmCustomFieldNotFoundError,
    CrmCustomFieldStore,
    MemoryCrmCustomFieldStore,
)

CUSTOM_FIELD_PREFIX = "custom:"

# 2026-08-07 Dietary Preferences design -- opt-in exception to the fill-only merge
# rule above, for core thesis fields specifically. Every other thesis field (Asset
# Types, Business Models, ...) keeps the fill-only behavior verbatim; only fields
# named here get the same union-merge treatment custom multi-selects already have,
# so a later CSV can add a newly-disclosed restriction without ever dropping one a
# contact already had. Deliberately a separate, explicitly-named set rather than a
# rewrite of the shared merge rule -- same "one small named set governs one specific
# behavior" pattern as LIST_FIELD_NAMES/BOOLEAN_FIELD_NAMES in crm_import_service.py.
UNION_MERGE_THESIS_LIST_FIELDS = frozenset({"thesis_dietary_preferences"})

# Companion free-text "overflow" fields that behave like a delimited SET of raw
# strings rather than a single opaque value: a new unrecognized value is appended
# (deduplicated) rather than only filled in from empty or replaced outright.
UNION_MERGE_DELIMITED_TEXT_FIELDS = frozenset({"thesis_dietary_preferences_other"})
DELIMITED_TEXT_SEPARATOR = "; "

# 2026-08-10 ITF intake design -- a second, distinct opt-in exception, this time for
# custom fields whose entire purpose is "the most recent value we were told", not "a
# value worth protecting once set". itf_submitted_at must always reflect the latest
# Google Form submission timestamp for a returning respondent -- fill-only-if-empty
# would freeze it at whatever the first submission said, which is exactly wrong for a
# field meant to answer "when did this person last submit the ITF". Scoped to custom
# fields only (checked in _apply_custom_field below); core/thesis fields have no such
# field today and keep the fill-only rule unconditionally.
LATEST_WINS_CUSTOM_FIELDS = frozenset({"itf_submitted_at"})

# A third, distinct exception -- "source" (provenance of contact CREATION,
# e.g. "itf") must be set on a brand-new contact but NEVER touched on an
# update, even if it's currently empty. This is stricter than the default
# fill-only-if-empty rule (which WOULD fill it in from empty on an update --
# wrong here, since an existing contact's source describes how it was
# ORIGINALLY created, not how it was most recently touched). is_new=True
# already sets every mapped field directly (see apply_import_mapping above),
# so this set only changes behavior on the is_new=False path.
CREATE_ONLY_FIELD_NAMES = frozenset({"source"})


def _union_merge_list(existing: list[str], incoming: list[str]) -> list[str]:
    """Order-preserving, deduplicated list union -- the exact logic
    `_apply_custom_field` already uses for multi-select custom fields, extracted
    here so both call sites share one implementation instead of two copies."""
    return existing + [v for v in incoming if v not in existing]


def _union_merge_delimited_text(existing: str | None, incoming: str, separator: str = DELIMITED_TEXT_SEPARATOR) -> str:
    """Same union-merge idea as `_union_merge_list`, but for a scalar TEXT field that
    represents a delimited set of raw strings (e.g. thesis_dietary_preferences_other)
    rather than a real list field. An existing value is never removed or overwritten;
    a new value already present (by exact string match) is never duplicated."""
    existing_tokens = [t.strip() for t in (existing or "").split(separator) if t.strip()]
    incoming_tokens = [t.strip() for t in incoming.split(separator) if t.strip()]
    return separator.join(_union_merge_list(existing_tokens, incoming_tokens))


def _is_empty(value: Any) -> bool:
    """None, "", [], {} count as empty. False and 0 do NOT -- they're real answers."""
    if value is None:
        return True
    if isinstance(value, (str, list, dict, tuple, set)):
        return len(value) == 0
    return False


class CrmContactNotFound(Exception):
    def __init__(self, crm_contact_id: str):
        self.crm_contact_id = crm_contact_id
        super().__init__(f"CRM contact not found: {crm_contact_id}")


class CrmCustomFieldNotFound(Exception):
    def __init__(self, crm_custom_field_id: str):
        self.crm_custom_field_id = crm_custom_field_id
        super().__init__(f"CRM custom field not found: {crm_custom_field_id}")


class CrmDuplicateFieldKeyError(Exception):
    def __init__(self, field_key: str):
        self.field_key = field_key
        super().__init__(f"A custom field with key '{field_key}' already exists")


class CrmContactListNotFound(Exception):
    def __init__(self, list_id: str):
        self.list_id = list_id
        super().__init__(f"CRM contact list not found: {list_id}")


class CrmService:
    def __init__(
        self,
        contact_store: CrmContactStore | None = None,
        custom_field_store: CrmCustomFieldStore | None = None,
        list_store: CrmContactListStore | None = None,
        list_member_store: CrmContactListMemberStore | None = None,
    ):
        self.contact_store = contact_store or MemoryCrmContactStore()
        self.custom_field_store = custom_field_store or MemoryCrmCustomFieldStore()
        self.list_store = list_store or MemoryCrmContactListStore()
        self.list_member_store = list_member_store or MemoryCrmContactListMemberStore()

    # --- Contacts: manual CRUD ---

    async def _require_contact(self, crm_contact_id: str) -> CrmContact:
        contact = await self.contact_store.get(crm_contact_id)
        if contact is None:
            raise CrmContactNotFound(crm_contact_id)
        return contact

    async def create_contact_from_import(self, mapped_fields: dict[str, Any]) -> CrmContact:
        """Used by crm_import_service.py for `create` decisions -- reuses apply_import_mapping's
        is_new=True branch against a blank skeleton, so create and update share one merge path."""
        now = datetime.now(timezone.utc)
        blank = CrmContact(crm_contact_id=str(uuid.uuid4()), created_at=now, updated_at=now)
        contact = self.apply_import_mapping(blank, mapped_fields, is_new=True)
        await self.contact_store.create(contact)
        return contact

    async def create_contact(self, fields: dict[str, Any]) -> CrmContact:
        """
        Manual creation. Light duplicate protection on the three confident
        dedup tiers only (email/apollo_contact_id/linkedin_url) -- the
        fallback name+company tier is deliberately NOT checked here, since
        that tier only exists to flag an IMPORT row for human review, not
        to block a human who is already, directly, creating a record.
        """
        existing = (
            await self._match_by_email(fields.get("email"))
            or await self._match_by_apollo_contact_id(fields.get("apollo_contact_id"))
            or await self._match_by_linkedin(fields.get("linkedin_url"))
        )
        if existing is not None:
            raise ValueError(f"A CRM contact already exists with this identifier: {existing.crm_contact_id}")

        if not fields.get("thesis_investor_mode_manual_override", False):
            fields = {**fields, "thesis_investor_mode": derive_investor_mode(fields.get("custom_fields", {}).get("investor_type"))}

        now = datetime.now(timezone.utc)
        contact = CrmContact(crm_contact_id=str(uuid.uuid4()), created_at=now, updated_at=now, **fields)
        await self.contact_store.create(contact)
        return contact

    async def get_contact(self, crm_contact_id: str) -> CrmContact:
        return await self._require_contact(crm_contact_id)

    async def update_contact(self, crm_contact_id: str, patch: dict[str, Any]) -> CrmContact:
        """
        Direct partial update -- every key in `patch` is set as given, no
        merge rule, EXCEPT `custom_fields` itself: since it's a dict
        containing many independent field_key entries, a caller sending
        just the one key they changed must not wipe out every other
        custom field's value. Shallow-merged onto the existing dict
        before applying, same as the real frontend edit page already
        does client-side -- this makes that safety guaranteed at the
        service layer too, not just a property of how the UI happens to
        submit its state today.

        thesis_investor_mode gets similar treatment, but recomputes ONLY
        when there's an actual reason to -- either (a) the effective
        (post-merge) `investor_type` value genuinely differs from what the
        contact already had, or (b) `thesis_investor_mode_manual_override`
        is transitioning True -> False this call (the documented "resume
        automation" moment -- see test_turning_override_back_off_resumes_automation).
        A PATCH that never mentions custom_fields, or whose custom_fields
        doesn't change investor_type (e.g. editing Notes, Investment
        Industry, Check Size, Company -- anything else), must never
        recompute at all. This matters most for ITF-created contacts, whose
        custom_fields has no "investor_type" key in the first place
        (ITF sets thesis_investor_mode from a different question entirely,
        never through investor_type): before this guard, ANY unrelated edit
        to such a contact -- via this method directly OR via the frontend's
        full-object PATCH /crm/contacts/{id} save, which always resubmits
        the complete custom_fields dict -- would silently recompute
        derive_investor_mode(None) -> None and erase a valid
        thesis_investor_mode, with zero relationship to what was actually
        edited. Flipping thesis_investor_mode_manual_override to True
        remains the one explicit, unambiguous way to make this method leave
        the field alone entirely, regardless of any of the above.
        """
        contact = await self._require_contact(crm_contact_id)
        if "custom_fields" in patch:
            patch = {**patch, "custom_fields": {**contact.custom_fields, **patch["custom_fields"]}}
        manual_override = patch.get("thesis_investor_mode_manual_override", contact.thesis_investor_mode_manual_override)
        if not manual_override:
            effective_custom_fields = patch.get("custom_fields", contact.custom_fields)
            investor_type_changed = (
                effective_custom_fields.get("investor_type") != contact.custom_fields.get("investor_type")
            )
            override_just_resumed = contact.thesis_investor_mode_manual_override and not manual_override
            if investor_type_changed or override_just_resumed:
                patch = {**patch, "thesis_investor_mode": derive_investor_mode(effective_custom_fields.get("investor_type"))}
        updated = contact.model_copy(update={**patch, "updated_at": datetime.now(timezone.utc)})
        await self.contact_store.save(updated)
        return updated

    async def archive_contact(self, crm_contact_id: str) -> CrmContact:
        """Soft-delete only -- never hard-deleted, matching this app's archive convention."""
        return await self.update_contact(crm_contact_id, {"archived": True})

    async def list_contacts(
        self,
        q: str | None = None,
        city: str | None = None,
        state: str | None = None,
        country: str | None = None,
        company: str | None = None,
        industry: str | None = None,
        deal_stage: str | None = None,
        check_size: str | None = None,
        investor_mode: str | None = None,
        email_status: str | None = None,
        include_archived: bool = False,
        page: int = 1,
        page_size: int = 50,
    ) -> CrmContactPage:
        """
        Filtering AND pagination both happen here, in the service layer --
        the caller (route, frontend) only ever receives the one page it
        asked for plus a total count, never the full filtered set. The
        filtering itself is still a Python scan over store.list() (same
        convention as LeadService -- no search index at the store layer);
        fine at the scale this CRM is expected to run at, would need real
        SQL WHERE/LIKE querying if this grows to tens of thousands of
        rows -- but that ceiling is independent of pagination itself,
        which is correct regardless of how the filtering underneath it
        is implemented.

        `industry`/`deal_stage`/`check_size` match against EITHER the
        private or institutional thesis list -- a contact matches if
        either context contains the value, which is what a filter like
        "invests in SaaS" should mean regardless of which section they
        answered it in.
        """
        contacts = await self.contact_store.list()
        if not include_archived:
            contacts = [c for c in contacts if not c.archived]

        def matches(c: CrmContact) -> bool:
            if city and (c.city or "").lower() != city.lower():
                return False
            if state and (c.state or "").lower() != state.lower():
                return False
            if country and (c.country or "").lower() != country.lower():
                return False
            if company and company.lower() not in (c.company or "").lower():
                return False
            if email_status and (c.email_status or "").lower() != email_status.lower():
                return False
            if investor_mode and (c.thesis_investor_mode or "").lower() != investor_mode.lower():
                return False
            if industry and not self._thesis_list_contains(c, "industries", industry):
                return False
            if deal_stage and not self._thesis_list_contains(c, "deal_stages", deal_stage):
                return False
            if check_size and not self._check_size_contains(c, check_size):
                return False
            if q:
                haystack = self._searchable_text(c)
                if q.lower() not in haystack:
                    return False
            return True

        filtered = [c for c in contacts if matches(c)]
        total = len(filtered)

        page = max(page, 1)
        page_size = max(page_size, 1)
        start = (page - 1) * page_size
        items = filtered[start : start + page_size]

        return CrmContactPage(items=items, total=total, page=page, page_size=page_size)

    @staticmethod
    def _thesis_list_contains(contact: CrmContact, suffix: str, value: str) -> bool:
        private_list = getattr(contact, f"thesis_private_{suffix}", [])
        institutional_list = getattr(contact, f"thesis_institutional_{suffix}", [])
        value_lower = value.lower()
        return any(value_lower in v.lower() for v in [*private_list, *institutional_list])

    @staticmethod
    def _check_size_contains(contact: CrmContact, value: str) -> bool:
        """
        Check Size deliberately does NOT use _thesis_list_contains -- as of the
        2026-08-06 Check Size consolidation, check_size_personal/
        check_size_institutional (custom fields) are the sole canonical
        destinations; thesis_private_check_sizes/thesis_institutional_check_sizes
        are deprecated and contain zero data not already present in the custom
        fields (confirmed by full production audit), so checking them here would
        only risk this filter drifting out of sync with reality again. Matches a
        contact if EITHER the personal or institutional custom field contains the
        value, same "either context" semantics as _thesis_list_contains.
        """
        personal = contact.custom_fields.get("check_size_personal") or []
        institutional = contact.custom_fields.get("check_size_institutional") or []
        value_lower = value.lower()
        return any(value_lower in v.lower() for v in [*personal, *institutional])

    # Every thesis field that's a list[str] of selections -- flattened into free-text
    # search so "SaaS" finds a contact whose thesis_private_industries contains
    # "SaaS / Software Infrastructure", not just contacts with "SaaS" in their city/company.
    _THESIS_LIST_FIELD_NAMES = tuple(
        name for name in THESIS_FIELD_NAMES if name.startswith(("thesis_private_", "thesis_institutional_")) and not name.endswith("_other")
    )

    @classmethod
    def _searchable_text(cls, contact: CrmContact) -> str:
        parts: list[str] = [
            contact.first_name or "",
            contact.last_name or "",
            contact.email or "",
            contact.linkedin_url or "",
            contact.company or "",
            contact.title or "",
            contact.city or "",
            contact.state or "",
            contact.country or "",
            contact.thesis_cities or "",
            contact.thesis_private_other_criteria or "",
            contact.thesis_institutional_other_criteria or "",
        ]
        for field_name in cls._THESIS_LIST_FIELD_NAMES:
            value = getattr(contact, field_name, None)
            if isinstance(value, list):
                parts.extend(value)
            elif isinstance(value, str):
                parts.append(value)
        for value in contact.custom_fields.values():
            if isinstance(value, str):
                parts.append(value)
            elif isinstance(value, list):
                parts.extend(v for v in value if isinstance(v, str))
        return " ".join(parts).lower()

    # --- Custom field definitions ---

    async def create_custom_field(
        self,
        field_key: str,
        label: str,
        field_type: CustomFieldType,
        description: str | None = None,
        options: list[str] | None = None,
        required: bool = False,
    ) -> CrmCustomFieldDefinition:
        if await self.custom_field_store.get_by_field_key(field_key) is not None:
            raise CrmDuplicateFieldKeyError(field_key)
        now = datetime.now(timezone.utc)
        definition = CrmCustomFieldDefinition(
            crm_custom_field_id=str(uuid.uuid4()),
            field_key=field_key,
            label=label,
            description=description,
            field_type=field_type,
            options=options or [],
            required=required,
            active=True,
            created_at=now,
            updated_at=now,
        )
        await self.custom_field_store.create(definition)
        return definition

    async def update_custom_field(self, crm_custom_field_id: str, patch: dict[str, Any]) -> CrmCustomFieldDefinition:
        definition = await self.custom_field_store.get(crm_custom_field_id)
        if definition is None:
            raise CrmCustomFieldNotFound(crm_custom_field_id)
        updated = definition.model_copy(update={**patch, "updated_at": datetime.now(timezone.utc)})
        await self.custom_field_store.save(updated)
        return updated

    async def list_custom_fields(self, include_inactive: bool = True) -> list[CrmCustomFieldDefinition]:
        definitions = await self.custom_field_store.list()
        if include_inactive:
            return definitions
        return [d for d in definitions if d.active]

    # --- More Filters: dynamic field registry + query engine (crm_filter_service.py) ---
    # Deferred import to avoid a circular import -- crm_filter_service.py imports
    # `_is_empty` from this module, so it can't be imported at this module's top level.

    async def get_filterable_fields(self) -> list[FilterFieldMeta]:
        from app.services.crm_filter_service import build_registry

        active_custom_fields = await self.list_custom_fields(include_inactive=False)
        return build_registry(active_custom_fields)

    async def query_contacts(self, query: FilterQuery) -> CrmContactPage:
        """
        The More Filters engine's entry point -- entirely additive, does not touch
        list_contacts()/GET /crm/contacts above, which keeps backing the existing
        Contacts page unmodified. Field/operator/value are validated against the
        live registry before any predicate runs, so no unrecognized field name or
        operator (and no raw SQL) is ever accepted from the caller.
        """
        from app.services.crm_filter_service import query_contacts as _query_contacts, validate_query

        registry = await self.get_filterable_fields()
        field_by_key = validate_query(query, registry)
        contacts = await self.contact_store.list()
        return _query_contacts(contacts, query, field_by_key)

    # --- Lists: named, persistent groupings of existing contacts (2026-08-11) ---
    #
    # A list never holds contact data of its own -- only a reference
    # (crm_contact_id) via CrmContactListMemberStore. Every method below that
    # returns contacts re-fetches them live from contact_store, so an edit to
    # a contact is visible in every list it's in with no extra step, and
    # neither adding/removing membership nor deleting a list EVER writes to
    # contact_store -- the contact's own updated_at is never touched by any
    # of this.

    async def _require_contact_list(self, list_id: str) -> CrmContactList:
        contact_list = await self.list_store.get(list_id)
        if contact_list is None:
            raise CrmContactListNotFound(list_id)
        return contact_list

    async def list_contact_lists(self) -> list[CrmContactListSummary]:
        lists = await self.list_store.list()
        counts = await self.list_member_store.count_by_list()
        return [
            CrmContactListSummary(**contact_list.model_dump(), contact_count=counts.get(contact_list.list_id, 0))
            for contact_list in lists
        ]

    async def create_contact_list(self, name: str, description: str | None = None) -> CrmContactListSummary:
        now = datetime.now(timezone.utc)
        contact_list = CrmContactList(
            list_id=str(uuid.uuid4()), name=name, description=description, created_at=now, updated_at=now
        )
        await self.list_store.create(contact_list)
        return CrmContactListSummary(**contact_list.model_dump(), contact_count=0)

    async def get_contact_list(self, list_id: str) -> CrmContactListSummary:
        contact_list = await self._require_contact_list(list_id)
        counts = await self.list_member_store.count_by_list()
        return CrmContactListSummary(**contact_list.model_dump(), contact_count=counts.get(list_id, 0))

    async def update_contact_list(self, list_id: str, patch: dict[str, Any]) -> CrmContactListSummary:
        """Rename/description edit only -- `list_id`/`created_at` are never
        accepted from a patch even if present in the body (model_copy below
        only ever applies name/description/updated_at)."""
        contact_list = await self._require_contact_list(list_id)
        allowed = {k: v for k, v in patch.items() if k in ("name", "description")}
        updated = contact_list.model_copy(update={**allowed, "updated_at": datetime.now(timezone.utc)})
        await self.list_store.save(updated)
        counts = await self.list_member_store.count_by_list()
        return CrmContactListSummary(**updated.model_dump(), contact_count=counts.get(list_id, 0))

    async def delete_contact_list(self, list_id: str) -> CrmContactListSummary:
        """Permanently deletes the list and every membership row that pointed at
        it. Never touches crm_contacts -- every contact that was a member
        continues to exist, unarchived and unedited, exactly as before.
        Returns the summary as it was immediately before deletion (the route
        has nothing else to hand back, and every other route in this API
        returns JSON on success -- no 204s)."""
        summary = await self.get_contact_list(list_id)
        await self.list_member_store.remove_all_for_list(list_id)
        await self.list_store.delete(list_id)
        return summary

    async def get_list_contacts(self, list_id: str, page: int = 1, page_size: int = 50) -> CrmContactPage:
        """Same server-side pagination convention as list_contacts()/query_contacts()
        above -- the caller never has to fetch more than the one page it asked for.
        No sort applied (same as GET /crm/contacts) -- items come back in
        contact_store's own (created_at) order, filtered down to this list's members."""
        await self._require_contact_list(list_id)
        member_ids = set(await self.list_member_store.list_contact_ids_for_list(list_id))
        contacts = await self.contact_store.list()
        matching = [c for c in contacts if c.crm_contact_id in member_ids]

        total = len(matching)
        page = max(page, 1)
        page_size = max(page_size, 1)
        start = (page - 1) * page_size
        items = matching[start : start + page_size]
        return CrmContactPage(items=items, total=total, page=page, page_size=page_size)

    async def bulk_add_to_list(self, list_id: str, contact_ids: list[str]) -> CrmListBulkAddResult:
        """Reports added/already_member/not_found rather than raising on any of
        those -- a bulk action against hundreds or thousands of ids should never
        hard-fail the whole request over one bad id or one repeat. `not_found`
        ids are silently skipped (never create a membership row with no real
        contact behind it). Duplicate ids within `contact_ids` itself are
        collapsed first, order-preserving."""
        await self._require_contact_list(list_id)
        added = already_member = not_found = 0
        now = datetime.now(timezone.utc)
        for contact_id in dict.fromkeys(contact_ids):
            if await self.contact_store.get(contact_id) is None:
                not_found += 1
                continue
            is_new = await self.list_member_store.add(
                CrmContactListMembership(list_id=list_id, crm_contact_id=contact_id, added_at=now)
            )
            if is_new:
                added += 1
            else:
                already_member += 1
        return CrmListBulkAddResult(added=added, already_member=already_member, not_found=not_found)

    async def bulk_remove_from_list(self, list_id: str, contact_ids: list[str]) -> CrmListBulkRemoveResult:
        await self._require_contact_list(list_id)
        removed = 0
        for contact_id in dict.fromkeys(contact_ids):
            if await self.list_member_store.remove(list_id, contact_id):
                removed += 1
        return CrmListBulkRemoveResult(removed=removed)

    async def remove_contact_from_list(self, list_id: str, contact_id: str) -> CrmContactListSummary:
        """Idempotent -- removing a contact that isn't currently a member is a
        no-op, not a 404, since the end state (not a member) is already true.
        Returns the list's updated summary (contact_count reflects the removal)
        so the caller gets a fresh count with no extra request."""
        await self._require_contact_list(list_id)
        await self.list_member_store.remove(list_id, contact_id)
        return await self.get_contact_list(list_id)

    # --- Backup/export ---

    async def export_backup(self) -> dict[str, Any]:
        """
        A full, portable JSON snapshot of every contact and custom field
        definition -- meant to be taken before any bulk/migration
        operation (see crm_migration.py) so it's always reversible by
        restoring from this export, independent of whatever the
        migration itself already preserves.
        """
        contacts = await self.contact_store.list()
        custom_fields = await self.custom_field_store.list()
        return {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "contacts": [c.model_dump(mode="json") for c in contacts],
            "custom_fields": [f.model_dump(mode="json") for f in custom_fields],
        }

    # --- Dedup + import merge (shared with crm_import_service.py) ---

    async def _match_by_email(self, email: str | None) -> CrmContact | None:
        normalized = normalize_email(email)
        return await self.contact_store.get_by_email(normalized) if normalized else None

    async def _match_by_apollo_contact_id(self, apollo_contact_id: str | None) -> CrmContact | None:
        return await self.contact_store.get_by_apollo_contact_id(apollo_contact_id) if apollo_contact_id else None

    async def _match_by_linkedin(self, linkedin_url: str | None) -> CrmContact | None:
        normalized = normalize_linkedin_url(linkedin_url)
        return await self.contact_store.get_by_linkedin_url(normalized) if normalized else None

    @staticmethod
    def _conflicts_on_identity(mapped_fields: dict[str, Any], existing: CrmContact) -> bool:
        """
        True only if the incoming row's first AND last name BOTH disagree
        with the matched contact's -- e.g. an email/apollo_contact_id/
        linkedin_url match found a real existing contact, but the row is
        actually about a completely different, unrelated person (a source-
        data error: one row's identifier column holds someone else's
        value). Confirmed real shape in the 2026-08-06 two-CSV audit -- one
        contact's email column literally held a different person's address
        (James Feldkamp / Shawn Riely -- no name overlap at all).

        Deliberately NOT triggered by a partial mismatch (e.g. same first
        name, different last name -- Carlos Oviedo's CSV export also
        contained a "Carlos Cardenas" row under his email, likely a
        nickname/data-entry variant rather than a different person): that's
        exactly the ambiguous case this module's merge rule already handles
        safely on its own (a populated external field is never overwritten
        regardless of identity, so a partial-match row can still safely
        contribute new custom-field data like Dinner Subscriptions without
        risking a wrong scalar-field overwrite). If either side has no name
        at all, there's nothing to conflict on either.
        """
        incoming_first = (mapped_fields.get("first_name") or "").strip().lower()
        incoming_last = (mapped_fields.get("last_name") or "").strip().lower()
        existing_first = (existing.first_name or "").strip().lower()
        existing_last = (existing.last_name or "").strip().lower()
        if not (incoming_first or incoming_last) or not (existing_first or existing_last):
            return False
        return incoming_first != existing_first and incoming_last != existing_last

    async def classify_match(
        self, mapped_fields: dict[str, Any]
    ) -> tuple[CrmImportRowStatus, CrmContact | None, str | None]:
        """
        The dedup hierarchy, in order. The first three tiers are exact,
        normalized, confident matches -> EXISTING (safe to auto-update) --
        UNLESS the row's name conflicts with the matched contact's name, in
        which case it's downgraded to POSSIBLE_DUPLICATE (see
        _conflicts_on_identity): a shared identifier does not make it safe
        to auto-merge a row that's actually about someone else. The fourth
        tier is exact-normalized but NOT confident -> POSSIBLE_DUPLICATE,
        always surfaced for human review, never auto-merged. No fuzzy
        matching anywhere in this chain.
        """
        match = await self._match_by_email(mapped_fields.get("email"))
        if match:
            if self._conflicts_on_identity(mapped_fields, match):
                return CrmImportRowStatus.POSSIBLE_DUPLICATE, match, "email_conflicting_identity"
            return CrmImportRowStatus.EXISTING, match, "email"

        match = await self._match_by_apollo_contact_id(mapped_fields.get("apollo_contact_id"))
        if match:
            if self._conflicts_on_identity(mapped_fields, match):
                return CrmImportRowStatus.POSSIBLE_DUPLICATE, match, "apollo_contact_id_conflicting_identity"
            return CrmImportRowStatus.EXISTING, match, "apollo_contact_id"

        match = await self._match_by_linkedin(mapped_fields.get("linkedin_url"))
        if match:
            if self._conflicts_on_identity(mapped_fields, match):
                return CrmImportRowStatus.POSSIBLE_DUPLICATE, match, "linkedin_url_conflicting_identity"
            return CrmImportRowStatus.EXISTING, match, "linkedin_url"

        normalized_name_company = normalize_name_company(
            mapped_fields.get("first_name"), mapped_fields.get("last_name"), mapped_fields.get("company")
        )
        if normalized_name_company:
            candidates = await self.contact_store.find_by_name_and_company(normalized_name_company)
            if candidates:
                return CrmImportRowStatus.POSSIBLE_DUPLICATE, candidates[0], "name_company"

        return CrmImportRowStatus.NEW, None, None

    def apply_import_mapping(
        self, contact: CrmContact, mapped_fields: dict[str, Any], is_new: bool
    ) -> CrmContact:
        """
        Builds the merged contact for a create/update decision. `is_new`
        contacts get every mapped field set directly (nothing exists yet
        to protect). Existing contacts follow the merge rule described in
        this module's docstring.
        """
        updates: dict[str, Any] = {}
        for field_name, incoming_value in mapped_fields.items():
            if field_name.startswith(CUSTOM_FIELD_PREFIX):
                self._apply_custom_field(contact, updates, field_name[len(CUSTOM_FIELD_PREFIX) :], incoming_value, is_new)
                continue
            if field_name not in EXTERNAL_FIELD_NAMES and field_name not in THESIS_FIELD_NAMES:
                continue  # unmapped/unknown target -- ignored, never guessed at

            if is_new:
                if not _is_empty(incoming_value):
                    updates[field_name] = incoming_value
                continue

            # See CREATE_ONLY_FIELD_NAMES above -- never set on an update, regardless
            # of whether the existing value is currently empty.
            if field_name in CREATE_ONLY_FIELD_NAMES:
                continue

            # thesis_dietary_preferences (and its _other companion) are the one
            # exception to "fill only when empty" -- see UNION_MERGE_THESIS_LIST_FIELDS
            # above. Checked before the generic rule so every other thesis/external
            # field's fill-only behavior is completely unaffected.
            if field_name in UNION_MERGE_THESIS_LIST_FIELDS and isinstance(incoming_value, list):
                current_list = getattr(contact, field_name) or []
                merged_list = _union_merge_list(current_list, incoming_value)
                if merged_list != current_list:
                    updates[field_name] = merged_list
                continue
            if field_name in UNION_MERGE_DELIMITED_TEXT_FIELDS and isinstance(incoming_value, str):
                current_text = getattr(contact, field_name)
                merged_text = _union_merge_delimited_text(current_text, incoming_value)
                if merged_text != (current_text or ""):
                    updates[field_name] = merged_text
                continue

            # External AND thesis fields alike -- never overwrite an existing value,
            # whether the incoming value is blank or a genuinely different one. See
            # this module's docstring for why external fields no longer get the old
            # "overwrite on any non-empty incoming value" treatment.
            if _is_empty(getattr(contact, field_name)) and not _is_empty(incoming_value):
                updates[field_name] = incoming_value

        if "source_snapshot" in mapped_fields or is_new:
            updates["source_snapshot"] = mapped_fields.get("source_snapshot", contact.source_snapshot)

        updates["updated_at"] = datetime.now(timezone.utc)
        return contact.model_copy(update=updates)

    def _apply_custom_field(
        self, contact: CrmContact, updates: dict[str, Any], field_key: str, incoming_value: Any, is_new: bool
    ) -> None:
        """
        Custom fields follow the SAME protection rule as thesis fields --
        they're ours too. Builds on `updates["custom_fields"]` if an
        earlier custom field in this same call already started it --
        otherwise every custom field but the last would be lost.

        Multi-select fields (incoming_value is a list -- the shape
        _coerce_value()/classification rules always produce for a
        MULTI_SELECT custom field) are the one exception to "only fill
        when empty": an existing selection list is UNION-MERGED with the
        incoming one (order-preserving, deduplicated) rather than only
        filled in from empty. This is what lets a re-imported contact pick
        up a newly-added Dinner Subscription or a newly-attended dinner
        without ever dropping a selection that was already there --
        replacing the list outright would silently erase whatever the
        incoming row didn't happen to repeat.
        """
        base = updates.get("custom_fields", contact.custom_fields)
        current = base.get(field_key)

        if is_new:
            if not _is_empty(incoming_value):
                updates["custom_fields"] = {**base, field_key: incoming_value}
            return

        if isinstance(incoming_value, list):
            existing_list = current if isinstance(current, list) else []
            merged = _union_merge_list(existing_list, incoming_value)
            if merged != existing_list:
                updates["custom_fields"] = {**base, field_key: merged}
            return

        # See LATEST_WINS_CUSTOM_FIELDS above -- these always take the incoming value
        # (when non-empty), never gated on whether one is already set.
        if field_key in LATEST_WINS_CUSTOM_FIELDS:
            if not _is_empty(incoming_value) and incoming_value != current:
                updates["custom_fields"] = {**base, field_key: incoming_value}
            return

        if _is_empty(current) and not _is_empty(incoming_value):
            updates["custom_fields"] = {**base, field_key: incoming_value}
