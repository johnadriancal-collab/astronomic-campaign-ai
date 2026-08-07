"""
Pydantic models for the CRM -- our own, standalone source of truth for
known prospects/relationships. Deliberately separate from Lead/CampaignLead
(app/models/lead.py): a Lead requires an apollo_contact_id and only ever
exists because CampaignService.build() created a real Apollo contact for a
selected prospect. A CrmContact requires neither -- someone met at a
conference with no Apollo footprint at all is a completely normal
CrmContact. Nothing in this file is read by, or writes to, Campaign/Lead/
CampaignLead/EmailSequence/EmailMessage.

Three field groups, kept structurally distinct on purpose:

1. External/source fields -- Apollo-style prospect data. Overwritten on
   import, but ONLY when the incoming value is non-empty (see
   crm_service.py's merge rule) -- a blank CSV cell must never erase a
   value we already have. NOTE: our current Apollo integration
   (app/apollo/people.py) does not actually populate most of these from a
   live search -- verified live: `search_people()` only returns `title`
   and `organization.name` as real values, plus boolean presence-flags,
   never real email/phone/location/revenue. These fields exist here for
   CSV import and manual entry, and to be ready for a future Apollo
   enrichment integration -- this file adds no fake enrichment behavior.

2. Investor Thesis fields -- reproduce Astronomic's actual Investor Thesis
   Google Form question-for-question (see the `*_OPTIONS` constants below
   for each question's exact answer choices). Q1-4 of the real form (First
   Name/Last Name/Email/LinkedIn) are NOT duplicated here -- they're the
   same identity fields already in group 1. Every other question becomes
   its own field, preserving the form's private/institutional split
   (Section 2 vs Section 3 ask the identical seven questions once for each
   context) and its literal wording/options. "Other:" free-text on a
   multi-select question gets its own `_other` companion field rather than
   being folded into the option list.

3. Custom fields -- open-ended, user-managed via CrmCustomFieldDefinition
   (field_key/label/type/options/required/active), stored in
   `custom_fields: dict[str, Any]` keyed by field_key. Adding, editing, or
   deactivating a definition never touches existing contact rows or
   requires a migration -- a key simply doesn't exist on old rows until
   someone sets it. Nothing here is pre-seeded; every custom field is one
   the user creates through the CRM itself.
"""

import types
from datetime import datetime
from enum import Enum
from typing import Any, get_args, get_origin

from pydantic import BaseModel, Field, field_validator

# Shared option lists -- Section 2 (private) and Section 3 (institutional)
# of the real form ask these seven questions with IDENTICAL choices, so one
# canonical list per question serves both thesis_private_* and
# thesis_institutional_* fields below.

ASSET_TYPE_OPTIONS = [
    "Carbon credits / ESG investments",
    "Collectibles (e.g., art, wine, watches)",
    "Commodities (e.g., gold, oil, agriculture)",
    "Cryptocurrency / digital assets",
    "Fund-of-funds",
    "Hedge funds (multi-asset strategies)",
    "Infrastructure (e.g., toll roads, utilities, airports)",
    "Private credit (e.g., private loans, direct lending)",
    "Private equity",
    "Public equities (stocks, ETFs)",
    "Real estate (direct ownership, syndications, REITs)",
    "Royalty financing",
    "Search funds / ETA (entrepreneurship through acquisition)",
    "Secondaries (e.g., buying/selling LP interests or founder equity)",
    "Structured products (e.g., notes, derivatives)",
    "Venture capital (e.g., angel checks, early-stage startups, high-growth tech)",
]

BUSINESS_MODEL_OPTIONS = [
    "Agencies / Services (e.g., marketing, development firms)",
    "Auction or Bidding Platforms",
    "Brokerages",
    "Community-based / Network-led growth",
    "Creator-led / Influencer brands",
    "Direct-to-Consumer (DTC) eCommerce",
    "Franchises",
    "Hardware / Physical products",
    "Licensing / IP-based",
    "Manufacturing",
    "Marketplaces (e.g., Airbnb, Uber-style platforms)",
    "Software as a Service (SaaS)",
    "Subscription / Membership businesses",
    "Web3 / Tokenized business models",
]

INDUSTRY_OPTIONS = [
    "Aerospace & Defense",
    "AgTech & Food Production",
    "Artificial Intelligence / Machine Learning",
    "Automotive & Mobility",
    "Biotech & Life Sciences",
    "Climate Tech & Sustainability",
    "Construction & Built Environment",
    "Consumer Goods & Retail",
    "Creative Industries (Media, Music, Photo, etc.)",
    "Cybersecurity",
    "EdTech (Education Technology)",
    "Entertainment & Gaming",
    "Fashion & Apparel",
    "Fintech (Finance & Insurance)",
    "Food & Beverage",
    "GovTech / Civic Tech",
    "Healthcare & HealthTech",
    "HR Tech & Future of Work",
    "Industrial / Manufacturing / Robotics",
    "LegalTech",
    "Marketing & AdTech",
    "Mental Health & Wellness",
    "Real Estate & PropTech",
    "SaaS / Software Infrastructure",
    "Social Media & Creator Economy",
    "Telecom & Connectivity",
    "Travel, Tourism & Hospitality",
    "Veterinary / Animal Health",
]

CHECK_SIZE_OPTIONS = [
    "$1k - $10k",
    "$10k - $25k",
    "$25k - $50k",
    "$50k - $100k",
    "$100k - $250k",
    "$250k - $500k",
    "$500k - $1M",
    "$1M - $2M",
    "$2M - $5M",
    "$5M - $10M",
    "$10M+",
]

DEAL_STAGE_OPTIONS = [
    "Friends & Family (idea or concept stage, often pre-incorporation)",
    "Pre-Seed (early development, pre-revenue or minimal traction)",
    "Seed (product in market, early customers or pilots)",
    "Series A (scaling phase, revenue traction, team expansion)",
    "Series B or later (growth or expansion stage, institutional rounds)",
    "Fund LP (investor in venture/private equity funds)",
    "Secondary (buying equity from early investors or founders)",
    "Growth Equity (post-Series B+, but still private)",
    "Pre-IPO / Late-Stage Private (companies nearing exit or IPO)",
]

MEETING_PREFERENCE_OPTIONS = [
    "In an email intro",
    "I'd do a Zoom call",
    "I'd meet them at a restaurant or coffee shop",
    "I'd host a dinner at my house",
]

DEMOGRAPHIC_PREFERENCE_OPTIONS = [
    "I'm open to investing in anyone",
    "I prefer female fundraisers",
    "I prefer male fundraisers",
    "I prefer black fundraisers",
    "I prefer Latino fundraisers",
    "I prefer Asian fundraisers",
    "I prefer Indian fundraisers",
    "I prefer LGBTQ+ fundraisers",
    "I prefer military fundraisers",
]

INVESTOR_MODE_OPTIONS = ["Privately", "Institutionally", "Both"]

# 2026-08-07 Dietary Preferences design -- canonical multi-select vocabulary for Q24,
# derived from taxonomy review, NOT from observed CSV frequency (no CSV has ever
# populated this column). Distinctions deliberately preserved rather than merged:
# Dairy-Free/Lactose-Free, Gluten-Free/Wheat-Free, Seafood-Free/Shellfish-Free/
# Fish-Free/Mollusks-Free, Vegetarian/Vegan, Pescatarian/Pollotarian, and
# Paleo/Keto/Low Carb are each clinically or practically distinct concepts -- see
# get_contact_export_fields()-adjacent design notes in the PR/commit for the full
# reasoning. "None" and "Other" are themselves valid, recognized options here (not
# fallback sentinels) -- classify_dietary_preferences treats a literal "None" or
# "Other" value as already-recognized, never as unrecognized overflow.
DIETARY_PREFERENCE_OPTIONS = [
    "Vegetarian", "Vegan", "Pescatarian", "Pollotarian", "Paleo", "Keto", "Low Carb",
    "Halal", "Kosher", "Gluten-Free", "Dairy-Free", "Lactose-Free", "Nut-Free",
    "Soy-Free", "Egg-Free", "Pork-Free", "Beef-Free", "Shellfish-Free", "Fish-Free",
    "Seafood-Free", "Alcohol-Free", "Sugar-Free", "No Spicy Food", "Wheat-Free",
    "Mollusks-Free", "Grain-Free", "Corn-Free", "MSG-Free", "Seed Oil-Free",
    "None", "Other",
]

# The custom field "Investor Type" (field_key="investor_type") is Astronomic's
# investor archetype -- distinct from this Investor Thesis Q6 mode -- but each
# archetype implies a private-vs-institutional signal, which is what
# derive_investor_mode() below turns into thesis_investor_mode automatically.
PRIVATE_INVESTOR_TYPES = frozenset(
    {
        "Angel Investor",
        "I sponsor deals that I find",
        "Invest with group of Angels",
        "Participate in syndicated investments",
        "Private Investor",
    }
)
INSTITUTIONAL_INVESTOR_TYPES = frozenset(
    {
        "Family Office",
        "Fund LP",
        "Institutional Investor",
        "Private Equity",
        "Venture Capital",
    }
)


def derive_investor_mode(investor_type: list[str] | None) -> str | None:
    """
    thesis_investor_mode's automated value, derived from the "Investor Type"
    custom field. Called by CrmService on every create/update where
    thesis_investor_mode_manual_override is False -- see CrmContact's
    thesis_investor_mode_manual_override docstring for why that flag exists
    and how it interacts with this function. Returns None (leaves the field
    unset) when investor_type carries no private/institutional signal at
    all -- never guesses.
    """
    types = set(investor_type or [])
    is_private = bool(types & PRIVATE_INVESTOR_TYPES)
    is_institutional = bool(types & INSTITUTIONAL_INVESTOR_TYPES)
    if is_private and is_institutional:
        return "Both"
    if is_private:
        return "Privately"
    if is_institutional:
        return "Institutionally"
    return None


# The custom field "Dinner Subscriptions" (field_key="dinner_subscriptions") started
# as free text, so real contacts and future CSVs carry a mix of the 14 final options
# below plus retired legacy wording. normalize_dinner_subscriptions() is the single
# source of truth for collapsing that legacy wording down to the closed set -- reused
# by the one-time contact-value migration (crm_migration.py) AND by the CSV-import
# classification rule (crm_classification_rules.py), so a manually-fixed contact, a
# migrated contact, and a freshly-imported contact are always normalized identically.
DINNER_SUBSCRIPTION_OPTIONS = [
    "Investor Dinners",
    "Investor Dinners Unsubscribe",
    "Founder Dinners",
    "Founder Dinners Unsubscribe",
    "Newsletter",
    "Newsletter Unsubscribe",
    "Donor Dinners",
    "Donor Dinners Unsubscribe",
    "Unsubscribe (Do not Email)",
    "Not actively Investing",
    "Biz Dev Dinners",
    "Biz Dev Dinners Unsubscribe",
    "Fireside Dinners",
    "Fireside Dinners Unsubscribe",
]

# Legacy value -> the one final option it collapses into. Verified against every
# unique token that actually appears in the real production export (2026-08-06 audit) --
# nothing here is guessed.
DINNER_SUBSCRIPTION_LEGACY_MAP = {
    "Mansion dinners with matching founders/investors": "Investor Dinners",
    "Couples dinners with matching founder/investor couples": "Investor Dinners",
    "I own a 5k+ sqft house and would host private dinners at my home": "Investor Dinners",
    "Matching with global/international investors/founders in other capital cities around the world": (
        "Investor Dinners"
    ),
    "Regulus Dinners": "Founder Dinners",
    "Sigma Librae Dinners": "Founder Dinners",
    "Exodus Dinners": "Founder Dinners",
    "CD Newsletter Unsubscribe": "Newsletter Unsubscribe",
    "Small group dinners": "Investor Dinners",
}

# Legacy values that carry no forward-looking signal at all -- dropped, never mapped.
DINNER_SUBSCRIPTION_DELETE_VALUES = frozenset(
    {
        "Astronomic General Subscriber",
        "Parent dinners",
        "Retreats",
    }
)


def normalize_dinner_subscriptions(tokens: list[str]) -> list[str]:
    """
    Collapses a list of raw Dinner Subscriptions tokens (already comma-split
    and trimmed by the caller) down to the closed 14-option set: drops
    DINNER_SUBSCRIPTION_DELETE_VALUES entries, rewrites
    DINNER_SUBSCRIPTION_LEGACY_MAP entries to their final option, leaves
    already-final values untouched, and deduplicates while preserving first-
    seen order -- so a contact with both "Investor Dinners" and the legacy
    "Mansion dinners..." (which also maps to Investor Dinners) ends up with
    that option once, not twice. A token that matches none of the above
    (not a final option, not a legacy mapping, not a delete value) is
    preserved verbatim rather than silently dropped -- same convention as
    _translate_comma_joined_column's legacy-token handling elsewhere in this
    codebase -- so an unexpected future value stays visible instead of
    disappearing.
    """
    result: list[str] = []
    for raw in tokens:
        token = raw.strip()
        if not token or token in DINNER_SUBSCRIPTION_DELETE_VALUES:
            continue
        mapped = DINNER_SUBSCRIPTION_LEGACY_MAP.get(token, token)
        if mapped not in result:
            result.append(mapped)
    return result


def normalize_email(email: str | None) -> str | None:
    return email.strip().lower() or None if email else None


def normalize_linkedin_url(url: str | None) -> str | None:
    if not url:
        return None
    normalized = url.strip().lower().rstrip("/")
    for prefix in ("https://", "http://", "www."):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
    return normalized or None


def normalize_name_company(first_name: str | None, last_name: str | None, company: str | None) -> str | None:
    """
    The dedup hierarchy's fourth, non-confident tier -- exact normalized
    match only, never fuzzy. Returns None (never matchable) unless all
    three components are present, since a partial match here is exactly
    the kind of low-confidence guess this tier is deliberately NOT meant
    to make.
    """
    if not (first_name and last_name and company):
        return None
    parts = [first_name.strip().lower(), last_name.strip().lower(), company.strip().lower()]
    return "|".join(parts)


EXTERNAL_FIELD_NAMES = frozenset(
    {
        "apollo_contact_id", "first_name", "last_name", "email", "email_status", "phone",
        "linkedin_url", "title", "company", "company_website", "city", "state", "country",
        "industry", "company_size", "revenue", "funding_stage", "funding_amount",
        "technologies", "seniority", "department", "job_function",
    }
)

THESIS_FIELD_NAMES = frozenset(
    {
        "thesis_cities", "thesis_investor_mode", "thesis_investor_mode_manual_override",
        "thesis_private_asset_types", "thesis_private_asset_types_other",
        "thesis_private_business_models", "thesis_private_business_models_other",
        "thesis_private_industries", "thesis_private_industries_other",
        # thesis_private_check_sizes / thesis_private_check_sizes_other deliberately
        # excluded -- deprecated as of the 2026-08-06 Check Size consolidation.
        # check_size_personal (custom field) is now the sole canonical destination;
        # excluding these from THESIS_FIELD_NAMES makes apply_import_mapping()
        # refuse to write to them regardless of column_mapping, closing the import
        # write path. The model fields themselves are NOT deleted -- existing data
        # stays in place, untouched, for rollback safety.
        "thesis_private_deal_stages", "thesis_private_deal_stages_other",
        "thesis_private_meeting_preferences", "thesis_private_meeting_preferences_other",
        "thesis_private_demographic_preferences", "thesis_private_demographic_preferences_other",
        "thesis_private_other_criteria",
        "thesis_also_invests_institutionally",
        "thesis_institutional_asset_types", "thesis_institutional_asset_types_other",
        "thesis_institutional_business_models", "thesis_institutional_business_models_other",
        "thesis_institutional_industries", "thesis_institutional_industries_other",
        # thesis_institutional_check_sizes / thesis_institutional_check_sizes_other --
        # same deprecation as the private pair above; check_size_institutional
        # (custom field) is now the sole canonical destination.
        "thesis_institutional_deal_stages", "thesis_institutional_deal_stages_other",
        "thesis_institutional_meeting_preferences", "thesis_institutional_meeting_preferences_other",
        "thesis_institutional_demographic_preferences", "thesis_institutional_demographic_preferences_other",
        "thesis_institutional_other_criteria",
        "thesis_dietary_preferences", "thesis_dietary_preferences_other", "thesis_referral_emails",
    }
)


class CrmContact(BaseModel):
    crm_contact_id: str
    created_at: datetime
    updated_at: datetime
    archived: bool = False  # soft-delete only, matching this app's archive-not-delete
    # convention already used for EmailSequence -- never hard-deleted.

    # --- Group 1: external/source (Apollo-style) fields ---
    apollo_contact_id: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    email_status: str | None = None
    phone: str | None = None
    linkedin_url: str | None = None
    title: str | None = None
    company: str | None = None
    company_website: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    industry: str | None = None
    company_size: str | None = None
    revenue: str | None = None
    funding_stage: str | None = None
    funding_amount: str | None = None
    technologies: list[str] = Field(default_factory=list)
    seniority: str | None = None
    department: str | None = None
    job_function: str | None = None
    source_snapshot: dict = Field(default_factory=dict)  # raw imported row/payload --
    # never authoritative; the fields above always win.

    # --- Group 2: Investor Thesis fields (Astronomic's real form, Q5-25;
    # Q1-4 are first_name/last_name/email/linkedin_url above) ---
    thesis_cities: str | None = None  # Q5, free text as answered (not split/parsed)
    thesis_investor_mode: str | None = None  # Q6: one of INVESTOR_MODE_OPTIONS
    thesis_investor_mode_manual_override: bool = False  # False (default) = CrmService
    # keeps this auto-derived from custom_fields["investor_type"] on every create/update
    # (see crm_service.derive_investor_mode) -- a human can flip this to True via the UI
    # to pick Privately/Institutionally/Both by hand and have the automation leave it alone.

    thesis_private_asset_types: list[str] = Field(default_factory=list)  # Q7
    thesis_private_asset_types_other: str | None = None
    thesis_private_business_models: list[str] = Field(default_factory=list)  # Q8
    thesis_private_business_models_other: str | None = None
    thesis_private_industries: list[str] = Field(default_factory=list)  # Q9
    thesis_private_industries_other: str | None = None
    thesis_private_check_sizes: list[str] = Field(default_factory=list)  # Q10
    thesis_private_check_sizes_other: str | None = None
    thesis_private_deal_stages: list[str] = Field(default_factory=list)  # Q11
    thesis_private_deal_stages_other: str | None = None
    thesis_private_meeting_preferences: list[str] = Field(default_factory=list)  # Q12
    thesis_private_meeting_preferences_other: str | None = None
    thesis_private_demographic_preferences: list[str] = Field(default_factory=list)  # Q13
    thesis_private_demographic_preferences_other: str | None = None
    thesis_private_other_criteria: str | None = None  # Q14

    thesis_also_invests_institutionally: bool | None = None  # Q15 (Yes/No)

    thesis_institutional_asset_types: list[str] = Field(default_factory=list)  # Q16
    thesis_institutional_asset_types_other: str | None = None
    thesis_institutional_business_models: list[str] = Field(default_factory=list)  # Q17
    thesis_institutional_business_models_other: str | None = None
    thesis_institutional_industries: list[str] = Field(default_factory=list)  # Q18
    thesis_institutional_industries_other: str | None = None
    thesis_institutional_check_sizes: list[str] = Field(default_factory=list)  # Q19
    thesis_institutional_check_sizes_other: str | None = None
    thesis_institutional_deal_stages: list[str] = Field(default_factory=list)  # Q20
    thesis_institutional_deal_stages_other: str | None = None
    thesis_institutional_meeting_preferences: list[str] = Field(default_factory=list)  # Q21
    thesis_institutional_meeting_preferences_other: str | None = None
    thesis_institutional_demographic_preferences: list[str] = Field(default_factory=list)  # Q22
    thesis_institutional_demographic_preferences_other: str | None = None
    thesis_institutional_other_criteria: str | None = None  # Q23

    # Q24 -- converted from a plain TEXT field to a validated multi-select on
    # 2026-08-07 (see DIETARY_PREFERENCE_OPTIONS above). Every existing stored
    # contact has this field as a literal JSON `null` (the old str|None default),
    # which a bare `list[str]` annotation would reject outright on load -- the
    # `mode="before"` validator below coerces that legacy null to `[]` so every
    # pre-existing contact keeps loading correctly with zero data migration.
    thesis_dietary_preferences: list[str] = Field(default_factory=list)
    thesis_dietary_preferences_other: str | None = None  # free-text overflow for unrecognized values
    thesis_referral_emails: str | None = None  # Q25, raw text (not split/parsed)

    # --- Group 3: custom fields ---
    custom_fields: dict[str, Any] = Field(default_factory=dict)

    @field_validator("thesis_dietary_preferences", mode="before")
    @classmethod
    def _coerce_legacy_null_dietary_preferences(cls, value: Any) -> Any:
        """Every contact stored before the 2026-08-07 TEXT->list conversion has this
        field as a literal `null` in its persisted JSON (the old str|None default).
        Without this, loading any pre-existing contact would fail model validation
        outright the moment the field becomes `list[str]`. Only coerces None --
        anything else (a real list, or a bad value) is passed through unchanged so
        Pydantic's normal type validation still catches genuine errors."""
        return [] if value is None else value


class CrmContactExportField(BaseModel):
    """One core/thesis column for the CRM contacts CSV export -- see
    get_contact_export_fields() for how this list is computed."""

    key: str
    kind: str  # "scalar" | "list" | "boolean"


def _export_field_kind(annotation: Any) -> str:
    """Classifies a CrmContact field's type annotation for CSV export
    formatting: multi-select/list fields get semicolon-joined, booleans
    get true/false, everything else is stringified as-is. Checks for a
    bare `list[...]` origin first, then unwraps `X | None` (every core
    field is optional) and recurses -- get_args() on a plain `list[str]`
    also returns a non-empty tuple (the item type), so unwrapping before
    checking for `list` would wrongly collapse `list[str]` to `str`."""
    origin = get_origin(annotation)
    if origin is list:
        return "list"
    if origin is types.UnionType:
        non_none = [a for a in get_args(annotation) if a is not type(None)]
        if len(non_none) == 1:
            return _export_field_kind(non_none[0])
        return "scalar"
    if annotation is bool:
        return "boolean"
    return "scalar"


def get_contact_export_fields() -> list[CrmContactExportField]:
    """
    Every CORE/thesis field on CrmContact, in declaration order, computed
    directly from the Pydantic model via introspection -- NOT a
    hand-maintained list. A field added to CrmContact later is
    automatically included here with zero changes to this function,
    which is the whole point: this list backs the "export every CRM
    field" CSV feature, and a hardcoded column list would silently go
    stale the next time a field is added.

    Excludes `source_snapshot` (the raw last-imported CSV row -- an
    internal reference for auditing import behavior, not a stable field
    with a consistent meaning across contacts, and not something the CRM
    export should expose) and `custom_fields` (a dict container; its
    members are separately enumerable via GET /crm/custom-fields, each
    with its own real field_key/label/type).
    """
    excluded = {"source_snapshot", "custom_fields"}
    return [
        CrmContactExportField(key=name, kind=_export_field_kind(field.annotation))
        for name, field in CrmContact.model_fields.items()
        if name not in excluded
    ]


class CustomFieldType(str, Enum):
    TEXT = "text"
    LONG_TEXT = "long_text"
    NUMBER = "number"
    DATE = "date"
    BOOLEAN = "boolean"
    SINGLE_SELECT = "single_select"
    MULTI_SELECT = "multi_select"


class CrmCustomFieldDefinition(BaseModel):
    crm_custom_field_id: str
    field_key: str  # unique -- the key used in CrmContact.custom_fields
    label: str
    description: str | None = None
    field_type: CustomFieldType
    options: list[str] = Field(default_factory=list)  # for single_select/multi_select
    required: bool = False
    active: bool = True
    created_at: datetime
    updated_at: datetime


class CrmImportRowStatus(str, Enum):
    NEW = "new"
    EXISTING = "existing"
    POSSIBLE_DUPLICATE = "possible_duplicate"
    ERROR = "error"


class CrmImportRowPreview(BaseModel):
    row_index: int
    mapped_fields: dict[str, Any]
    status: CrmImportRowStatus
    matched_contact_id: str | None = None
    matched_on: str | None = None  # "email" | "apollo_contact_id" | "linkedin_url" | "name_company"
    error: str | None = None


class CrmImportBatchStatus(str, Enum):
    UPLOADED = "uploaded"
    MAPPED = "mapped"
    COMMITTED = "committed"


class CrmImportBatch(BaseModel):
    import_batch_id: str
    filename: str
    uploaded_at: datetime
    status: CrmImportBatchStatus = CrmImportBatchStatus.UPLOADED
    headers: list[str]
    rows: list[dict[str, Any]]  # raw parsed CSV rows, in file order
    row_count: int
    suggested_mapping: dict[str, str] = Field(default_factory=dict)  # csv_header -> crm field key
    column_mapping: dict[str, str] | None = None  # confirmed mapping, set by /preview
    preview: list[CrmImportRowPreview] | None = None
    new_count: int | None = None
    existing_count: int | None = None
    possible_duplicate_count: int | None = None
    error_count: int | None = None


class CrmImportReport(BaseModel):
    created: int
    updated: int
    skipped: int
    errors: int


class CrmContactPage(BaseModel):
    """
    Server-side pagination result -- `items` is only ever the ONE page
    requested, never the full filtered set. `total` is the full
    filtered-but-unpaginated count, needed for "page X of Y" / total-count
    display without the browser ever holding more than one page's worth
    of contacts at a time.
    """

    items: list[CrmContact]
    total: int
    page: int
    page_size: int


# --- More Filters: dynamic field registry + query engine (2026-08-07) ---
#
# The filter TYPE here is a richer classification than get_contact_export_fields()'s
# scalar/list/boolean -- it drives which OPERATORS a field exposes, and none of the
# core/thesis fields are actually enum-enforced at the Pydantic level (thesis_investor_mode
# is a plain `str`, thesis_private_industries is a plain `list[str]` -- the *_OPTIONS
# constants above are respected by convention, not validation). So this classification is a
# small, explicit, hand-maintained registry (see crm_filter_service.py), the same "one small
# named list is the source of truth" pattern as EXTERNAL_FIELD_NAMES/THESIS_FIELD_NAMES above --
# NOT something derivable purely from CrmContact.model_fields introspection.
class FilterFieldType(str, Enum):
    TEXT = "text"
    NUMBER = "number"
    BOOLEAN = "boolean"
    DATE = "date"
    SINGLE_SELECT = "single_select"
    MULTI_SELECT = "multi_select"


class FilterFieldMeta(BaseModel):
    """
    One filterable field's complete metadata -- the frontend builds its entire field
    picker, operator dropdown, and value control from this alone (GET
    /crm/filterable-fields), never from a second hardcoded field list of its own.

    `ordered_options`/`non_ordered_options` implement the explicit-ordering design for
    fields like Check Size and Age Range: `ordered_options` is the hand-authored
    ascending list a gt/gte/lt/lte comparison is positioned against; `non_ordered_options`
    is every other value in `options` (e.g. "Other:", "Retired", "Deceased") -- valid for
    every other operator, but NEVER assigned an inferred ordinal position. Both are empty
    unless `ordered` is True.
    """

    key: str
    label: str
    category: str
    type: FilterFieldType
    storage_shape: str  # "scalar" | "list"
    source: str  # "core" | "thesis" | "custom"
    options: list[str] = Field(default_factory=list)  # empty = open-vocabulary (e.g. technologies)
    ordered: bool = False
    ordered_options: list[str] = Field(default_factory=list)
    non_ordered_options: list[str] = Field(default_factory=list)
    operators: list[str] = Field(default_factory=list)


class FilterCondition(BaseModel):
    field: str
    operator: str
    value: Any = None


class FilterSort(BaseModel):
    field: str
    direction: str = "asc"  # "asc" | "desc"


class FilterQuery(BaseModel):
    filters: list[FilterCondition] = Field(default_factory=list)
    logic: str = "AND"  # "AND" | "OR" -- applied across every condition in `filters`;
    # OR *within* one field (e.g. "State is Texas or California") is expressed via a
    # multi-value `value` on a single condition instead, not a second logic level --
    # deliberately flat, no nested groups, per the approved V1 scope.
    include_archived: bool = False
    page: int = 1
    page_size: int = 50
    sort: FilterSort | None = None
