"""
One-time reconciliation of Astronomic's pre-existing (pre-Phase-1) CRM
custom fields against this CRM's core/thesis/custom structure -- see the
reconciliation table agreed with the user, refined further after
reviewing a real 50-row export (`test crm.csv`). Operations, all safe to
re-run:

    seed_legacy_custom_fields() -- creates a CrmCustomFieldDefinition for
    every pre-existing field with no core/thesis equivalent. Skips any
    field_key that already exists.

    apply_custom_field_corrections() -- fixes definitions that were
    seeded with a wrong guessed type/options BEFORE the real CSV was
    reviewed (Accredited Status, Investor Type, Dinners Attended, How
    Early Do You Invest?) to match what the real data actually contains.
    Also carries later, non-legacy field-type corrections -- e.g. Age
    Range's text-to-single_select change -- since the mechanism (look up
    by field_key, patch field_type/options, leave contact rows alone) is
    identical either way. Idempotent -- re-applying the same correction
    is a no-op.

    migrate_all_contacts() -- for direct-duplicate fields (Deal Stage,
    Check Size, ...), copies any value already sitting under the OLD
    custom_fields key into the matching thesis field, ONLY if that thesis
    field is currently empty. Never touches the old key -- nothing is
    lost, and it's reversible by construction.

    translate_legacy_import_batch() -- CSV-import-specific: rewrites the
    five legacy multi-select thesis columns (comma-joined, abbreviated
    legacy wording) into semicolon-joined CANONICAL Investor Thesis text,
    re-tokenizes "How early do you invest?" using known-phrase matching
    instead of blind comma-splitting (two of its six real options contain
    their own internal comma, which a naive split would have shredded),
    and converts Investor Type/Dinners Attended commas to semicolons (no
    wording change -- they're custom fields with their own vocabulary).
    Operates on an already-uploaded CrmImportBatch's raw rows -- run this
    BEFORE preview()/commit() so the existing, unmodified semicolon-based
    splitting in crm_import_service.py does the rest. Deliberately kept
    OUT of crm_import_service.py itself: this is knowledge about
    Astronomic's specific old export, not a generic CSV-import concern.

    repair_all_contacts_comma_delimited_fields() -- one-time targeted
    repair for contacts committed BEFORE the Investor Type/Dinners
    Attended fix above existed (found via post-commit spot-check: multi-
    selection values were sitting as one comma-joined string instead of
    separate list items). Derives the correct value from each contact's
    OWN `source_snapshot`, never from the corrupted value. Touches only
    `custom_fields`/`updated_at`; leaves single-value contacts alone.

    migrate_all_dinner_subscriptions() -- rewrites Dinner Subscriptions'
    raw comma-joined free text into the normalized multi-select list, via
    normalize_dinner_subscriptions() (app/models/crm.py) -- the same
    function the CSV-import classification rule uses, so migrated and
    freshly-imported contacts always agree. Idempotent: a contact whose
    value is already a list is left alone. Legacy wording collapses into
    its mapped final option (deduped against anything already present);
    delete-only values are dropped, removing the key entirely if nothing
    valid remains.

    migrate_all_funding_stage_corruption() -- one-time repair for a
    2026-08-06 discovery: HEADER_ALIASES used to map BOTH "stage" and
    "funding stage" to the core `funding_stage` field (fixed in
    crm_import_service.py), so every past CSV commit that had a "Stage"
    column (outreach/engagement values -- Interested/Cold/Unresponsive/
    Replied/"(No Stage)") silently overwrote or filled funding_stage with
    that value instead of a real funding-round term. Clears funding_stage
    ONLY when it is byte-identical to THIS CONTACT'S OWN source_snapshot
    ["Stage"] value AND that value is one of the known engagement-stage-
    shaped strings -- the same "derive from own source_snapshot, prove it,
    never guess" pattern as repair_contact_comma_delimited_fields. A
    funding_stage value that doesn't mechanically prove this exact
    signature (e.g. a real term like "Seed"/"Series A") is left completely
    untouched -- confirmed via the 2026-08-06 audit that all 29 non-
    matching populated values are genuine funding-round terms from a
    different, legitimate source. Also fills engagement_stage from the
    same value, but ONLY when engagement_stage is currently empty AND the
    value is one of its live options -- CUSTOM_FIELD_CORRECTIONS added
    "Replied" as a real option once the audit confirmed it's a legitimate
    outreach stage (17 real occurrences), so those contacts DO get
    engagement_stage filled. "(No Stage)" (1 occurrence) is deliberately
    NOT an option -- it's a null/unset placeholder, not a real stage -- so
    that one contact gets funding_stage cleared but engagement_stage left
    blank rather than set to a fabricated value. Idempotent: once
    funding_stage is cleared, the exact-match precondition can never fire
    again for that contact.

Old single-value fields with no explicit "(Institutional)" counterpart
default to the PRIVATE thesis variant, mirroring "Check Size" (no suffix)
vs "Check Size (Institutional)" already being two distinct fields.
"""

from datetime import datetime, timezone
from typing import Any

from app.models.crm import (
    CrmContact,
    CrmImportBatch,
    CustomFieldType,
    DINNER_SUBSCRIPTION_OPTIONS,
    normalize_dinner_subscriptions,
)
from app.repositories.crm_contact_store import CrmContactStore
from app.services.crm_service import CrmService

# (field_key, label, field_type, description, options)
LEGACY_FIELD_SEEDS: list[tuple[str, str, CustomFieldType, str | None, list[str]]] = [
    ("gender", "Gender", CustomFieldType.TEXT, None, []),
    ("age_range", "Age Range", CustomFieldType.SINGLE_SELECT, None,
     ["18-22", "23-30", "31-40", "41-50", "51-60", "61-70", "71-80", "81+", "Retired", "Deceased"]),
    ("role", "Role", CustomFieldType.TEXT,
     "Their role/relationship within Astronomic -- distinct from job title.", []),
    ("accredited_status", "Accredited Status", CustomFieldType.SINGLE_SELECT, None,
     ["Yes", "No"]),
    ("investor_type", "Investor Type", CustomFieldType.MULTI_SELECT,
     "Investor archetype -- distinct from the Investor Thesis Form's Privately/Institutionally/Both. "
     "Multi-select: real contacts carry more than one of these simultaneously.",
     ["Angel Investor", "Family Office", "Fund LP", "I sponsor deals that I find", "Institutional Investor",
      "Invest with group of Angels", "Participate in syndicated investments", "Private Equity",
      "Private Investor", "Venture Capital"]),
    ("how_early_do_you_invest", "How Early Do You Invest?", CustomFieldType.MULTI_SELECT,
     "Two of these six options contain their own internal comma -- see HOW_EARLY_KNOWN_PHRASES; "
     "CSV import re-tokenizes by known-phrase match, never by blind comma-split.",
     ["Great team, no revenue", "Great team, some revenue", "$10k-$50k MRR / GMV",
      "$50k-$100k MRR / GMV", "$100k-$1M MRR / GMV", "$1M+ MRR / GMV"]),
    ("how_often_do_you_invest", "How Often Do You Invest?", CustomFieldType.TEXT, None, []),
    ("notes", "Notes", CustomFieldType.LONG_TEXT, "General CRM/investment/context notes.", []),
    ("personal_notes", "Personal Notes", CustomFieldType.LONG_TEXT, "Personal/relationship context.", []),
    ("sub_industry", "Sub-Industry", CustomFieldType.TEXT,
     "Finer-grained than the core `industry` field.", []),
    ("do_not_invest_in", "Do Not Invest In", CustomFieldType.LONG_TEXT,
     "Explicit exclusions -- the Thesis Form only captures what they DO invest in.", []),
    ("dinner_subscriptions", "Dinner Subscriptions", CustomFieldType.MULTI_SELECT, None, DINNER_SUBSCRIPTION_OPTIONS),
    ("dinners_attended", "Dinners Attended", CustomFieldType.MULTI_SELECT,
     "Not a stable closed set -- every new dinner adds a value. Options need manual upkeep over time.",
     ["Investor Dinners", "Fireside Dinners", "Founder Dinners", "Biz Dev Dinners",
      "Alpha Rose [08.13.2025] Austin", "Civilization Fund [01.19.2026] Austin",
      "Colony Hills Capital [09.16.2025] Austin", "Dripping Springs [03.24.2026] Austin",
      "Ensitech [11.13.2025] Austin", "Flex Radio [10.09.2025] Austin", "GeneSilico [08.21.2025] Austin",
      "Innovosens [06.23.2026] Austin", "Lake Hour [10.20.2025] Austin", "Leon Y Sol [11.06.2025] Austin",
      "Meghani Capital [09.24.2025] Austin", "Offerd [06.25.2025] Austin", "Predict RX [03.10.2026] Austin",
      "Quantum Mobility [10.08.2025] Austin", "RIoT Technology [03.05.2026] Austin",
      "Raveum [01.12.2026] Austin", "Realize Music - [04.15.2026] Austin", "Rush [12.08.2025] Austin",
      "Savvy [2.25.2025] Austin", "Submersive [04.30.2026] Austin", "Talent Stream [06.16.2026] SF",
      "Valorem Capital [02.18.26] SF", "Valorem Capital [03.12.26] Austin"]),
    ("referred_to_constellation_dinners_by", "Who were you referred to Constellation Dinners by?",
     CustomFieldType.TEXT, None, []),
    ("chris_knows_personally", "Chris Knows Personally", CustomFieldType.BOOLEAN, None, []),
    ("chris_degree_connection", "Chris Degree Connection", CustomFieldType.TEXT, None, []),
    # Added after reviewing the real 50-row export. "Replied" was missing until the
    # 2026-08-06 two-CSV audit -- it never appeared in that original 50-row export,
    # only in the larger 501-row one (17 real occurrences). "(No Stage)" (1 occurrence,
    # also only in the larger export) is a null/unset placeholder, not a real stage --
    # deliberately NOT added as an option; a contact whose Stage is "(No Stage)" gets
    # engagement_stage left blank, never a literal "(No Stage)" value.
    ("engagement_stage", "Engagement Stage", CustomFieldType.SINGLE_SELECT,
     "Our own outreach/engagement pipeline stage -- NOT a funding stage.",
     ["Cold", "Interested", "Unresponsive", "Replied"]),
    ("do_not_call", "Do Not Call", CustomFieldType.BOOLEAN, None, []),
    ("secondary_email", "Secondary Email", CustomFieldType.TEXT, None, []),
    ("revenue_stage", "Revenue Stage", CustomFieldType.SINGLE_SELECT,
     "The contact's own company's revenue stage -- distinct from Deal Stage (their investing preference).",
     ["$250K - $500K", "$500k - $1M", "$1M - $10M", "$10M - $100M"]),
    ("last_raised_at", "Last Raised At", CustomFieldType.DATE, None, []),
    ("investment_geography_preference", "Investment Geography Preference", CustomFieldType.TEXT,
     "Free text -- the source data's formatting was too inconsistent for a clean option list.", []),
    ("qualify_contact", "Qualify Contact", CustomFieldType.TEXT,
     "No real values existed in the reviewed export -- type is a best guess.", []),
    ("total_funding", "Total Funding", CustomFieldType.NUMBER,
     "Cumulative lifetime funding raised -- distinct from `funding_amount` (latest round only).", []),
    ("work_direct_phone", "Work Direct Phone", CustomFieldType.TEXT, None, []),
    ("corporate_phone", "Corporate Phone", CustomFieldType.TEXT, None, []),
]

# field_key -> (corrected_field_type, corrected_options) -- fixes definitions that were
# seeded with a wrong guess before the real CSV was available to check against.
CUSTOM_FIELD_CORRECTIONS: dict[str, tuple[CustomFieldType, list[str]]] = {
    "accredited_status": (CustomFieldType.SINGLE_SELECT, ["Yes", "No"]),
    # "Replied" confirmed via the 2026-08-06 two-CSV audit: 17 real occurrences, only in
    # the larger 501-row export -- missed originally because the 50-row export this field
    # was first reviewed against had none. Deliberately does NOT add "(No Stage)" -- that's
    # a null/unset placeholder (1 occurrence), not a real stage; a contact with that raw
    # value gets engagement_stage left blank rather than a fabricated literal option.
    "engagement_stage": (CustomFieldType.SINGLE_SELECT, ["Cold", "Interested", "Unresponsive", "Replied"]),
    "age_range": (CustomFieldType.SINGLE_SELECT,
                  ["18-22", "23-30", "31-40", "41-50", "51-60", "61-70", "71-80", "81+", "Retired", "Deceased"]),
    "dinner_subscriptions": (CustomFieldType.MULTI_SELECT, DINNER_SUBSCRIPTION_OPTIONS),
    "investor_type": (CustomFieldType.MULTI_SELECT, [
        "Angel Investor", "Family Office", "Fund LP", "I sponsor deals that I find", "Institutional Investor",
        "Invest with group of Angels", "Participate in syndicated investments", "Private Equity",
        "Private Investor", "Venture Capital",
    ]),
    "dinners_attended": (CustomFieldType.MULTI_SELECT, [
        "Investor Dinners", "Fireside Dinners", "Founder Dinners", "Biz Dev Dinners",
        "Alpha Rose [08.13.2025] Austin", "Civilization Fund [01.19.2026] Austin",
        "Colony Hills Capital [09.16.2025] Austin", "Dripping Springs [03.24.2026] Austin",
        "Ensitech [11.13.2025] Austin", "Flex Radio [10.09.2025] Austin", "GeneSilico [08.21.2025] Austin",
        "Innovosens [06.23.2026] Austin", "Lake Hour [10.20.2025] Austin", "Leon Y Sol [11.06.2025] Austin",
        "Meghani Capital [09.24.2025] Austin", "Offerd [06.25.2025] Austin", "Predict RX [03.10.2026] Austin",
        "Quantum Mobility [10.08.2025] Austin", "RIoT Technology [03.05.2026] Austin",
        "Raveum [01.12.2026] Austin", "Realize Music - [04.15.2026] Austin", "Rush [12.08.2025] Austin",
        "Savvy [2.25.2025] Austin", "Submersive [04.30.2026] Austin", "Talent Stream [06.16.2026] SF",
        "Valorem Capital [02.18.26] SF", "Valorem Capital [03.12.26] Austin",
    ]),
    "how_early_do_you_invest": (CustomFieldType.MULTI_SELECT, [
        "Great team, no revenue", "Great team, some revenue", "$10k-$50k MRR / GMV",
        "$50k-$100k MRR / GMV", "$100k-$1M MRR / GMV", "$1M+ MRR / GMV",
    ]),
}

# old custom_fields key -> (canonical CrmContact field, is_list_field)
DIRECT_DUPLICATE_MIGRATIONS: dict[str, tuple[str, bool]] = {
    "deal_stage": ("thesis_private_deal_stages", True),
    "check_size": ("thesis_private_check_sizes", True),
    "check_size_institutional": ("thesis_institutional_check_sizes", True),
    "investing_asset_types": ("thesis_private_asset_types", True),
    "investing_business_models": ("thesis_private_business_models", True),
    "founder_diversity_preference": ("thesis_private_demographic_preferences", True),
    "would_like_to_meet_founders_by": ("thesis_private_meeting_preferences", True),
    "dietary_restrictions": ("thesis_dietary_preferences", False),
}

# --- CSV-import-specific: legacy (abbreviated) -> canonical Investor Thesis Form text.
# Verified against every unique value actually found in the real 50-row export -- every
# legacy token below has a confirmed, unambiguous canonical match; nothing here is guessed.

_DEAL_STAGE_LEGACY_MAP = {
    "friends & family": "Friends & Family (idea or concept stage, often pre-incorporation)",
    "pre-seed": "Pre-Seed (early development, pre-revenue or minimal traction)",
    "seed": "Seed (product in market, early customers or pilots)",
    "series a": "Series A (scaling phase, revenue traction, team expansion)",
    "series b": "Series B or later (growth or expansion stage, institutional rounds)",
    "series c": "Series B or later (growth or expansion stage, institutional rounds)",
    # Raw data contains "Series C,D,E" (shorthand, no repeated "Series") -- after the normal
    # comma-split these arrive as bare "D"/"E" tokens. User-confirmed reading: Series D / Series E.
    "d": "Series B or later (growth or expansion stage, institutional rounds)",
    "e": "Series B or later (growth or expansion stage, institutional rounds)",
    "fund lp": "Fund LP (investor in venture/private equity funds)",
    "secondary": "Secondary (buying equity from early investors or founders)",
    "growth equity": "Growth Equity (post-Series B+, but still private)",
    "pre-ipo / late-stage private": "Pre-IPO / Late-Stage Private (companies nearing exit or IPO)",
}

_ASSET_TYPE_LEGACY_MAP = {
    "carbon credits / esg investments": "Carbon credits / ESG investments",
    "collectibles": "Collectibles (e.g., art, wine, watches)",
    "commodities": "Commodities (e.g., gold, oil, agriculture)",
    "cryptocurrency / digital assets": "Cryptocurrency / digital assets",
    "fund-of-funds": "Fund-of-funds",
    "hedge funds": "Hedge funds (multi-asset strategies)",
    "infrastructure": "Infrastructure (e.g., toll roads, utilities, airports)",
    "private credit": "Private credit (e.g., private loans, direct lending)",
    "private equity": "Private equity",
    "public equities": "Public equities (stocks, ETFs)",
    "real estate": "Real estate (direct ownership, syndications, REITs)",
    "royalty financing": "Royalty financing",
    "search funds / eta": "Search funds / ETA (entrepreneurship through acquisition)",
    "secondaries": "Secondaries (e.g., buying/selling LP interests or founder equity)",
    "structured products": "Structured products (e.g., notes, derivatives)",
    "venture capital": "Venture capital (e.g., angel checks, early-stage startups, high-growth tech)",
}

_BUSINESS_MODEL_LEGACY_MAP = {
    "agencies / services": "Agencies / Services (e.g., marketing, development firms)",
    "brokerages": "Brokerages",
    "community-based / network-led growth": "Community-based / Network-led growth",
    "creator-led / influencer brands": "Creator-led / Influencer brands",
    "direct-to-consumer (dtc) ecommerce": "Direct-to-Consumer (DTC) eCommerce",
    "franchises": "Franchises",
    "hardware / physical products": "Hardware / Physical products",
    "licensing / ip-based": "Licensing / IP-based",
    "manufacturing": "Manufacturing",
    "marketplaces": "Marketplaces (e.g., Airbnb, Uber-style platforms)",
    "software as a service (saas)": "Software as a Service (SaaS)",
    "subscription / membership businesses": "Subscription / Membership businesses",
    "web3 / tokenized business models": "Web3 / Tokenized business models",
}

_DEMOGRAPHIC_PREFERENCE_LEGACY_MAP = {
    "open to investing in anyone": "I'm open to investing in anyone",
    "female founders": "I prefer female fundraisers",
    "male founders": "I prefer male fundraisers",
    "black founders": "I prefer black fundraisers",
    "latino founders": "I prefer Latino fundraisers",
    "asian founders": "I prefer Asian fundraisers",
    "indian founders": "I prefer Indian fundraisers",
    "lgbtq+": "I prefer LGBTQ+ fundraisers",
    "military founders": "I prefer military fundraisers",
}

_MEETING_PREFERENCE_LEGACY_MAP = {
    "email intro": "In an email intro",
    "host a dinner at my house": "I'd host a dinner at my house",
    "meet them at a restaurant or coffee shop": "I'd meet them at a restaurant or coffee shop",
    "zoom call": "I'd do a Zoom call",
}

# CSV header (exact, as this export spells it) -> its legacy->canonical value map.
LEGACY_THESIS_COLUMN_VALUE_MAPS: dict[str, dict[str, str]] = {
    "Deal Stage": _DEAL_STAGE_LEGACY_MAP,
    "Investing in these types of assets": _ASSET_TYPE_LEGACY_MAP,
    "Investing in these business models:": _BUSINESS_MODEL_LEGACY_MAP,
    "Founder Diversity Preference": _DEMOGRAPHIC_PREFERENCE_LEGACY_MAP,
    "Would like to meet founders by": _MEETING_PREFERENCE_LEGACY_MAP,
}

HOW_EARLY_COLUMN = "How early do you invest?"
HOW_EARLY_KNOWN_PHRASES = [
    "Great team, no revenue",
    "Great team, some revenue",
    "$10k-$50k MRR / GMV",
    "$50k-$100k MRR / GMV",
    "$100k-$1M MRR / GMV",
    "$1M+ MRR / GMV",
]

# Custom fields (own vocabulary, no thesis-canonical rewording needed) whose raw CSV
# values are ALSO comma-delimited -- pure delimiter conversion only, unlike
# LEGACY_THESIS_COLUMN_VALUE_MAPS above which also translates wording. Originally missed
# when the five thesis columns were fixed -- found via post-commit spot-check, when
# Investor Type/Dinners Attended values with more than one selection turned out to still
# be single comma-joined strings instead of separate list items.
COMMA_TO_SEMICOLON_ONLY_COLUMNS = ["Investor type", "Dinners Attended"]

# CSV column -> custom_fields key, for the same two fields -- used by the one-time
# targeted repair below (repair_contact_comma_delimited_fields), not by the import path.
REPAIR_COMMA_DELIMITED_CUSTOM_FIELDS = {
    "Investor type": "investor_type",
    "Dinners Attended": "dinners_attended",
}


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (str, list, dict, tuple, set)):
        return len(value) == 0
    return False


async def seed_legacy_custom_fields(crm_service: CrmService) -> dict[str, list[str]]:
    created: list[str] = []
    skipped: list[str] = []
    for field_key, label, field_type, description, options in LEGACY_FIELD_SEEDS:
        if await crm_service.custom_field_store.get_by_field_key(field_key) is not None:
            skipped.append(field_key)
            continue
        await crm_service.create_custom_field(
            field_key=field_key, label=label, field_type=field_type, description=description, options=options
        )
        created.append(field_key)
    return {"created": created, "skipped": skipped}


async def apply_custom_field_corrections(crm_service: CrmService) -> list[str]:
    """Fixes already-seeded definitions whose type/options were guessed wrong before the
    real CSV was reviewed. Idempotent -- re-applying an already-correct definition is a no-op
    (still counted as "corrected" in the return list, since the check is cheap and the
    caller only needs to know it ran, not whether it actually changed anything)."""
    corrected: list[str] = []
    for field_key, (field_type, options) in CUSTOM_FIELD_CORRECTIONS.items():
        definition = await crm_service.custom_field_store.get_by_field_key(field_key)
        if definition is None:
            continue  # not seeded yet -- seed_legacy_custom_fields() runs first in reconcile_legacy_fields()
        await crm_service.update_custom_field(
            definition.crm_custom_field_id, {"field_type": field_type, "options": options}
        )
        corrected.append(field_key)
    return corrected


def migrate_contact_legacy_fields(contact: CrmContact) -> tuple[CrmContact, list[str]]:
    """Pure function -- does not save. Returns (possibly-updated contact, list of old
    keys that were migrated). Never mutates `contact.custom_fields` -- the old values
    stay exactly where they were."""
    updates: dict[str, Any] = {}
    migrated: list[str] = []

    for old_key, (new_field, is_list) in DIRECT_DUPLICATE_MIGRATIONS.items():
        old_value = contact.custom_fields.get(old_key)
        if _is_empty(old_value):
            continue

        current_new_value = updates.get(new_field, getattr(contact, new_field))
        if not _is_empty(current_new_value):
            continue  # never overwrite an existing thesis value -- same rule as every other import merge

        if is_list and not isinstance(old_value, list):
            old_value = [old_value]

        updates[new_field] = old_value
        migrated.append(old_key)

    if not updates:
        return contact, []

    updates["updated_at"] = datetime.now(timezone.utc)
    return contact.model_copy(update=updates), migrated


async def migrate_all_contacts(contact_store: CrmContactStore) -> dict[str, int]:
    contacts = await contact_store.list()
    contacts_updated = 0
    fields_migrated = 0

    for contact in contacts:
        updated, migrated = migrate_contact_legacy_fields(contact)
        if migrated:
            await contact_store.save(updated)
            contacts_updated += 1
            fields_migrated += len(migrated)

    return {"contacts_scanned": len(contacts), "contacts_updated": contacts_updated, "fields_migrated": fields_migrated}


def migrate_contact_dinner_subscriptions(contact: CrmContact) -> tuple[CrmContact, bool]:
    """
    Pure function -- does not save. Dinner Subscriptions started as free
    text (comma-joined when a contact had more than one subscription);
    this rewrites that raw string into the normalized multi-select list via
    normalize_dinner_subscriptions() (the SAME function the CSV-import
    classification rule uses -- see crm_classification_rules.py -- so a
    migrated contact and a freshly-imported one always agree). Idempotent:
    a contact whose value is already a list (already migrated, or created
    fresh under the new field type) is left untouched -- the isinstance
    check IS the "already migrated" signal, no separate flag needed. If
    every token a contact had was delete-only, the key is removed entirely
    (matching _apply_mapping's own convention of never storing an empty
    list) rather than left as `[]`.
    """
    raw = contact.custom_fields.get("dinner_subscriptions")
    if not isinstance(raw, str) or not raw.strip():
        return contact, False

    tokens = [t.strip() for t in raw.split(",") if t.strip()]
    normalized = normalize_dinner_subscriptions(tokens)

    new_custom_fields = dict(contact.custom_fields)
    if normalized:
        new_custom_fields["dinner_subscriptions"] = normalized
    else:
        del new_custom_fields["dinner_subscriptions"]

    updated = contact.model_copy(
        update={"custom_fields": new_custom_fields, "updated_at": datetime.now(timezone.utc)}
    )
    return updated, True


async def migrate_all_dinner_subscriptions(contact_store: CrmContactStore) -> dict[str, int]:
    contacts = await contact_store.list()
    contacts_updated = 0

    for contact in contacts:
        updated, changed = migrate_contact_dinner_subscriptions(contact)
        if changed:
            await contact_store.save(updated)
            contacts_updated += 1

    return {"dinner_subscriptions_contacts_scanned": len(contacts), "dinner_subscriptions_contacts_updated": contacts_updated}


# funding_stage values that are shaped like an engagement_stage answer, not a real
# funding-round term -- confirmed via the 2026-08-06 audit as the exhaustive set of
# raw "Stage" values actually found in the two real CSVs.
FUNDING_STAGE_ENGAGEMENT_SHAPED_VALUES = frozenset(
    {"Interested", "Cold", "Unresponsive", "Replied", "(No Stage)"}
)


def migrate_contact_funding_stage_corruption(
    contact: CrmContact, engagement_stage_options: set[str]
) -> tuple[CrmContact, str]:
    """
    Pure function -- does not save. Returns (possibly-updated contact,
    outcome) where outcome is one of:
      "not_populated"     -- funding_stage was already empty, nothing to do.
      "legitimate"        -- funding_stage doesn't look like an engagement
                              value at all; never touched.
      "ambiguous"         -- funding_stage LOOKS engagement-shaped, but
                              doesn't exactly match this contact's own
                              source_snapshot Stage value -- flagged, never
                              touched, since the corruption can't be
                              mechanically proven for this specific row.
      "cleared"           -- proven corrupted; funding_stage cleared,
                              engagement_stage NOT touched (already
                              populated, or the value isn't one of its
                              live options).
      "cleared_and_engagement_set" -- proven corrupted; funding_stage
                              cleared AND engagement_stage filled in from
                              the same value (was empty, value is valid).
    """
    current = contact.funding_stage
    if not current:
        return contact, "not_populated"
    if current not in FUNDING_STAGE_ENGAGEMENT_SHAPED_VALUES:
        return contact, "legitimate"

    stage_raw = (contact.source_snapshot.get("Stage") or "").strip()
    if stage_raw != current:
        return contact, "ambiguous"

    updates: dict[str, Any] = {"funding_stage": None}
    outcome = "cleared"
    if not contact.custom_fields.get("engagement_stage") and current in engagement_stage_options:
        updates["custom_fields"] = {**contact.custom_fields, "engagement_stage": current}
        outcome = "cleared_and_engagement_set"

    updates["updated_at"] = datetime.now(timezone.utc)
    return contact.model_copy(update=updates), outcome


async def migrate_all_funding_stage_corruption(
    contact_store: CrmContactStore, engagement_stage_options: set[str]
) -> dict[str, int]:
    contacts = await contact_store.list()
    counts = {
        "not_populated": 0, "legitimate": 0, "ambiguous": 0, "cleared": 0, "cleared_and_engagement_set": 0,
    }
    for contact in contacts:
        updated, outcome = migrate_contact_funding_stage_corruption(contact, engagement_stage_options)
        counts[outcome] += 1
        if outcome in ("cleared", "cleared_and_engagement_set"):
            await contact_store.save(updated)
    return {
        "funding_stage_contacts_scanned": len(contacts),
        "funding_stage_legitimate": counts["legitimate"],
        "funding_stage_ambiguous": counts["ambiguous"],
        "funding_stage_cleared": counts["cleared"] + counts["cleared_and_engagement_set"],
        "funding_stage_engagement_stage_set": counts["cleared_and_engagement_set"],
    }


async def reconcile_legacy_fields(crm_service: CrmService) -> dict[str, Any]:
    """The single entry point: seed new definitions, correct previously-guessed ones,
    migrate any values already sitting under the old keys, normalize Dinner
    Subscriptions' free-text values into the multi-select representation, then clear
    any funding_stage value proven corrupted by the old Stage->funding_stage mapping
    bug. Safe to call any number of times."""
    seed_report = await seed_legacy_custom_fields(crm_service)
    corrected = await apply_custom_field_corrections(crm_service)
    migrate_report = await migrate_all_contacts(crm_service.contact_store)
    dinner_subscriptions_report = await migrate_all_dinner_subscriptions(crm_service.contact_store)
    engagement_stage_field = await crm_service.custom_field_store.get_by_field_key("engagement_stage")
    engagement_stage_options = set(engagement_stage_field.options) if engagement_stage_field else set()
    funding_stage_report = await migrate_all_funding_stage_corruption(
        crm_service.contact_store, engagement_stage_options
    )
    return {
        **seed_report, "corrected": corrected, **migrate_report,
        **dinner_subscriptions_report, **funding_stage_report,
    }


def _translate_comma_joined_column(raw_value: str, legacy_map: dict[str, str]) -> str:
    """Comma-splits (verified safe for these 5 columns -- no individual legacy value
    contains an internal comma), translates each token through the legacy->canonical
    map case-insensitively, and rejoins with semicolons for the standard multi-value
    pipeline in crm_import_service.py to split again at coercion time. An unrecognized
    token is preserved verbatim rather than dropped, though this shouldn't happen here --
    every real token in the reviewed export was verified against these maps."""
    tokens = [t.strip() for t in raw_value.split(",") if t.strip()]
    translated = [legacy_map.get(t.lower(), t) for t in tokens]
    return ";".join(translated)


def _retokenize_known_phrases(raw_value: str, known_phrases: list[str]) -> list[str]:
    """
    Splits a comma-joined legacy cell into its real atomic values when some of
    those values themselves contain a comma (naive comma-splitting would break
    them) -- greedily matches the LONGEST known phrase at each position, so
    "Great team, some revenue" is matched whole before its internal comma is
    ever treated as a separator. Stops (rather than guessing) on an
    unrecognized fragment.
    """
    remaining = raw_value.strip()
    phrases_by_length = sorted(known_phrases, key=len, reverse=True)
    found: list[str] = []
    while remaining:
        remaining = remaining.lstrip(",").strip()
        if not remaining:
            break
        matched = next((p for p in phrases_by_length if remaining.startswith(p)), None)
        if matched is None:
            break
        found.append(matched)
        remaining = remaining[len(matched):]
    return found


def _convert_comma_to_semicolon(raw_value: str) -> str:
    """Pure delimiter conversion, no value rewording -- for custom fields with their
    own vocabulary (Investor Type, Dinners Attended), unlike the thesis columns above
    which also need their wording translated to the canonical form."""
    tokens = [t.strip() for t in raw_value.split(",") if t.strip()]
    return ";".join(tokens)


def translate_legacy_import_batch(batch: CrmImportBatch) -> CrmImportBatch:
    """
    Rewrites the raw rows of an uploaded CrmImportBatch: the five legacy
    multi-select thesis columns become semicolon-joined canonical text,
    "How early do you invest?" is re-tokenized by known-phrase match, and
    Investor Type/Dinners Attended get their commas converted to semicolons
    (no wording change). Returns a NEW batch (does not save) -- caller
    persists it via batch_store.save() before calling preview(). Every
    other column is untouched.
    """
    new_rows = []
    for row in batch.rows:
        new_row = dict(row)
        for column, legacy_map in LEGACY_THESIS_COLUMN_VALUE_MAPS.items():
            if column in new_row and new_row[column].strip():
                new_row[column] = _translate_comma_joined_column(new_row[column], legacy_map)
        if HOW_EARLY_COLUMN in new_row and new_row[HOW_EARLY_COLUMN].strip():
            phrases = _retokenize_known_phrases(new_row[HOW_EARLY_COLUMN], HOW_EARLY_KNOWN_PHRASES)
            new_row[HOW_EARLY_COLUMN] = ";".join(phrases)
        for column in COMMA_TO_SEMICOLON_ONLY_COLUMNS:
            if column in new_row and new_row[column].strip():
                new_row[column] = _convert_comma_to_semicolon(new_row[column])
        new_rows.append(new_row)
    return batch.model_copy(update={"rows": new_rows})


def repair_contact_comma_delimited_fields(contact: CrmContact) -> tuple[CrmContact, list[str]]:
    """
    One-time targeted repair for contacts committed BEFORE
    COMMA_TO_SEMICOLON_ONLY_COLUMNS existed: Investor Type/Dinners Attended
    with more than one real selection were stored as a single comma-joined
    string inside a one-item list instead of separate list items.

    Derives the correct value from THIS CONTACT'S OWN `source_snapshot`
    (the untouched original CSV row, captured at commit time) -- never
    from the already-corrupted custom_fields value. Only touches a field
    when the source data genuinely contains more than one value; a
    contact whose source only ever had one real selection is left
    completely untouched. Changes nothing but `custom_fields` and
    `updated_at` -- no other field on the contact is read or written.
    """
    repaired_fields: list[str] = []
    new_custom_fields = dict(contact.custom_fields)

    for csv_column, field_key in REPAIR_COMMA_DELIMITED_CUSTOM_FIELDS.items():
        raw_value = contact.source_snapshot.get(csv_column) or ""
        tokens = [t.strip() for t in raw_value.split(",") if t.strip()]
        if len(tokens) <= 1:
            continue  # genuinely single-value (or empty) -- nothing to repair
        if new_custom_fields.get(field_key) == tokens:
            continue  # already correct -- no-op
        new_custom_fields[field_key] = tokens
        repaired_fields.append(field_key)

    if not repaired_fields:
        return contact, []

    updated = contact.model_copy(
        update={"custom_fields": new_custom_fields, "updated_at": datetime.now(timezone.utc)}
    )
    return updated, repaired_fields


async def repair_all_contacts_comma_delimited_fields(contact_store: CrmContactStore) -> dict[str, int]:
    contacts = await contact_store.list()
    contacts_touched = 0
    investor_type_repaired = 0
    dinners_attended_repaired = 0

    for contact in contacts:
        updated, repaired_fields = repair_contact_comma_delimited_fields(contact)
        if repaired_fields:
            await contact_store.save(updated)
            contacts_touched += 1
            if "investor_type" in repaired_fields:
                investor_type_repaired += 1
            if "dinners_attended" in repaired_fields:
                dinners_attended_repaired += 1

    return {
        "contacts_scanned": len(contacts),
        "contacts_touched": contacts_touched,
        "investor_type_repaired": investor_type_repaired,
        "dinners_attended_repaired": dinners_attended_repaired,
    }
