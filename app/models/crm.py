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

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

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
        "thesis_private_check_sizes", "thesis_private_check_sizes_other",
        "thesis_private_deal_stages", "thesis_private_deal_stages_other",
        "thesis_private_meeting_preferences", "thesis_private_meeting_preferences_other",
        "thesis_private_demographic_preferences", "thesis_private_demographic_preferences_other",
        "thesis_private_other_criteria",
        "thesis_also_invests_institutionally",
        "thesis_institutional_asset_types", "thesis_institutional_asset_types_other",
        "thesis_institutional_business_models", "thesis_institutional_business_models_other",
        "thesis_institutional_industries", "thesis_institutional_industries_other",
        "thesis_institutional_check_sizes", "thesis_institutional_check_sizes_other",
        "thesis_institutional_deal_stages", "thesis_institutional_deal_stages_other",
        "thesis_institutional_meeting_preferences", "thesis_institutional_meeting_preferences_other",
        "thesis_institutional_demographic_preferences", "thesis_institutional_demographic_preferences_other",
        "thesis_institutional_other_criteria",
        "thesis_dietary_preferences", "thesis_referral_emails",
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

    thesis_dietary_preferences: str | None = None  # Q24
    thesis_referral_emails: str | None = None  # Q25, raw text (not split/parsed)

    # --- Group 3: custom fields ---
    custom_fields: dict[str, Any] = Field(default_factory=dict)


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
