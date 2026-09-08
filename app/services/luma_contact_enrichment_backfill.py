"""
Historical, idempotent, dry-run-capable driver for the Luma self-report
Company/Job Title enrichment (see app/services/luma_contact_enrichment.py
for the shared resolution/merge rules this reuses verbatim -- no separate
backfill algorithm exists here, only orchestration + counting).

Reads every already-stored LumaRegistration (no Luma API calls -- historical
`registration_answers[]` are already persisted, see the architecture
report) and every non-archived CrmContact, groups registrations by
`crm_contact_id`, and for each contact-with-registrations calls the exact
same `resolve_contact_luma_fields()` + `apply_luma_self_report()` the live
webhook path calls. `dry_run=True` (the default) computes everything and
writes NOTHING -- no CrmContact save, no LumaRegistration mutation.

ABSOLUTELY zero Activity Log writes, in EITHER mode -- this module takes
no audit-log-service dependency at all (no import, no constructor
parameter, no call site anywhere below); a dedicated structural test in
this feature's own test file scans this module's source and fails the
moment that ever stops being true. "luma.contact.enrichment_ambiguous"
(the live webhook path's own audit event for this same condition) is
therefore structurally impossible to fire from here -- an ambiguous
history is reported ONLY in this module's own in-memory BackfillReport
(`ambiguous_contact_ids`), never persisted as an event.

Idempotent by construction: re-running with unchanged Luma data
recomputes the identical resolution from the identical stored data and
produces zero additional changes (the merge function's own
`if not updates: return unchanged` early-out) -- and, per the V1
correction below, a Contact whose Luma-resolved Company/Title/Website all
already match its current values now produces that exact same "zero
updates" outcome, never a provenance-only write.

Company Website (V1, corrected): an existing NONBLANK company_website is
NEVER cleared/overwritten by this driver, even when Company changes --
see apply_luma_self_report's own docstring for why (no reliable
provenance on an existing website means we cannot safely guess whether it
survived a Company change). `existing_websites_flagged_for_review` counts
exactly the contacts that would previously have had their website
DESTRUCTIVELY CLEARED -- they are now left completely untouched instead,
flagged for a human review queue. Tier 1 may only populate a company_
website that is CURRENTLY BLANK. Tier 2 (email-domain) is computed for
EVERY examined contact purely for reporting/evaluation -- never written,
regardless of dry_run.

`contacts_would_update_*_from_unknown_recency`: how many of the proposed
updates rest on a SINGLE, uncontested UNKNOWN-recency answer (not a
known-recency one) -- surfaced separately so a human can judge how much
of a proposed backfill depends on weak evidence before approving real
writes, without having to cross-reference every example by hand.

Suspicious-case flags (`BackfillExample.flags`) are deliberately
conservative and NEVER block/alter anything -- purely a note for the
human reviewer, same spirit as scripts/audit_contact_names.py's own
flagging. A contact can carry zero or several flags.

`excluded_contact_ids` (backfill-only precaution): an explicit set of
FULL CrmContact IDs to skip entirely for THIS run -- no resolution is
computed, apply_luma_self_report() is never called, no
Contact/provenance/Activity Log change of any kind occurs for an
excluded Contact, and it is never added to any other count/bucket
(would_update_*/unchanged/ambiguous/website_*/examples) -- it simply
does not exist as far as the rest of this function is concerned, other
than being counted in `contacts_excluded` and listed in
`excluded_contact_ids`. This is a ONE-RUN, IN-MEMORY parameter only --
nothing is persisted, no new CrmContact field, no general suppression
mechanism, and it has zero effect on the live webhook path (which never
passes this parameter at all -- see luma_sync_service.py's own call
site, unmodified by this capability). Registration-level counts
(registrations_examined, the recency-tier tallies) are computed from the
RAW registration data before grouping and are deliberately UNAFFECTED by
exclusion -- they describe the dataset, not what got processed.
Resolving a short ID prefix to a full CrmContact ID is the CALLER's
job (see scripts/run_luma_contact_enrichment_backfill.py's own
prefix-resolution, which fails closed on zero or multiple matches) --
this function only ever accepts and compares full IDs.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from app.models.crm import CrmContact
from app.models.luma import LumaEvent, LumaRegistration
from app.repositories.crm_contact_store import CrmContactStore
from app.repositories.luma_event_store import LumaEventStore
from app.repositories.luma_registration_store import LumaRegistrationStore
from app.services.luma_contact_enrichment import (
    RecencyTier,
    apply_luma_self_report,
    compute_registration_recency,
    extract_company_question_answer,
    normalize_company_name,
    normalize_website_domain,
    resolve_contact_luma_fields,
    resolve_tier1_website,
    resolve_tier2_email_domain_candidate,
)

# Conservative, small, and reused for BOTH company and title -- flagging
# only, never blocking. Deliberately excludes anything that could be a
# real self-reported value (e.g. "self-employed", "freelance", "retired"
# are real answers, not placeholders).
_PLACEHOLDER_VALUES = frozenset({"n/a", "na", "none", "unknown", "test", "asdf", "-", "--", "---", "tbd", ".", "?", "x"})


@dataclass
class BackfillCounts:
    registrations_examined: int = 0
    unique_contacts_represented: int = 0
    unresolved_registrations_skipped: int = 0
    registrations_using_registered_at: int = 0
    registrations_using_joined_at: int = 0
    registrations_using_event_start_at: int = 0
    registrations_with_unknown_recency: int = 0
    contacts_multiple_valid_company_answers: int = 0
    contacts_multiple_valid_title_answers: int = 0
    contacts_ambiguous_unknown_recency: int = 0
    contacts_would_update_company: int = 0
    contacts_would_update_title: int = 0
    contacts_would_update_both: int = 0
    contacts_unchanged: int = 0  # zero Contact save would occur for these
    # How many of the above updates rest on a single, uncontested
    # UNKNOWN-recency answer rather than a known-recency one -- see module
    # docstring. contacts_would_update_from_unknown_recency is the UNION
    # (a contact counted once even if BOTH its company and title updates
    # happened to come from unknown recency).
    contacts_would_update_company_from_unknown_recency: int = 0
    contacts_would_update_title_from_unknown_recency: int = 0
    contacts_would_update_from_unknown_recency: int = 0
    # Company Website, V1 corrected semantics -- see module docstring.
    # existing_websites_flagged_for_review is EXACTLY the set that would
    # previously have been destructively cleared; every one of them is
    # left completely untouched instead (preserved), not overwritten by
    # either Tier 1 or Tier 2.
    existing_websites_flagged_for_review: int = 0
    websites_populated_from_blank_tier1: int = 0
    websites_tier1_ambiguous: int = 0
    websites_tier2_candidates: int = 0
    websites_tier2_free_excluded: int = 0
    websites_still_unresolved: int = 0
    contacts_saved: int = 0  # only nonzero when dry_run=False
    # Backfill-only precaution -- see module docstring. Counts Contacts in
    # `excluded_contact_ids` that were ACTUALLY present in this run's
    # dataset (had at least one registration) and were therefore really
    # skipped; a requested exclusion ID with no registrations at all has
    # nothing to skip and is not counted here (see
    # BackfillReport.excluded_contact_ids_not_found for transparency on
    # that case instead).
    contacts_excluded: int = 0


@dataclass
class BackfillExample:
    """Deliberately no email/raw payload -- IDs and the actual before/
    after field values (which are the whole point of a review sample),
    nothing more. Company and Title recency are reported SEPARATELY
    (never collapsed into one "dominant" tier) since they're resolved
    completely independently and can legitimately differ. `new_website`
    is populated ONLY when company_website itself actually changed this
    round (always a blank -> Tier 1 fill, per V1's corrected semantics --
    never a clear/overwrite of an existing value); `tier2_candidate_fyi`
    is ALWAYS computed for evaluation and NEVER applied, regardless of
    whether the existing website is blank or already set."""

    crm_contact_id: str
    old_company: str | None
    new_company: str | None
    company_recency_tier: str | None
    company_recency_at: str | None
    old_title: str | None
    new_title: str | None
    title_recency_tier: str | None
    title_recency_at: str | None
    old_website: str | None
    new_website: str | None
    tier2_candidate_fyi: str | None
    website_flagged_for_review: bool = False
    flags: list[str] = field(default_factory=list)


@dataclass
class BackfillReport:
    counts: BackfillCounts = field(default_factory=BackfillCounts)
    examples: list[BackfillExample] = field(default_factory=list)
    ambiguous_contact_ids: list[str] = field(default_factory=list)
    tier1_ambiguous_examples: list[dict] = field(default_factory=list)
    website_review_needed_contact_ids: list[str] = field(default_factory=list)
    # Backfill-only precaution -- see module docstring.
    excluded_contact_ids: list[str] = field(default_factory=list)
    # Requested exclusion IDs that had no registrations at all in this
    # run's dataset -- nothing to skip, but surfaced so a caller never
    # silently assumes an exclusion "took" when it was actually a no-op.
    excluded_contact_ids_not_found: list[str] = field(default_factory=list)
    dry_run: bool = True


def _is_placeholder_value(value: str | None) -> bool:
    return bool(value) and value.strip().casefold() in _PLACEHOLDER_VALUES


def _looks_malformed(value: str | None) -> bool:
    if not value:
        return False
    v = value.strip()
    if len(v) > 100:
        return True
    if "@" in v:
        return True
    if not re.search(r"[a-zA-Z]", v):
        return True
    return False


def _shares_no_token(a: str, b: str) -> bool:
    """Conservative whole-word overlap check on two already-normalized/
    lowercased strings -- used only to FLAG a possibly-wrong website match
    or a dramatic change for human review, never to reject/alter
    anything. Returns False (not flagged) when either side is blank --
    nothing to meaningfully compare."""
    tokens_a, tokens_b = set(a.split()), set(b.split())
    if not tokens_a or not tokens_b:
        return False
    return tokens_a.isdisjoint(tokens_b)


def _domain_stem(website: str | None) -> str:
    domain = normalize_website_domain(website) or ""
    return domain.split(".")[0] if domain else ""


async def run_luma_contact_enrichment_backfill(
    crm_contact_store: CrmContactStore,
    registration_store: LumaRegistrationStore,
    event_store: LumaEventStore,
    *,
    dry_run: bool = True,
    example_cap: int = 20,
    excluded_contact_ids: frozenset[str] | set[str] | None = None,
) -> BackfillReport:
    excluded = frozenset(excluded_contact_ids or ())
    all_contacts = await crm_contact_store.list()
    all_registrations = await registration_store.list()
    all_events = await event_store.list()

    contacts_by_id: dict[str, CrmContact] = {c.crm_contact_id: c for c in all_contacts}
    events_by_id: dict[str, LumaEvent] = {e.luma_event_id: e for e in all_events}

    counts = BackfillCounts()
    regs_by_contact: dict[str, list[LumaRegistration]] = defaultdict(list)

    for reg in all_registrations:
        counts.registrations_examined += 1
        if not reg.crm_contact_id:
            counts.unresolved_registrations_skipped += 1
            continue
        regs_by_contact[reg.crm_contact_id].append(reg)

        company, job_title = extract_company_question_answer(reg)
        if company is None and job_title is None:
            continue  # no self-reported answer on this registration at all -- not a recency-tier data point
        recency = compute_registration_recency(reg, events_by_id.get(reg.luma_event_id))
        if recency.tier == RecencyTier.REGISTERED_AT:
            counts.registrations_using_registered_at += 1
        elif recency.tier == RecencyTier.JOINED_AT:
            counts.registrations_using_joined_at += 1
        elif recency.tier == RecencyTier.EVENT_START_AT:
            counts.registrations_using_event_start_at += 1
        else:
            counts.registrations_with_unknown_recency += 1

    counts.unique_contacts_represented = len(regs_by_contact)

    report = BackfillReport(counts=counts, dry_run=dry_run)
    report.excluded_contact_ids_not_found = sorted(excluded - set(regs_by_contact.keys()))

    for crm_contact_id, regs in regs_by_contact.items():
        if crm_contact_id in excluded:
            # Skip ENTIRELY -- no resolution, no merge call, no
            # Contact/provenance/Activity Log change, not counted toward
            # any other bucket. See module docstring.
            counts.contacts_excluded += 1
            report.excluded_contact_ids.append(crm_contact_id)
            continue

        contact = contacts_by_id.get(crm_contact_id)
        if contact is None:
            continue  # dangling crm_contact_id reference -- data-integrity edge, not expected; skip defensively

        resolutions = resolve_contact_luma_fields(regs, events_by_id)
        if resolutions.company.candidate_count > 1:
            counts.contacts_multiple_valid_company_answers += 1
        if resolutions.title.candidate_count > 1:
            counts.contacts_multiple_valid_title_answers += 1

        # Report-only Tier 1/Tier 2 evaluation against the RESOLVED (post-
        # Luma) company for every examined contact, independent of
        # whether apply_luma_self_report() itself would ever consult
        # tier1 (it only does when company_website is currently blank) --
        # this is what lets the report show "Tier 2 candidates" broadly
        # for later human evaluation on every contact, not just those
        # eligible for an actual Tier 1 write.
        resolved_company_value = resolutions.company.resolved.value if resolutions.company.resolved else contact.company
        normalized_company = normalize_company_name(resolved_company_value)
        tier1 = (
            resolve_tier1_website(normalized_company, all_contacts, exclude_crm_contact_id=crm_contact_id)
            if normalized_company
            else None
        )
        tier2 = resolve_tier2_email_domain_candidate(contact.email)

        outcome = apply_luma_self_report(contact, resolutions, tier1_result=tier1)

        if outcome.ambiguous_fields:
            counts.contacts_ambiguous_unknown_recency += 1
            report.ambiguous_contact_ids.append(crm_contact_id)

        company_updated = "company" in outcome.changed_field_keys
        title_updated = "title" in outcome.changed_field_keys
        # "unchanged" means EXACTLY "zero save would occur" -- gated on
        # changed_field_keys as a whole, not just company/title, so a
        # (rare) website-only change (a blank company_website populated
        # from Tier 1 while Company/Title both happen to already match)
        # is correctly EXCLUDED from "unchanged" even though it doesn't
        # fit the company/title/both buckets either.
        if not outcome.changed_field_keys:
            counts.contacts_unchanged += 1
        elif company_updated and title_updated:
            counts.contacts_would_update_both += 1
        elif company_updated:
            counts.contacts_would_update_company += 1
        elif title_updated:
            counts.contacts_would_update_title += 1

        company_from_unknown = company_updated and resolutions.company.resolved.tier == RecencyTier.UNKNOWN
        title_from_unknown = title_updated and resolutions.title.resolved.tier == RecencyTier.UNKNOWN
        if company_from_unknown:
            counts.contacts_would_update_company_from_unknown_recency += 1
        if title_from_unknown:
            counts.contacts_would_update_title_from_unknown_recency += 1
        if company_from_unknown or title_from_unknown:
            counts.contacts_would_update_from_unknown_recency += 1

        website_populated = "company_website" in outcome.changed_field_keys and outcome.contact.company_website
        if outcome.website_review_needed:
            counts.existing_websites_flagged_for_review += 1
            report.website_review_needed_contact_ids.append(crm_contact_id)
        if website_populated:
            counts.websites_populated_from_blank_tier1 += 1
        if outcome.website_tier1_ambiguous:
            counts.websites_tier1_ambiguous += 1
            if len(report.tier1_ambiguous_examples) < example_cap:
                report.tier1_ambiguous_examples.append(
                    {"crm_contact_id": crm_contact_id, "normalized_company": normalized_company}
                )
        if tier2.candidate:
            counts.websites_tier2_candidates += 1
        if tier2.excluded_free_domain:
            counts.websites_tier2_free_excluded += 1
        if not outcome.contact.company_website:
            counts.websites_still_unresolved += 1

        if outcome.changed_field_keys and len(report.examples) < example_cap:
            new_company = outcome.contact.company if company_updated else None
            new_title = outcome.contact.title if title_updated else None
            new_website = outcome.contact.company_website if website_populated else None

            flags: list[str] = []
            if company_updated:
                if _is_placeholder_value(new_company):
                    flags.append("generic_or_placeholder_company")
                if _looks_malformed(new_company):
                    flags.append("malformed_company_value")
                if contact.company and _shares_no_token(normalize_company_name(contact.company), normalize_company_name(new_company)):
                    flags.append("dramatic_company_change")
            if title_updated:
                if _looks_malformed(new_title):
                    flags.append("malformed_title_value")
                if contact.title and _shares_no_token(contact.title.strip().lower(), new_title.strip().lower()):
                    flags.append("dramatically_different_title")
            if website_populated and normalized_company and _shares_no_token(normalized_company, _domain_stem(new_website)):
                flags.append("tier1_website_looks_questionable")
            if tier2.candidate and normalized_company and _shares_no_token(normalized_company, _domain_stem(tier2.candidate)):
                flags.append("tier2_domain_does_not_match_company")
            if company_from_unknown or title_from_unknown:
                flags.append("update_based_solely_on_unknown_recency")
            if outcome.website_review_needed:
                flags.append("website_review_needed")

            report.examples.append(
                BackfillExample(
                    crm_contact_id=crm_contact_id,
                    old_company=contact.company,
                    new_company=new_company,
                    company_recency_tier=resolutions.company.resolved.tier.value if resolutions.company.resolved else None,
                    company_recency_at=(
                        resolutions.company.resolved.recency_at.isoformat()
                        if resolutions.company.resolved and resolutions.company.resolved.recency_at
                        else None
                    ),
                    old_title=contact.title,
                    new_title=new_title,
                    title_recency_tier=resolutions.title.resolved.tier.value if resolutions.title.resolved else None,
                    title_recency_at=(
                        resolutions.title.resolved.recency_at.isoformat()
                        if resolutions.title.resolved and resolutions.title.resolved.recency_at
                        else None
                    ),
                    old_website=contact.company_website,
                    new_website=new_website,
                    tier2_candidate_fyi=tier2.candidate,
                    website_flagged_for_review=outcome.website_review_needed,
                    flags=flags,
                )
            )

        if not dry_run and outcome.changed_field_keys:
            await crm_contact_store.save(outcome.contact)
            counts.contacts_saved += 1

    report.excluded_contact_ids.sort()
    return report
