// Deterministic (no LLM, no network call) contact summary for the CRM
// contact detail page's Overview section -- kept separate from the page
// component so it's unit-testable without rendering React, same split as
// lib/sort-contacts.ts and lib/csv-export.ts.
//
// Every clause below is generated ONLY from fields already loaded on the
// contact (core fields + custom_fields) -- never inferred from title/job
// function, never guessed, and always omitted (not shown as "Unknown")
// when the underlying data is missing.

import type { CrmContact } from "@/lib/api";

// The exact, ordered custom:check_size_personal / check_size_institutional
// taxonomy (see the CRM custom field registry) -- ordering matters here
// because it's what "contiguous buckets" is checked against. "Other:" is
// deliberately excluded from ordering/range logic below: it's a wildcard,
// never merged into a range.
const CHECK_SIZE_BUCKET_ORDER = [
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
];

// Investor Type values that map cleanly to a short, natural noun phrase.
// Anything not in this map (or when more than one type is present) falls
// back to the generic "Investor" -- the exact type(s) are never lost,
// they just move to the highlights instead of being guessed at in prose.
const INVESTOR_TYPE_WORDING: Record<string, string> = {
  "Angel Investor": "Angel investor",
  "Family Office": "Family office investor",
  "Venture Capital": "Venture capital investor",
  "Private Equity": "Private equity investor",
  "Institutional Investor": "Institutional investor",
  "Private Investor": "Private investor",
};

// Already the exact, final sentence text (capitalized, no trailing
// period) -- used as-is, never run through capitalizeFirst.
const DEPLOYING_CAPITAL_CLAUSE: Record<string, string> = {
  "Yes, actively": "Currently actively deploying capital",
  Selectively: "Currently deploying capital selectively",
  "Not at the moment": "Not currently deploying capital",
};

// Prose is capped at 3 industries -- a contact with many selected
// industries would otherwise turn the Overview sentence into an unreadable
// run-on list. Order is never resorted: this always takes the FIRST N as
// stored on the contact, so it's deterministic and reflects the CRM's own
// order, never alphabetized or "most relevant first" (a judgment this
// function has no basis to make). The underlying custom_fields.investment_industry
// value itself is never touched -- this only affects what's displayed here.
const PROSE_INDUSTRY_LIMIT = 3;
// Highlights show slightly more since there's no sentence to keep readable,
// just a compact chip.
const HIGHLIGHT_INDUSTRY_LIMIT = 4;

export function formatIndustriesForProse(industries: string[]): string {
  if (industries.length <= PROSE_INDUSTRY_LIMIT) return joinList(industries);
  const shown = industries.slice(0, PROSE_INDUSTRY_LIMIT);
  const remaining = industries.length - PROSE_INDUSTRY_LIMIT;
  return `${shown.join(", ")}, and ${remaining} more`;
}

export function formatIndustriesForHighlight(industries: string[]): string {
  if (industries.length <= HIGHLIGHT_INDUSTRY_LIMIT) return industries.join(", ");
  const shown = industries.slice(0, HIGHLIGHT_INDUSTRY_LIMIT);
  const remaining = industries.length - HIGHLIGHT_INDUSTRY_LIMIT;
  return `${shown.join(", ")}, +${remaining} more`;
}

export interface ContactSummaryHighlight {
  label: string;
  value: string;
}

export interface ContactSummary {
  sentence: string; // "" when there's nothing to say
  highlights: ContactSummaryHighlight[];
}

function asStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string" && v.length > 0) : [];
}

function asString(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function joinList(items: string[]): string {
  if (items.length === 0) return "";
  if (items.length === 1) return items[0];
  if (items.length === 2) return `${items[0]} and ${items[1]}`;
  return `${items.slice(0, -1).join(", ")}, and ${items[items.length - 1]}`;
}

function capitalizeFirst(text: string): string {
  return text.length > 0 ? text[0].toUpperCase() + text.slice(1) : text;
}

// "$100k - $250k" -> ["$100k", "$250k"]; "$10M+" -> ["$10M", null] (open-ended).
function bucketBounds(bucket: string): [string, string | null] {
  if (bucket.endsWith("+")) return [bucket.slice(0, -1), null];
  const [low, high] = bucket.split(" - ");
  return [low, high ?? null];
}

// Formats a contact's check-size buckets conservatively: a contiguous run
// (per CHECK_SIZE_BUCKET_ORDER) collapses to one truthful range; anything
// else (a gap, or the "Other:" wildcard present) is listed individually --
// never smoothed into a range that would overstate what's actually known.
export function formatCheckSizeBuckets(buckets: string[]): string {
  const known = buckets.filter((b) => CHECK_SIZE_BUCKET_ORDER.includes(b));
  const other = buckets.filter((b) => !CHECK_SIZE_BUCKET_ORDER.includes(b));

  if (known.length === 0) {
    return joinList(other);
  }

  const indices = known.map((b) => CHECK_SIZE_BUCKET_ORDER.indexOf(b)).sort((a, b) => a - b);
  const isContiguous = indices.every((idx, i) => i === 0 || idx === indices[i - 1] + 1);

  if (isContiguous && other.length === 0) {
    if (indices.length === 1) return known[0];
    const [low] = bucketBounds(CHECK_SIZE_BUCKET_ORDER[indices[0]]);
    const lastBucket = CHECK_SIZE_BUCKET_ORDER[indices[indices.length - 1]];
    const [, high] = bucketBounds(lastBucket);
    return `${low}–${high ?? lastBucket}`;
  }

  // Non-contiguous, or a mix of known + "Other:" -- display every value
  // as-is rather than inventing a range that would misrepresent a gap.
  const orderedKnown = indices.map((i) => CHECK_SIZE_BUCKET_ORDER[i]);
  return joinList([...orderedKnown, ...other]);
}

function investorArchetypeClause(role: string[], investorTypes: string[]): string {
  const isInvestor = role.includes("Investor") || investorTypes.length > 0;
  const isFounder = role.includes("Founder");

  let archetype = "";
  if (isInvestor) {
    archetype = investorTypes.length === 1 ? INVESTOR_TYPE_WORDING[investorTypes[0]] || "Investor" : "Investor";
  }

  if (isFounder) {
    archetype = archetype ? `${archetype} and founder` : "Founder";
  }

  return archetype;
}

function locationClause(city: string | null, state: string | null): string {
  if (city && state) return `based in ${city}, ${state}`;
  if (city) return `based in ${city}`;
  if (state) return `based in ${state}`;
  return "";
}

export function buildContactSummary(contact: CrmContact): ContactSummary {
  const role = asStringArray(contact.custom_fields.role);
  const investorTypes = asStringArray(contact.custom_fields.investor_type);
  const checkSizePersonal = asStringArray(contact.custom_fields.check_size_personal);
  const checkSizeInstitutional = asStringArray(contact.custom_fields.check_size_institutional);
  const investmentIndustry = asStringArray(contact.custom_fields.investment_industry);
  const deployingCapital = asString(contact.custom_fields.deploying_capital);

  const archetype = investorArchetypeClause(role, investorTypes);
  const location = locationClause(contact.city, contact.state);

  const sentences: string[] = [];

  let opening = archetype;
  if (location) opening = opening ? `${opening} ${location}` : capitalizeFirst(location);
  if (opening) sentences.push(`${opening}.`);

  const secondClauseParts: string[] = [];
  if (checkSizePersonal.length > 0) {
    secondClauseParts.push(`Typically writes ${formatCheckSizeBuckets(checkSizePersonal)} personal checks`);
  }
  if (investmentIndustry.length > 0) {
    const focus = `focuses on ${formatIndustriesForProse(investmentIndustry)}`;
    secondClauseParts.push(secondClauseParts.length > 0 ? focus : capitalizeFirst(focus));
  }
  if (secondClauseParts.length > 0) {
    sentences.push(`${secondClauseParts.join(" and ")}.`);
  }

  if (deployingCapital && DEPLOYING_CAPITAL_CLAUSE[deployingCapital]) {
    sentences.push(`${DEPLOYING_CAPITAL_CLAUSE[deployingCapital]}.`);
  }

  const highlights: ContactSummaryHighlight[] = [];
  if (role.length > 0) highlights.push({ label: "Role", value: role.join(", ") });
  if (investorTypes.length > 0) highlights.push({ label: "Investor Type", value: investorTypes.join(", ") });
  if (checkSizePersonal.length > 0) {
    highlights.push({
      label: checkSizeInstitutional.length > 0 ? "Check Size (Personal)" : "Check Size",
      value: formatCheckSizeBuckets(checkSizePersonal),
    });
  }
  if (checkSizeInstitutional.length > 0) {
    highlights.push({
      label: checkSizePersonal.length > 0 ? "Check Size (Institutional)" : "Check Size",
      value: formatCheckSizeBuckets(checkSizeInstitutional),
    });
  }
  if (deployingCapital) highlights.push({ label: "Deploying Capital", value: deployingCapital });
  if (investmentIndustry.length > 0) {
    highlights.push({ label: "Investment Focus", value: formatIndustriesForHighlight(investmentIndustry) });
  }
  const location2 = contact.city && contact.state ? `${contact.city}, ${contact.state}` : contact.city || contact.state;
  if (location2) highlights.push({ label: "Location", value: location2 });

  return { sentence: sentences.join(" "), highlights };
}
