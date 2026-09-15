"""
Sale Bot -> AstroHub onboarding integration (2026-09-15).

Orchestrates the Client/ClientContact/Engagement creation triggered once
astronomic-sale-automation's completion-tracker.js confirms a sale's
DocuSign contract is signed AND its Mercury invoice is paid. See that
repo's src/completion-tracker.js for the trigger itself -- this service
is only ever reached via POST /sync/sale-onboarding
(app/api/sale_onboarding.py), never called directly by anything in this
app.

Idempotency is AstroHub's own responsibility, not the Sale Bot's: this
service is the source of truth for "has this sale already been
onboarded," checked by `sale_id` (primary) and `docusign_envelope_id`
(secondary safeguard) BEFORE any write, so repeated polls, network
retries, a Sale Bot crash/restart, or a lost response after a successful
write all resolve to the same, safe outcome -- either a no-op
"already processed" result, or (for a genuine same-envelope/
different-sale_id conflict) a rejected request, never a duplicate
Client/Engagement.

Explicitly out of scope for this stage (per the approved architecture):
Google Drive/Sheets onboarding steps -- not built here or anywhere yet;
`sale_id` is already the natural shared idempotency key those steps will
use once their own folder/sheet schema is defined.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.models.client_crm import (
    Client,
    ClientContact,
    ClientStatus,
    DinnerType,
    Engagement,
    EngagementContractStatus,
    EngagementPaymentStatus,
    EngagementStatus,
    EngagementType,
    normalize_company_name,
)
from app.models.crm import CrmContact, normalize_email
from app.services.client_crm_service import ClientCrmService
from app.services.crm_service import CrmService

# service_sold -> DinnerType, conservative exact-normalized match only
# (same normalize_company_name() whitespace/case handling as Client
# matching -- reused here for the identical "trim/collapse/casefold,
# nothing fuzzier" reasoning, not because these are company names).
# Deliberately NOT exhaustive and NEVER extended by guessing -- every
# value the /sale form's own known examples use, plus DinnerType's own
# CUSTOM_DINNER member (the one case the approved design explicitly
# allows treating as "explicitly recognizable" rather than unknown).
_KNOWN_DINNER_TYPE_LABELS: dict[str, DinnerType] = {
    "investor dinner": DinnerType.INVESTOR_DINNER,
    "fireside dinner": DinnerType.FIRESIDE_DINNER,
    "bizdev dinner": DinnerType.BIZDEV_DINNER,
    "donor dinner": DinnerType.DONOR_DINNER,
    "custom dinner": DinnerType.CUSTOM_DINNER,
}

# Fixed, explicit set of accepted event_date formats -- deliberately NOT a
# fuzzy/heuristic date parser (e.g. dateutil's fuzzy mode): the /sale
# Slack form's "Tentative event date" field is unvalidated free text a
# human typed, and a fuzzy parser can confidently produce a WRONG date
# from ambiguous input, which is worse than admitting it can't parse one.
# Anything outside this exact list is reported unparsed, never guessed.
_EVENT_DATE_FORMATS = (
    "%m/%d/%Y",
    "%m/%d/%y",
    "%Y-%m-%d",
    "%B %d, %Y",
    "%B %d %Y",
    "%b %d, %Y",
    "%b %d %Y",
)


def normalize_service_sold(service_sold: str | None) -> tuple[EngagementType, DinnerType | None, str | None]:
    """Returns (engagement_type, dinner_type, service_sold_raw). A
    confident, exact-normalized match against _KNOWN_DINNER_TYPE_LABELS
    returns (DINNER, <that type>, None) -- service_sold_raw stays None
    specifically BECAUSE it matched cleanly, so its mere presence on the
    resulting Engagement already signals "this one needs a human look."
    Anything else -- including a value that merely LOOKS like a dinner --
    returns (OTHER, None, <the original raw string>), never a guessed
    DinnerType. service_sold may legitimately describe a future,
    non-dinner Astronomic service; OTHER is exactly what that case
    already means (see EngagementType's own model docstring)."""
    normalized = normalize_company_name(service_sold)
    if normalized is not None and normalized in _KNOWN_DINNER_TYPE_LABELS:
        return EngagementType.DINNER, _KNOWN_DINNER_TYPE_LABELS[normalized], None
    return EngagementType.OTHER, None, service_sold


def parse_event_date(raw: str | None) -> tuple[date | None, str | None]:
    """Returns (engagement_date, event_date_raw). Tries each of
    _EVENT_DATE_FORMATS in turn via datetime.strptime -- the first exact
    match wins; no fuzzy fallback. On success, engagement_date is set and
    event_date_raw stays None (same "raw field only populated when
    normalization failed" convention as normalize_service_sold() above).
    On failure -- including a blank/missing value -- engagement_date stays
    None and event_date_raw carries the original string forward so it is
    never silently dropped; a human sets the real date by hand later."""
    from datetime import datetime as _datetime

    if not raw or not raw.strip():
        return None, raw
    text = raw.strip()
    for fmt in _EVENT_DATE_FORMATS:
        try:
            return _datetime.strptime(text, fmt).date(), None
        except ValueError:
            continue
    return None, raw


def split_full_name(full_name: str | None) -> tuple[str | None, str | None]:
    """primary_contact on the /sale form is one free-text "full name"
    field -- CrmContact needs first_name/last_name separately. Simple,
    explicit split: the first whitespace-separated token is first_name,
    everything else (if any) is last_name. Imperfect for a genuinely
    multi-word first name, but there is no more structured source to draw
    on, and this matches how a person's own name most commonly appears
    (a name with no space at all becomes first_name only, last_name
    None -- never fabricated)."""
    if not full_name or not full_name.strip():
        return None, None
    parts = full_name.strip().split(None, 1)
    if len(parts) == 1:
        return parts[0], None
    return parts[0], parts[1]


@dataclass
class SaleOnboardingPayload:
    sale_id: str
    docusign_envelope_id: str
    mercury_invoice_id: str
    qb_invoice_id: str
    qb_customer_id: str
    qb_amount: float
    client_company: str
    primary_contact: str
    contact_email: str
    signer_name: str
    signer_email: str
    service_sold: str
    city: str
    event_date: str
    internal_owner: str | None = None
    referral_source: str | None = None
    special_terms: str | None = None


@dataclass
class SaleOnboardingResult:
    already_processed: bool
    client_id: str
    engagement_id: str
    client_contact_id: str | None
    crm_contact_id: str | None
    created_new_client: bool
    created_new_contact: bool
    event_date_parsed: bool
    warnings: list[str] = field(default_factory=list)


class SaleOnboardingConflict(Exception):
    """Raised when docusign_envelope_id already exists under a DIFFERENT
    sale_id -- a real conflict (a duplicate/corrupted request), never
    silently resolved by creating a second Engagement for the same signed
    contract. Mapped to a 409 at the API layer."""

    def __init__(self, docusign_envelope_id: str, existing_sale_id: str):
        self.docusign_envelope_id = docusign_envelope_id
        self.existing_sale_id = existing_sale_id
        super().__init__(
            f"docusign_envelope_id {docusign_envelope_id} is already linked to a different sale ({existing_sale_id})."
        )


class SaleOnboardingService:
    def __init__(self, client_crm_service: ClientCrmService, crm_service: CrmService):
        self.client_crm_service = client_crm_service
        self.crm_service = crm_service

    async def onboard(self, payload: SaleOnboardingPayload) -> SaleOnboardingResult:
        # 1. PRIMARY idempotency check -- sale_id. A hit means this exact
        # sale has already been fully onboarded (the Engagement is only
        # ever created after every other step below succeeds), so this is
        # always a clean, safe no-op, regardless of why this request is a
        # repeat (a routine 5-minute re-poll, a network-timeout retry, or
        # a Sale Bot restart that lost track of its own "already
        # triggered" bookkeeping).
        existing = await self.client_crm_service.engagement_store.get_by_sale_id(payload.sale_id)
        if existing is not None:
            client_contact_id, crm_contact_id = await self._existing_link_ids(existing)
            return SaleOnboardingResult(
                already_processed=True,
                client_id=existing.client_id,
                engagement_id=existing.engagement_id,
                client_contact_id=client_contact_id,
                crm_contact_id=crm_contact_id,
                created_new_client=False,
                created_new_contact=False,
                event_date_parsed=existing.engagement_date is not None,
            )

        # 2. SECONDARY safeguard -- docusign_envelope_id must never end up
        # on two different sale_ids. A hit here (necessarily under a
        # DIFFERENT sale_id, since step 1 already ruled out this exact
        # sale_id) is a real conflict, not a retry -- reject rather than
        # silently create a second Engagement for the same contract.
        envelope_conflict = await self.client_crm_service.engagement_store.get_by_docusign_envelope_id(
            payload.docusign_envelope_id
        )
        if envelope_conflict is not None:
            raise SaleOnboardingConflict(payload.docusign_envelope_id, envelope_conflict.sale_id or "")

        warnings: list[str] = []

        # 3. Client: conservative normalized-exact match, get-or-create.
        client, created_new_client = await self._resolve_client(payload.client_company)

        # 4. Contact: normalized-email match, get-or-create via the
        # existing CrmService.create_contact() path (never fuzzy name
        # matching). A race where someone else creates the same email
        # between our lookup and our create attempt is handled by
        # re-fetching rather than failing the whole onboarding call.
        contact, created_new_contact = await self._resolve_contact(payload)

        # 5. ClientContact: idempotent link -- reuse if this exact
        # (client, contact) pairing already exists, never create a
        # second relationship row for the same two records.
        client_contact = await self._resolve_client_contact(client.client_id, contact.crm_contact_id)

        # 6. service_sold -> EngagementType/DinnerType, conservative.
        engagement_type, dinner_type, service_sold_raw = normalize_service_sold(payload.service_sold)

        # 7. event_date -> engagement_date, fail-safe.
        engagement_date, event_date_raw = parse_event_date(payload.event_date)
        if engagement_date is None:
            warnings.append(
                f"event_date {payload.event_date!r} could not be parsed into a real date -- "
                "preserved as event_date_raw; engagement_date left unset."
            )

        # 8. Engagement -- signed+paid is the trigger for this whole call,
        # so contract_status/payment_status are always SIGNED/PAID here.
        # status stays the model's own PLANNED default: the /sale form's
        # own label calls this a "Tentative event date," so nothing about
        # a signed contract implies the date/venue is actually locked.
        engagement = await self.client_crm_service.create_client_engagement(
            client.client_id,
            {
                "title": f"{payload.service_sold} — {payload.city}",
                "engagement_type": engagement_type,
                "dinner_type": dinner_type,
                "engagement_date": engagement_date,
                "event_date_raw": event_date_raw,
                "location": payload.city,
                "status": EngagementStatus.PLANNED,
                "owner": payload.internal_owner,
                "fee": payload.qb_amount,
                "contract_status": EngagementContractStatus.SIGNED,
                "payment_status": EngagementPaymentStatus.PAID,
                "signer_name": payload.signer_name,
                "signer_email": payload.signer_email,
                "service_sold_raw": service_sold_raw,
                "referral_source": payload.referral_source,
                "special_terms": payload.special_terms,
                "sale_id": payload.sale_id,
                "docusign_envelope_id": payload.docusign_envelope_id,
                "mercury_invoice_id": payload.mercury_invoice_id,
                "qb_invoice_id": payload.qb_invoice_id,
                "qb_customer_id": payload.qb_customer_id,
                # contract_url deliberately NOT populated -- see module
                # docstring / SaleOnboardingService.onboard()'s own
                # instructions: not verified that DocuSign provides a
                # stable, non-ephemeral long-term URL, so nothing is set
                # rather than risk an expiring/recipient-authenticated link.
                # signed_date deliberately NOT populated for the same
                # reason -- obtaining it reliably needs a real DocuSign API
                # call this service doesn't make; left for a later, more
                # deliberate addition rather than guessed as "today."
            },
        )

        return SaleOnboardingResult(
            already_processed=False,
            client_id=client.client_id,
            engagement_id=engagement.engagement_id,
            client_contact_id=client_contact.client_contact_id,
            crm_contact_id=contact.crm_contact_id,
            created_new_client=created_new_client,
            created_new_contact=created_new_contact,
            event_date_parsed=engagement_date is not None,
            warnings=warnings,
        )

    async def _resolve_client(self, client_company: str) -> tuple[Client, bool]:
        existing = await self.client_crm_service.find_client_by_normalized_name(client_company)
        if existing is not None:
            return existing, False
        created = await self.client_crm_service.create_client(
            {"name": client_company, "status": ClientStatus.ACTIVE.value}
        )
        return created, True

    async def _resolve_contact(self, payload: SaleOnboardingPayload) -> tuple[CrmContact, bool]:
        normalized = normalize_email(payload.contact_email)
        if normalized is None:
            raise ValueError("contact_email is required and must be a real email address.")

        existing = await self.client_crm_service.crm_contact_store.get_by_email(normalized)
        if existing is not None:
            return existing, False

        first_name, last_name = split_full_name(payload.primary_contact)
        try:
            created = await self.crm_service.create_contact(
                {
                    "first_name": first_name,
                    "last_name": last_name,
                    "email": payload.contact_email,
                    "source": "sale_bot",
                }
            )
            return created, True
        except ValueError:
            # Race: another request created this same email between our
            # lookup above and this create attempt. Re-fetch rather than
            # fail the whole onboarding call -- the contact now exists
            # either way, which is all this step actually needs.
            refetched = await self.client_crm_service.crm_contact_store.get_by_email(normalized)
            if refetched is None:
                raise
            return refetched, False

    async def _resolve_client_contact(self, client_id: str, crm_contact_id: str) -> ClientContact:
        existing_links = await self.client_crm_service.client_contact_store.list_for_crm_contact(crm_contact_id)
        for link in existing_links:
            if link.client_id == client_id and not link.archived:
                return link
        return await self.client_crm_service.create_client_contact(
            client_id, {"crm_contact_id": crm_contact_id, "is_primary_contact": True}
        )

    async def _existing_link_ids(self, engagement: Engagement) -> tuple[str | None, str | None]:
        """Best-effort ClientContact/crm_contact_id lookup for an
        already-processed sale's response -- the original contact/link
        that onboarding created isn't stored ON the Engagement itself (it
        doesn't need to be, for idempotency -- sale_id alone is enough),
        so this re-derives it from the Client's own primary contact for a
        more useful already-processed response. Never raises -- a miss
        here just means the response's contact fields come back None,
        which is fine since the caller's own next step doesn't depend on
        them."""
        contacts = await self.client_crm_service.client_contact_store.list_for_client(engagement.client_id)
        primary = next((c for c in contacts if c.is_primary_contact), None)
        if primary is None:
            return None, None
        return primary.client_contact_id, primary.crm_contact_id
