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
from typing import Annotated, Any

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


class ClientListItem(Client):
    """Client CRM Stage 2C -- exactly a Client plus two READ-ONLY derived
    summary columns for the Master Client CRM table, never persisted and
    never accepted on any create/update request (only ClientPage's own
    list_clients() response uses this type -- GET /clients/{id} and every
    create/update route keep returning plain Client, unaugmented).

    `next_dinner`: the earliest still-upcoming, qualifying Engagement's
    own `engagement_date` for this Client (engagement_type == DINNER, not
    archived, status != CANCELLED, engagement_date >= business-today), or
    None if no such Engagement exists -- see
    ClientCrmService.list_clients()'s own docstring for the exact
    algorithm and the "business-today" timezone convention. NOT the same
    thing as `Engagement.engagement_date` on any specific row -- this is a
    derived MINIMUM across a Client's qualifying Engagements, recomputed
    on every read, never cached or written back to any Engagement.

    `last_contacted`: the newest active (non-archived) ClientTouchpoint's
    own `occurred_at` for this Client, using the exact same canonical
    `touchpoint_sort_key` ordering as the Client detail page's own Last
    Contact -- see client_touchpoint_store.py. Deliberately the SAME
    semantic definition in both places, computed from the same
    ClientTouchpoint rows, so the Master CRM table and a Client's own
    detail page can never disagree about who was contacted last.

    Neither field is a new column on the `clients` table -- both are
    computed at read time from Engagement/ClientTouchpoint data already
    stored elsewhere, matching ClientTouchpoint's own explicit "no
    Last Contact/Next Dinner concept belongs on Client" rule."""

    next_dinner: date | None = None
    last_contacted: datetime | None = None


class ClientPage(BaseModel):
    """One page of a filtered/sorted Client list (Stage 1B, `items` type
    upgraded to ClientListItem in Stage 2C) -- same shape as
    CrmContactPage/ActivityEventPage: `items` is exactly the one page the
    caller asked for, `total` is the full filtered count (before
    pagination), so a caller never has to fetch everything to know how
    many pages exist."""

    items: list[ClientListItem]
    total: int
    page: int
    page_size: int


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
    """Stage 1E.1: the commercial/delivery *category* of an Engagement,
    deliberately collapsed to three values -- DINNER no longer names which
    kind of dinner (that's `DinnerType`'s own job, a separate axis, only
    meaningful when engagement_type is DINNER). Not exhaustive of every
    future Astronomic service -- OTHER exists precisely so a new kind of
    engagement is never blocked on a code change before it can be
    recorded; a real, named value can be added later purely additively (a
    str Enum's existing stored rows are unaffected by adding a new
    member)."""

    DINNER = "dinner"
    SPONSORSHIP = "sponsorship"
    OTHER = "other"


class EngagementStatus(str, Enum):
    """Deliberately small -- describes delivery/logistics state only,
    never a sales/pipeline probability (that's a future Deal concern, not
    this). CONFIRMED (Stage 1E) answers a real, distinct operational
    question -- "is the date/venue locked and guests being invited" --
    from PLANNED ("still being discussed/scheduled"). A postponement is
    just an `engagement_date` edit with status unchanged; a fully-failed
    event's details belong in a future closeout record, not another
    status value here."""

    PLANNED = "planned"
    CONFIRMED = "confirmed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class DinnerType(str, Enum):
    """Stage 1E.1: which kind of dinner this Engagement is, ONLY
    meaningful when `engagement_type == DINNER`. Replaces the retired
    Stage 1E `DinnerProgram` enum (Supernova/Galaxy/Aurora) -- those were
    Astronomic's own past internal program/brand names for these same
    three dinner kinds, retired from current use; this enum names the
    underlying dinner kind directly instead of via a retired brand name.
    Nullable and normally None for a non-dinner Engagement (e.g.
    SPONSORSHIP) -- see ClientCrmService's own backend-authoritative
    normalization for why this is enforced server-side, not just hidden
    in the UI.

    DONOR_DINNER and CUSTOM_DINNER (added post-Stage-1E.1) are genuinely
    current dinner types, not renames of any retired program/brand --
    unlike the first three members, neither has a corresponding entry in
    the Stage 1E.1 migration's legacy-program rename table, and none is
    needed: no retired `dinner_program` value ever meant "donor" or
    "custom", so no existing row could legitimately migrate to either
    value."""

    INVESTOR_DINNER = "investor_dinner"
    FIRESIDE_DINNER = "fireside_dinner"
    BIZDEV_DINNER = "bizdev_dinner"
    DONOR_DINNER = "donor_dinner"
    CUSTOM_DINNER = "custom_dinner"


class EngagementContractStatus(str, Enum):
    NOT_SENT = "not_sent"
    SENT = "sent"
    SIGNED = "signed"


class EngagementPaymentStatus(str, Enum):
    UNPAID = "unpaid"
    PARTIAL = "partial"
    PAID = "paid"


class Engagement(BaseModel):
    """One delivered or planned Astronomic engagement for a Client -- a
    dinner (see `dinner_type` for which kind), a sponsorship, or (OTHER)
    some future service. A Client may have many Engagements over time; an
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
    live Astronomic system (unlike Deal, which doesn't exist yet). Stage
    1E does not implement any Luma synchronization, auto-create, or
    attendance calculation -- this field stays a plain optional reference,
    not read or written by anything yet.

    `owner` (Stage 1E) is this SPECIFIC Engagement's owner/runner --
    deliberately independent from `Client.owner` (the overall relationship
    owner), same reasoning that already separates ClientContact.title from
    CrmContact.title: a dinner can be staffed by someone other than
    whoever owns the Client relationship overall. Updating an Engagement
    never writes back to Client.owner (or any other Client field) -- see
    ClientCrmService's own Stage 1E docstring for the full "Engagement is
    historical/delivery data, never a side-effect source" rule.

    `location` stays a single flexible free-text field, NOT split into a
    structured city -- Astronomic wants to record a venue, private
    residence, or neighborhood here too, not just a city name, and a
    single free-text field accommodates all of those without guessing at
    a taxonomy that doesn't exist yet."""

    engagement_id: str
    client_id: str
    title: str
    engagement_type: EngagementType
    # `dinner_type` is its own separate axis -- see DinnerType's own
    # docstring for why this isn't merged into engagement_type. Only ever
    # meaningful when engagement_type == DINNER; the API layer keeps this
    # authoritative even if the frontend hides/clears the field otherwise.
    dinner_type: DinnerType | None = None
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
    owner: str | None = None  # free text, no auth-identity system exists yet -- same
    # placeholder convention as Client.owner/ActivityEvent.actor/ClientContact fields.
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


# A non-negative-int-or-null count -- shared by every Stage 1F turnout
# field. Enforced at the schema layer (same class of validation as an
# invalid enum value elsewhere in this module) so a negative count is
# rejected by FastAPI's own request validation, never reaching the
# service layer. Null means "not entered" (unknown); an explicit 0 means
# "entered, confirmed zero" -- Pydantic's Optional handling already keeps
# these distinct (a field never coerces None to 0 or vice versa).
NonNegativeCount = Annotated[int, Field(ge=0)] | None


class EngagementCloseout(BaseModel):
    """Client CRM Stage 1F -- the same-day factual/qualitative baseline for
    one Engagement (one dinner), recorded once shortly after it happens.
    Deliberately its OWN entity, not fields on Engagement and not a
    structured ClientNote -- see the Stage 1F investigation report for the
    full architecture reasoning (ClientNote stays human free-text-only;
    Engagement stays the commercial/delivery record; Closeout is the
    factual/qualitative same-day snapshot, structurally distinct from a
    later, judgment-based Day 3/30/90 follow-up, which does not exist yet).

    At most ONE EngagementCloseout is ever created per Engagement -- see
    ClientCrmService's own Stage 1F docstring for why this is enforced at
    the service layer (checking EngagementCloseoutStore.get_for_engagement()
    before create), not a DB uniqueness constraint, matching this
    codebase's own established "the service layer owns invariants, not the
    store" convention (see ClientContactStore's own docstring for the
    identical precedent with is_primary_contact).

    Turnout counts are Stage 1F's own manually-entered aggregate SNAPSHOT,
    not derived from any per-person record -- Stage 1G (EngagementParticipant,
    not built yet) will introduce real per-person attendance; whether these
    aggregate fields stay as the historical snapshot, become computed from
    participants, or both, is an explicit Stage 1G decision, not made here.

    `attendance_rate` is deliberately NOT a field here at all (not stored,
    not a computed_field) -- it's purely derived from confirmed_guest_count/
    cancelled_count/attended_count, all already present on this model, so
    it's computed client-side only (same "computed, not duplicated"
    convention already used for e.g. the Clients list's own total-pages
    calculation) rather than risking it being serialized into the stored
    JSON blob as a stale/redundant value.

    Qualitative fields are deliberately plain free text for V1 -- no
    enums invented without a concrete requirement (see Stage 1F's own
    investigation report)."""

    closeout_id: str
    engagement_id: str
    client_id: str  # denormalized, matches Engagement's own client_id column

    # Turnout -- manually-entered aggregate snapshot (Stage 1F). Null means
    # not entered/unknown; 0 means explicitly confirmed zero. Never
    # fabricated by this model or any service method.
    confirmed_guest_count: NonNegativeCount = None
    attended_count: NonNegativeCount = None
    no_show_count: NonNegativeCount = None
    cancelled_count: NonNegativeCount = None
    unexpected_attendee_count: NonNegativeCount = None

    # Qualitative Closeout -- free text, V1 (see this model's own docstring
    # for why no enum was invented here without a concrete requirement).
    guest_quality: str | None = None
    dinner_dynamics: str | None = None
    initial_client_experience: str | None = None
    immediate_outcomes: str | None = None
    notable_signals: str | None = None
    issues: str | None = None
    referrals: str | None = None
    future_opportunities: str | None = None
    internal_notes: str | None = None

    # Completion -- a nullable timestamp, not a separate status enum: no
    # concrete requirement for more than "recorded" vs "not yet recorded"
    # was found during this stage's own investigation.
    completed_at: datetime | None = None
    completed_by: str | None = None  # free text, no auth-identity system yet -- same
    # placeholder convention as Client.owner / ClientNote.created_by / ActivityEvent.actor.

    created_at: datetime
    updated_at: datetime
    archived: bool = False


class ParticipantRole(str, Enum):
    """Client CRM Stage 1G. Client and Host are deliberately SEPARATE
    values (revisited after this stage's own investigation proposed
    combining them) -- Astronomic's own review preferred keeping them
    distinct. Moderator is NOT a separate value yet -- folds into
    SPEAKER_PANELIST until a concrete need for the distinction exists."""

    GUEST = "guest"
    CLIENT = "client"
    HOST = "host"
    SPEAKER_PANELIST = "speaker_panelist"
    ASTRONOMIC_TEAM = "astronomic_team"
    OTHER = "other"


class ParticipantRsvpStatus(str, Enum):
    """Independent from ParticipantAttendanceStatus -- see
    EngagementParticipant's own docstring for why these are two separate
    nullable fields, not one overloaded status."""

    INVITED = "invited"
    CONFIRMED = "confirmed"
    DECLINED = "declined"


class ParticipantAttendanceStatus(str, Enum):
    """CANCELLED here mirrors EngagementCloseout.cancelled_count's own
    concept: told us in advance they wouldn't attend. A pure walk-in is
    NOT represented here -- see `is_walk_in` on EngagementParticipant."""

    ATTENDED = "attended"
    NO_SHOW = "no_show"
    CANCELLED = "cancelled"


class ParticipantSource(str, Enum):
    """WHERE this participant record came from. Stage 1G creates ONLY
    MANUAL records -- `source` is server-owned (never accepted from a
    request body) and always set to MANUAL by ClientCrmService; LUMA is
    a reserved value for a future, dedicated Luma-linkage stage, not
    something Stage 1G reads, writes, or exposes as a create/update
    option. Keeping it in the enum now (rather than adding it later) is
    harmless and avoids a values migration once that stage exists."""

    MANUAL = "manual"
    LUMA = "luma"


class EngagementParticipant(BaseModel):
    """Client CRM Stage 1G -- the person-level relationship between a
    canonical CrmContact (AstroHub's one people database) and a specific
    Engagement, e.g. "Ethan Wong attended the SF Investor Dinner as a
    Guest." Deliberately its own entity (not a field on Engagement, not a
    new people table) -- see this stage's own investigation report.

    `crm_contact_id` is OPTIONAL, unlike ClientContact's own effectively-
    mandatory linking rule -- Luma's own `LumaRegistration.match_status ==
    NEEDS_REVIEW` already proves "a real person with no confirmed
    canonical match yet" is a normal, expected, non-error state elsewhere
    in this codebase; historical guest lists, walk-ins, and unmatched
    people are legitimate cases this stage must not force into a fake
    CrmContact merely to satisfy a foreign key. An unresolved participant
    (crm_contact_id is None) must still carry enough identity information
    to be meaningful -- see ClientCrmService's own Stage 1G docstring for
    the exact validation rule.

    first_name/last_name/email/title/company are SNAPSHOTS -- populated
    FROM the canonical CrmContact at link time (create, or later linking
    an unresolved participant to one), exactly the same "snapshot, not
    live reference, never mutates the canonical record" principle
    ClientContact already established. When crm_contact_id is None, these
    fields are the ONLY record of who this person is, not a snapshot of
    anything else.

    Stage 4A (2026-09-11) addendum -- these snapshot fields remain
    immutable-ish historical/source data: editing the linked Contact
    later never rewrites them, and nothing here changes that. What DOES
    change is what CURRENT DISPLAY prefers: for a linked participant
    (crm_contact_id set, Contact found), ClientCrmService.
    list_engagement_participants() now resolves and returns
    EngagementParticipantView, whose resolved_name/resolved_title/
    resolved_company/resolved_profile_photo_url prefer the canonical
    Contact's CURRENT values, falling back to these snapshot fields only
    when the Contact field is blank (or there's no linked Contact, or it
    can't be found) -- see EngagementParticipantView's own docstring for
    the exact precedence. The raw snapshot fields below are still
    returned unchanged in that same response, for compatibility and for
    editing this participant's own record.

    `rsvp_status` and `attendance_status` are deliberately TWO separate
    nullable fields, not one overloaded status enum -- they answer
    different questions ("did they say they were coming" vs "did they
    actually show up") and conflating them would make ordinary states
    (e.g. confirmed-but-later-cancelled, or invited-with-no-attendance-
    outcome-yet) impossible to represent cleanly.

    `is_walk_in` is provenance/context, not attendance -- a walk-in can
    (usually does) also have attendance_status=ATTENDED; collapsing this
    into one status enum would make that ordinary case a contradiction.

    At most one ACTIVE (non-archived) EngagementParticipant may exist per
    (engagement_id, crm_contact_id) -- enforced by a real SQLite partial
    unique index (see sqlite_engagement_participant_store.py), not just a
    service-layer check, so this is safe even under concurrent requests.
    Re-adding someone after archiving their old relationship is done by
    restoring the existing (archived) row, never by creating a new one --
    same "restore, don't recreate" idiom as every other Client CRM entity.

    Stage 1G never automatically updates EngagementCloseout's own
    manually-entered turnout counts based on participant records --
    participant-derived counts and the Closeout snapshot remain two
    separate facts (see this stage's own investigation report)."""

    participant_id: str
    engagement_id: str
    client_id: str  # denormalized, matching Engagement/EngagementCloseout's own convention

    crm_contact_id: str | None = None

    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    title: str | None = None
    company: str | None = None

    role: ParticipantRole = ParticipantRole.GUEST
    rsvp_status: ParticipantRsvpStatus | None = None
    attendance_status: ParticipantAttendanceStatus | None = None
    is_walk_in: bool = False

    source: ParticipantSource = ParticipantSource.MANUAL

    created_at: datetime
    updated_at: datetime
    archived: bool = False


class EngagementParticipantView(EngagementParticipant):
    """Client CRM Stage 4A -- exactly an EngagementParticipant plus four
    READ-ONLY derived display fields, never persisted and never accepted
    on any create/update request (only list_engagement_participants()'s
    response uses this type -- create/update routes keep returning plain
    EngagementParticipant, unaugmented). Same "strict superset, derived-
    only, GET-response-only" shape as ClientListItem(Client).

    Precedence (see ClientCrmService.list_engagement_participants()'s own
    docstring for the exact implementation):

    resolved_name: current Contact's first_name+last_name if that joins
    to a nonblank string, else the participant's OWN first_name+last_name
    snapshot, else "Unnamed participant". A whitespace-only Contact name
    does NOT suppress a nonblank snapshot name.

    resolved_title / resolved_company: current Contact's field if
    nonblank, else the participant's own snapshot field, else None.

    resolved_profile_photo_url: current Contact's profile_photo_url if
    the participant is linked to a Contact that has one, else None --
    EngagementParticipant itself has no photo field of its own to fall
    back to.

    An ARCHIVED linked Contact still wins over the snapshot -- archived
    means "no longer an active relationship," not "this person's current
    identity should be forgotten." Fallback to the snapshot happens ONLY
    when crm_contact_id is None, the linked Contact can't be found, or
    the specific Contact field itself is blank -- never merely because
    the Contact is archived.

    None of this touches storage: the plain first_name/last_name/title/
    company/email fields inherited from EngagementParticipant are still
    present, unchanged, in this same response -- for compatibility and
    for editing this participant's own snapshot."""

    resolved_name: str
    resolved_title: str | None = None
    resolved_company: str | None = None
    resolved_profile_photo_url: str | None = None


class ContactEventHistoryEntry(BaseModel):
    """Contacts CRM Stage 3A -- one row of a canonical CrmContact's Event
    History, derived entirely from a single non-archived
    EngagementParticipant (see ClientCrmService.list_contact_event_history()
    for the exact derivation). EngagementParticipant is the canonical
    source of "this Contact is associated with this event" -- Luma is only
    ONE ingestion source that creates/updates EngagementParticipant rows;
    manual/email RSVP is another. LumaRegistration itself is deliberately
    NEVER read here -- merging both would risk a duplicate history row for
    the same Contact+Engagement; this is guaranteed to be exactly one row
    per (engagement_id, crm_contact_id) because that's already the
    EngagementParticipant store's own enforced uniqueness invariant.

    Every field here is copied straight from the underlying
    EngagementParticipant/Engagement/Client rows -- no new business logic,
    no filtering by role/RSVP/attendance/source (that's Stage 3B's
    concern, not this projection's). `role`/`rsvp_status`/
    `attendance_status`/`source` reuse the EXACT existing enums already
    defined above -- a manually-created Guest/Confirmed participant and a
    Luma-created one are indistinguishable in shape, differing only in
    `source`."""

    engagement_id: str
    participant_id: str
    event_name: str  # Engagement.title
    client_name: str  # Client.name
    engagement_date: date | None
    engagement_type: EngagementType
    dinner_type: DinnerType | None
    role: ParticipantRole
    rsvp_status: ParticipantRsvpStatus | None
    attendance_status: ParticipantAttendanceStatus | None
    source: ParticipantSource


class ContactType(str, Enum):
    """Client CRM Stage 2A. The channel a ClientTouchpoint happened
    through -- V1's fixed, approved set; no other values are accepted."""

    EMAIL = "email"
    CALL = "call"
    SLACK = "slack"
    LINKEDIN = "linkedin"
    IN_PERSON = "in_person"


class ClientTouchpoint(BaseModel):
    """Client CRM Stage 2A -- a persistent, structured record of one
    communication/interaction with a Client, e.g. "emailed Sid Atkinson
    about dinner planning." Deliberately its OWN entity, not a repurposed
    ClientNote -- see this stage's own investigation report for the full
    comparison. ClientNote's own docstring already draws a hard line
    around what it refuses to become (structured Day 3/30/90 business
    data); a Touchpoint needs a required `contact_type`, an optional link
    to a specific canonical Contact, and a `contacted_by` field -- none of
    which ClientNote has or should grow, and none of which belong mixed
    into `note_type` (a relationship-lifecycle tag, not a contact
    channel).

    `crm_contact_id` is OPTIONAL -- a touchpoint isn't always about one
    identifiable person (a Slack message to a whole channel, a voicemail
    with no callback yet), and forcing one here would be the same mistake
    Stage 1G's own investigation already ruled out for EngagementParticipant.
    When supplied, the service layer requires that this canonical Contact
    already be linked to this SAME Client via an active (non-archived)
    ClientContact -- Stage 2A does NOT allow attaching an arbitrary global
    Contact directly to a Touchpoint; if the person isn't a ClientContact
    yet, they must be linked to the Client first (see
    ClientCrmService.create_client_touchpoint()'s own docstring for the
    exact validation).

    `contact_name` is a SNAPSHOT, populated FROM the canonical Contact at
    link time (create, or a later crm_contact_id change) -- same "point-
    in-time snapshot, never a live reference" principle ClientContact and
    EngagementParticipant already established, so a Touchpoint keeps
    rendering correctly even after the canonical Contact is later renamed,
    archived, or merged. It is a single combined display string (not a
    first_name/last_name split) -- this model's only UI need is one
    "Contact" column, not a queryable name. Clearing crm_contact_id also
    clears contact_name; changing crm_contact_id re-snapshots from the
    newly-selected Contact. The canonical Contact changing later NEVER
    retroactively rewrites an existing Touchpoint's own snapshot.

    `contacted_by` stays plain free text -- same placeholder convention as
    Client.owner/Engagement.owner/ClientNote.created_by/EngagementCloseout.
    completed_by (no authenticated-per-user identity system exists
    anywhere in this app yet; see each of those fields' own identical
    comment). Required (non-blank after trimming) for a NEW Touchpoint at
    the service/API layer -- not a schema-level constraint, matching how
    Client.name's own required-non-blank rule is enforced in the service,
    not via a Pydantic Field constraint.

    `note` is optional -- the structured fields (occurred_at/contact_type/
    contacted_by/crm_contact_id) are this model's primary content; a
    "left voicemail, no answer" touchpoint has nothing more to say and
    shouldn't be blocked on writing one.

    No `delete()` on this store -- same archive-only convention as every
    other Client CRM entity. Archiving a Client does NOT cascade-archive
    its Touchpoints (same "no cascading archive" precedent already
    established elsewhere in this app).

    Deliberately carries NO derived Last Contact/Last Contacted/Next
    Dinner concept -- Stage 2A is the persistence layer only; deriving
    "the newest active Touchpoint" for display is a later stage's job,
    computed at read time, never stored here or on Client."""

    touchpoint_id: str
    client_id: str

    crm_contact_id: str | None = None
    contact_name: str | None = None  # snapshot -- see model docstring

    occurred_at: datetime
    contact_type: ContactType
    contacted_by: str | None = None  # free text; required non-blank on CREATE, enforced in the service layer
    note: str | None = None

    created_at: datetime
    updated_at: datetime
    archived: bool = False
