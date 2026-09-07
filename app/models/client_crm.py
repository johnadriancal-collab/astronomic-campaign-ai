"""
Pydantic models for Client CRM (Stage 1A, 2026-09-07) -- Astronomic's
future system of record for company/client relationships, distinct from
the existing CRM (app/models/crm.py), which is our people/prospect/guest/
investor contact database. A Client is not a CrmContact, and a Client is
not a dinner: one Client may have many ClientContacts and many
Engagements over time.

Stage 1A is deliberately execution-inert: models + stores only. No API
route, service, or frontend reads or writes any of this yet -- see each
model's own docstring for what is explicitly NOT built yet and why.

Scope (approved architecture, see the Client CRM investigation report):
Client -> ClientContact -> Engagement -> ClientNote. Deal/Pipeline is
explicitly excluded from Stage 1A -- deliberately not even a reserved
field on Engagement yet (Deal's own domain hasn't been designed, and
guessing its eventual relationship shape now risks getting it wrong; the
JSON-blob architecture makes adding a nullable Deal relationship later a
trivial, non-breaking addition once that design exists).
"""

from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ClientStatus(str, Enum):
    """Coarse, operational: "are we currently engaged with this Client at
    all." Deliberately separate from ClientRelationshipClassification
    (the SOP's qualitative categorization of HOW that engagement is
    going) -- the two answer different questions and are never derived
    from one another. Also distinct from `archived` (Client.archived):
    INACTIVE is still a real, visible historical record; archived is this
    app's usual hide-it-from-default-views soft-delete, e.g. a Client
    created by mistake or merged into another one."""

    ACTIVE = "active"
    INACTIVE = "inactive"


class ClientRelationshipClassification(str, Enum):
    """The SOP's own eventual categories (Post-Dinner Client Follow-Up SOP
    Framework) -- a qualitative, judgment-based trajectory expected to
    change over time as Day 3/30/90 follow-ups reveal new signal. `None`
    on Client.relationship_classification (not a member here) means
    "not yet assessed" -- a freshly-created Client has not been through
    any follow-up cycle yet, and defaulting to NURTURE would be a guess
    this model has no basis to make."""

    NURTURE = "nurture"
    OPPORTUNITY = "opportunity"
    REFERRAL = "referral"
    NEEDS_ATTENTION = "needs_attention"
    CLOSED_INACTIVE = "closed_inactive"


class Client(BaseModel):
    """The durable company/account relationship record -- e.g. "Hive
    ASMBLD". Owns nothing else directly; ClientContact/Engagement/
    ClientNote each hold their own `client_id` foreign key back to this.

    No Deal/pipeline field exists here in Stage 1A by design -- a Client
    can be created directly (e.g. entered by hand once a relationship
    starts) independent of any future sales-pipeline concept."""

    client_id: str
    name: str
    website: str | None = None
    industry: str | None = None
    status: ClientStatus = ClientStatus.ACTIVE
    relationship_classification: ClientRelationshipClassification | None = None
    owner: str | None = None  # free text, no auth-identity system exists yet -- same
    # placeholder convention as ActivityEvent.actor; a real user FK can replace this
    # later without a data migration (still just a string column in the JSON blob).
    next_action: str | None = None
    next_action_due: date | None = None
    created_at: datetime
    updated_at: datetime
    archived: bool = False  # soft-delete only, matching CrmContact.archived's own convention


class ClientContact(BaseModel):
    """A person in their capacity as a client-side stakeholder for one
    specific Client -- e.g. the primary decision-maker at Hive ASMBLD.
    Deliberately its own record rather than adding client-relationship
    fields (title-at-this-client, is_decision_maker, etc.) directly onto
    CrmContact, which would pollute that model with fields meaningless to
    the vast majority of CRM contacts who will never become a client.

    `crm_contact_id` is an OPTIONAL link to an existing CrmContact --
    approved architecture: reference by id, never a second independent
    identity system. name/email/phone are stored as SNAPSHOTS here (not
    live-joined against CrmContact) so a ClientContact's relationship
    record keeps rendering correctly even if the linked CrmContact is
    later archived, edited, or (in principle) removed -- same "snapshot,
    not live reference" instinct already used by ActivityEvent.entity_name
    and CrmContact.source_snapshot elsewhere in this codebase.

    Stage 1A does NOT implement the CRM dedup/linking flow -- creating a
    ClientContact here never checks CrmContactStore for a match, never
    calls CrmService.classify_match(), and never auto-populates the
    snapshot fields from an existing CrmContact. That entire flow is
    Stage 1D's own investigation-and-implementation. This model only
    needs to durably HOLD `crm_contact_id` once that flow exists -- which
    it already does."""

    client_contact_id: str
    client_id: str
    crm_contact_id: str | None = None
    first_name: str | None = None  # snapshot -- mirrors CrmContact's own first_name/last_name split
    last_name: str | None = None  # snapshot
    email: str | None = None  # snapshot
    phone: str | None = None  # snapshot
    title: str | None = None  # role/title AT THIS CLIENT -- e.g. "VP of BD" -- distinct from
    # any title CrmContact might separately hold, since a person's role in this specific
    # client relationship is not necessarily their general job title.
    is_primary_contact: bool = False
    is_decision_maker: bool = False
    role_notes: str | None = None  # short, relationship-specific context about this person's
    # role in the relationship (e.g. "introduced us to the CFO") -- general/dated activity
    # about this contact belongs in ClientNote instead, not here.
    created_at: datetime
    updated_at: datetime
    archived: bool = False


class EngagementType(str, Enum):
    """Not exhaustive of every future Astronomic service -- OTHER exists
    precisely so a new kind of engagement is never blocked on a code
    change before it can be recorded; a real, named value can be added
    later purely additively (a str Enum's existing stored rows are
    unaffected by adding a new member)."""

    INVESTOR_DINNER = "investor_dinner"
    CUSTOMER_DINNER = "customer_dinner"
    SPONSORSHIP = "sponsorship"
    OTHER = "other"


class EngagementStatus(str, Enum):
    """Deliberately small, matching the user's own explicit instruction --
    no in-progress/no-show/rescheduled sub-states invented here."""

    PLANNED = "planned"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class EngagementContractStatus(str, Enum):
    NOT_SENT = "not_sent"
    SENT = "sent"
    SIGNED = "signed"


class EngagementPaymentStatus(str, Enum):
    UNPAID = "unpaid"
    PARTIAL = "partial"
    PAID = "paid"


class Engagement(BaseModel):
    """One delivered or planned Astronomic engagement for a Client -- an
    investor dinner, a customer dinner, a sponsorship, or (OTHER) some
    future service. A Client may have many Engagements over time; an
    Engagement never implies or requires a single all-time "the deal."

    Deliberately has NO Deal/Pipeline relationship field yet -- Deal
    design was explicitly deferred (see the Client CRM investigation
    report and this stage's own STOP report), and reserving its eventual
    shape (a `deal_id` FK) before that domain is actually designed risks
    guessing wrong about what it should look like. Adding a nullable
    `deal_id` later is a trivial, non-breaking addition given this
    store's JSON-blob architecture (see sqlite_engagement_store.py) --
    there is nothing to migrate.

    Contract/commercial state is tracked here as plain fields ONLY --
    contract_status/contract_url/signed_date/payment_status -- never a
    document management system. `contract_url` points at wherever the
    real document already lives (Drive, DocuSign, etc.); this model never
    stores or serves a file itself.

    `luma_event_id` is a reserved, always-None-today field for a future
    integration point -- Luma (app/models/luma.py) is already a real,
    live Astronomic system (unlike Deal, which doesn't exist yet), so
    this one reserved field is kept -- not read or written by anything in
    Stage 1A."""

    engagement_id: str
    client_id: str
    title: str
    engagement_type: EngagementType
    # NOT named `date` -- a Pydantic field literally named the same as its
    # own `date` type breaks: Python's annotated-assignment evaluation
    # order binds the default value to the name BEFORE evaluating the
    # annotation expression, so `date: date | None = None` resolves the
    # annotation's own `date` reference to the just-bound `None`,
    # producing `None | None` at class-definition time (confirmed via a
    # standalone repro, not a guess). `engagement_date` sidesteps this
    # entirely and is more descriptive besides.
    engagement_date: date | None = None  # planned/actual dinner date -- nullable: a PLANNED
    # engagement may not have a firm date locked in yet.
    location: str | None = None
    status: EngagementStatus = EngagementStatus.PLANNED
    fee: float | None = None
    contract_status: EngagementContractStatus = EngagementContractStatus.NOT_SENT
    contract_url: str | None = None
    signed_date: date | None = None
    payment_status: EngagementPaymentStatus = EngagementPaymentStatus.UNPAID
    luma_event_id: str | None = None
    created_at: datetime
    updated_at: datetime
    archived: bool = False


class ClientNoteType(str, Enum):
    """GENERAL covers ordinary relationship notes. The DAY_* values exist
    so a free-text note can still be tagged to a specific SOP touchpoint
    in Stage 1A, WITHOUT this model owning any structured/due-date/
    completion-state business logic for that touchpoint -- see this
    model's own docstring and the Stage 1A investigation's
    ClientFollowUpRecord recommendation for why that structured
    responsibility is deliberately kept OUT of ClientNote."""

    GENERAL = "general"
    DAY_0 = "day_0"
    DAY_3 = "day_3"
    DAY_30 = "day_30"
    DAY_90 = "day_90"


class ClientNote(BaseModel):
    """A human-authored relationship/activity note -- semantically
    separate from ActivityEventStore (app/models/activity.py), which
    stays a system-authored, non-editable audit trail of create/change/
    delete actions with `actor` always None. A ClientNote is the opposite:
    always human-written content, never emitted by this codebase's own
    automation. Stage 1A does not integrate ActivityLogService at all --
    no ClientNote/Client/ClientContact/Engagement create-or-edit emits an
    ActivityEvent yet; that integration is a later stage's own addition.

    Deliberately NOT the home for structured Day 3/30/90 business data
    (client sentiment, investment outcomes, due dates, completion state)
    -- see this Stage's own STOP report for the full recommendation: a
    future dedicated ClientFollowUpRecord entity is the right home for
    that, once built. ClientNote has no `structured_data` field and none
    should be added later without revisiting that recommendation --
    adding one now, before ClientFollowUpRecord exists, would invite that
    structured data to accumulate here anyway (the exact "shoved into one
    giant text blob" outcome this Stage was asked to avoid), only to need
    an awkward migration off of ClientNote once the dedicated entity
    finally arrives.

    `occurred_at` is the conversation/event's own real-world time (may
    predate `created_at` for a backfilled note); `created_at`/`updated_at`
    are this row's own persistence timestamps, same distinction
    MailTriggerOccurrence and others already draw between "when this
    happened" and "when this row was written."""

    client_note_id: str
    client_id: str
    engagement_id: str | None = None  # None = a general Client-level note, not tied to one dinner
    note_type: ClientNoteType = ClientNoteType.GENERAL
    body: str
    created_by: str | None = None  # free text, no auth-identity system yet -- same placeholder
    # convention as Client.owner / ActivityEvent.actor.
    occurred_at: datetime
    created_at: datetime
    updated_at: datetime
    archived: bool = False
