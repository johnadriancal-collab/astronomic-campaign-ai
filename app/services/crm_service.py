"""
Core CRM service: manual contact CRUD, search/filter, custom field
definitions, and the merge/dedup logic shared by both manual editing and
CSV import (see crm_import_service.py, which calls into
classify_match()/apply_import_mapping() rather than re-implementing them).

Merge rule (the crux of "the CRM is our own source of truth"):
  - External/source fields: an incoming value only overwrites an existing
    one if the incoming value is non-empty. A blank CSV cell never erases
    data we already have.
  - Investor Thesis fields AND custom fields: NEVER automatically
    overwritten if a value already exists. Only filled in when the
    existing value is currently empty. Treated as "ours" -- an import can
    add missing thesis/custom data, never silently replace what's there.
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
    CrmContactPage,
    CrmCustomFieldDefinition,
    CrmImportRowStatus,
    CustomFieldType,
    normalize_email,
    normalize_linkedin_url,
    normalize_name_company,
)
from app.repositories.crm_contact_store import CrmContactNotFoundError, CrmContactStore, MemoryCrmContactStore
from app.repositories.crm_custom_field_store import (
    CrmCustomFieldNotFoundError,
    CrmCustomFieldStore,
    MemoryCrmCustomFieldStore,
)

CUSTOM_FIELD_PREFIX = "custom:"


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


class CrmService:
    def __init__(
        self,
        contact_store: CrmContactStore | None = None,
        custom_field_store: CrmCustomFieldStore | None = None,
    ):
        self.contact_store = contact_store or MemoryCrmContactStore()
        self.custom_field_store = custom_field_store or MemoryCrmCustomFieldStore()

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
        """
        contact = await self._require_contact(crm_contact_id)
        if "custom_fields" in patch:
            patch = {**patch, "custom_fields": {**contact.custom_fields, **patch["custom_fields"]}}
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
            if check_size and not self._thesis_list_contains(c, "check_sizes", check_size):
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

    async def classify_match(
        self, mapped_fields: dict[str, Any]
    ) -> tuple[CrmImportRowStatus, CrmContact | None, str | None]:
        """
        The dedup hierarchy, in order. The first three tiers are exact,
        normalized, confident matches -> EXISTING (safe to auto-update).
        The fourth is exact-normalized but NOT confident -> POSSIBLE_DUPLICATE,
        always surfaced for human review, never auto-merged. No fuzzy
        matching anywhere in this chain.
        """
        match = await self._match_by_email(mapped_fields.get("email"))
        if match:
            return CrmImportRowStatus.EXISTING, match, "email"

        match = await self._match_by_apollo_contact_id(mapped_fields.get("apollo_contact_id"))
        if match:
            return CrmImportRowStatus.EXISTING, match, "apollo_contact_id"

        match = await self._match_by_linkedin(mapped_fields.get("linkedin_url"))
        if match:
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

            if field_name in EXTERNAL_FIELD_NAMES:
                if not _is_empty(incoming_value):
                    updates[field_name] = incoming_value
            else:  # THESIS_FIELD_NAMES -- never overwrite an existing value
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
        """
        base = updates.get("custom_fields", contact.custom_fields)
        current = base.get(field_key)
        if is_new or (_is_empty(current) and not _is_empty(incoming_value)):
            updates["custom_fields"] = {**base, field_key: incoming_value}
