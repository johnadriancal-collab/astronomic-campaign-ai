import assert from "node:assert/strict";
import { test } from "node:test";
import {
  buildContactSummary,
  formatCheckSizeBuckets,
  formatIndustriesForHighlight,
  formatIndustriesForProse,
} from "./contact-summary.ts";
import type { CrmContact } from "./api.ts";

function makeContact(overrides: Partial<CrmContact> = {}): CrmContact {
  return {
    crm_contact_id: "c-1",
    created_at: "2026-08-01T00:00:00Z",
    updated_at: "2026-08-01T00:00:00Z",
    archived: false,
    apollo_contact_id: null,
    first_name: "Alex",
    last_name: "Rivera",
    email: "alex@example.com",
    email_status: null,
    phone: null,
    linkedin_url: null,
    title: null,
    company: null,
    company_website: null,
    city: null,
    state: null,
    country: null,
    industry: null,
    company_size: null,
    revenue: null,
    funding_stage: null,
    funding_amount: null,
    technologies: [],
    seniority: null,
    department: null,
    job_function: null,
    source_snapshot: {},
    thesis_cities: null,
    thesis_investor_mode: null,
    thesis_investor_mode_manual_override: false,
    thesis_private_asset_types: [],
    thesis_private_asset_types_other: null,
    thesis_private_business_models: [],
    thesis_private_business_models_other: null,
    thesis_private_industries: [],
    thesis_private_industries_other: null,
    thesis_private_check_sizes: [],
    thesis_private_check_sizes_other: null,
    thesis_private_deal_stages: [],
    thesis_private_deal_stages_other: null,
    thesis_private_meeting_preferences: [],
    thesis_private_meeting_preferences_other: null,
    thesis_private_demographic_preferences: [],
    thesis_private_demographic_preferences_other: null,
    thesis_private_other_criteria: null,
    thesis_also_invests_institutionally: null,
    thesis_institutional_asset_types: [],
    thesis_institutional_asset_types_other: null,
    thesis_institutional_business_models: [],
    thesis_institutional_business_models_other: null,
    thesis_institutional_industries: [],
    thesis_institutional_industries_other: null,
    thesis_institutional_check_sizes: [],
    thesis_institutional_check_sizes_other: null,
    thesis_institutional_deal_stages: [],
    thesis_institutional_deal_stages_other: null,
    thesis_institutional_meeting_preferences: [],
    thesis_institutional_meeting_preferences_other: null,
    thesis_institutional_demographic_preferences: [],
    thesis_institutional_demographic_preferences_other: null,
    thesis_institutional_other_criteria: null,
    thesis_dietary_preferences: [],
    thesis_dietary_preferences_other: null,
    thesis_referral_emails: null,
    custom_fields: {},
    ...overrides,
  };
}

// --- formatCheckSizeBuckets ---------------------------------------------

test("a single bucket is displayed as-is", () => {
  assert.equal(formatCheckSizeBuckets(["$100k - $250k"]), "$100k - $250k");
});

test("contiguous buckets collapse into a truthful range (check-size formatting)", () => {
  assert.equal(formatCheckSizeBuckets(["$1k - $10k", "$10k - $25k", "$25k - $50k", "$50k - $100k"]), "$1k–$100k");
});

test("a contiguous range ending in the open-ended top bucket keeps the '+' ", () => {
  assert.equal(
    formatCheckSizeBuckets(["$500k - $1M", "$1M - $2M", "$2M - $5M", "$5M - $10M", "$10M+"]),
    "$500k–$10M+"
  );
});

test("non-contiguous buckets are listed individually, never a false range", () => {
  assert.equal(formatCheckSizeBuckets(["$1k - $10k", "$100k - $250k"]), "$1k - $10k and $100k - $250k");
});

test("an 'Other:' value alongside known buckets is listed individually, not merged", () => {
  const result = formatCheckSizeBuckets(["$1k - $10k", "$10k - $25k", "Other:"]);
  assert.equal(result, "$1k - $10k, $10k - $25k, and Other:");
});

// --- buildContactSummary -------------------------------------------------

test("rich investor summary matches the desired experience", () => {
  const contact = makeContact({
    city: "Austin",
    custom_fields: {
      role: ["Investor", "Founder"],
      investor_type: ["Angel Investor"],
      check_size_personal: ["$100k - $250k"],
      investment_industry: ["AI", "SaaS", "Healthcare"],
      deploying_capital: "Yes, actively",
    },
  });

  const summary = buildContactSummary(contact);
  assert.equal(
    summary.sentence,
    "Angel investor and founder based in Austin. Typically writes $100k - $250k personal checks and focuses on AI, SaaS, and Healthcare. Currently actively deploying capital."
  );
  assert.deepEqual(summary.highlights, [
    { label: "Role", value: "Investor, Founder" },
    { label: "Investor Type", value: "Angel Investor" },
    { label: "Check Size", value: "$100k - $250k" },
    { label: "Deploying Capital", value: "Yes, actively" },
    { label: "Investment Focus", value: "AI, SaaS, Healthcare" },
    { label: "Location", value: "Austin" },
  ]);
});

test("a sparse contact with no custom fields produces an empty sentence and no highlights", () => {
  const contact = makeContact();
  const summary = buildContactSummary(contact);
  assert.equal(summary.sentence, "");
  assert.deepEqual(summary.highlights, []);
});

test("multiple materially different investor types use generic 'Investor' wording, never picking one", () => {
  const contact = makeContact({
    custom_fields: { role: ["Investor"], investor_type: ["Angel Investor", "Venture Capital"] },
  });
  const summary = buildContactSummary(contact);
  assert.equal(summary.sentence, "Investor.");
  // The exact types still surface in the highlights, never lost.
  assert.deepEqual(summary.highlights.find((h) => h.label === "Investor Type"), {
    label: "Investor Type",
    value: "Angel Investor, Venture Capital",
  });
});

test("a Family Office investor type gets its own wording, not 'Angel investor'", () => {
  const contact = makeContact({ custom_fields: { role: ["Investor"], investor_type: ["Family Office"] } });
  assert.equal(buildContactSummary(contact).sentence, "Family office investor.");
});

test("missing check size omits the clause entirely, never says 'unknown'", () => {
  const contact = makeContact({
    city: "Denver",
    custom_fields: { role: ["Investor"], investor_type: ["Angel Investor"] },
  });
  const summary = buildContactSummary(contact);
  assert.equal(summary.sentence, "Angel investor based in Denver.");
  assert.ok(!summary.sentence.toLowerCase().includes("unknown"));
  assert.ok(!summary.highlights.some((h) => h.label.startsWith("Check Size")));
});

test("deploying capital: actively", () => {
  const contact = makeContact({ custom_fields: { deploying_capital: "Yes, actively" } });
  assert.equal(buildContactSummary(contact).sentence, "Currently actively deploying capital.");
});

test("deploying capital: selectively", () => {
  const contact = makeContact({ custom_fields: { deploying_capital: "Selectively" } });
  assert.equal(buildContactSummary(contact).sentence, "Currently deploying capital selectively.");
});

test("deploying capital: not at the moment", () => {
  const contact = makeContact({ custom_fields: { deploying_capital: "Not at the moment" } });
  assert.equal(buildContactSummary(contact).sentence, "Not currently deploying capital.");
});

test("missing location omits the location clause and highlight, never says 'unknown'", () => {
  const contact = makeContact({ custom_fields: { role: ["Founder"] } });
  const summary = buildContactSummary(contact);
  assert.equal(summary.sentence, "Founder.");
  assert.ok(!summary.highlights.some((h) => h.label === "Location"));
});

test("Founder-only role never claims investor status", () => {
  const contact = makeContact({ custom_fields: { role: ["Founder"] } });
  assert.equal(buildContactSummary(contact).sentence, "Founder.");
});

test("job title/company alone never produce an investor or founder claim", () => {
  const contact = makeContact({ title: "Managing Partner", company: "Some Fund", custom_fields: {} });
  const summary = buildContactSummary(contact);
  assert.equal(summary.sentence, "");
});

test("both personal and institutional check sizes get distinctly labeled highlights", () => {
  const contact = makeContact({
    custom_fields: { check_size_personal: ["$1k - $10k"], check_size_institutional: ["$1M - $2M"] },
  });
  const summary = buildContactSummary(contact);
  assert.deepEqual(
    summary.highlights.filter((h) => h.label.startsWith("Check Size")),
    [
      { label: "Check Size (Personal)", value: "$1k - $10k" },
      { label: "Check Size (Institutional)", value: "$1M - $2M" },
    ]
  );
});

test("city and state together produce a combined location clause", () => {
  const contact = makeContact({ city: "Austin", state: "Texas", custom_fields: { role: ["Founder"] } });
  assert.equal(buildContactSummary(contact).sentence, "Founder based in Austin, Texas.");
});

// --- Investment Focus truncation (presentation-only; CRM data untouched) ---

test("formatIndustriesForProse shows all industries when there are 3 or fewer", () => {
  assert.equal(formatIndustriesForProse(["AI"]), "AI");
  assert.equal(formatIndustriesForProse(["AI", "SaaS"]), "AI and SaaS");
  assert.equal(formatIndustriesForProse(["AI", "SaaS", "FinTech"]), "AI, SaaS, and FinTech");
});

test("formatIndustriesForProse caps at the first 3 plus a count, in stored order", () => {
  const industries = ["AI", "SaaS", "FinTech", "Healthcare", "Retail", "Education"];
  assert.equal(formatIndustriesForProse(industries), "AI, SaaS, FinTech, and 3 more");
});

test("formatIndustriesForProse with 21 industries matches the requested example shape", () => {
  const industries = Array.from({ length: 21 }, (_, i) => `Industry ${i + 1}`);
  const result = formatIndustriesForProse(industries);
  assert.equal(result, "Industry 1, Industry 2, Industry 3, and 18 more");
});

test("formatIndustriesForHighlight shows all industries when there are 4 or fewer", () => {
  assert.equal(formatIndustriesForHighlight(["AI", "SaaS", "FinTech", "Healthcare"]), "AI, SaaS, FinTech, Healthcare");
});

test("formatIndustriesForHighlight caps at the first 4 plus a compact '+N more'", () => {
  const industries = ["AI", "SaaS", "FinTech", "Healthcare", "Retail", "Education"];
  assert.equal(formatIndustriesForHighlight(industries), "AI, SaaS, FinTech, Healthcare, +2 more");
});

test("industry truncation never reorders -- always the first N in stored order", () => {
  const industries = ["Zoology", "Aerospace", "Biotech", "Consulting", "Defense"];
  assert.equal(formatIndustriesForProse(industries), "Zoology, Aerospace, Biotech, and 2 more");
  assert.equal(formatIndustriesForHighlight(industries), "Zoology, Aerospace, Biotech, Consulting, +1 more");
});

test("a long industry list is truncated in both the sentence and the highlight, but the full list is never lost from custom_fields", () => {
  const industries = ["AI", "SaaS", "FinTech", "Healthcare", "Retail", "Education", "Real Estate"];
  const contact = makeContact({ custom_fields: { investment_industry: industries } });
  const summary = buildContactSummary(contact);

  assert.ok(summary.sentence.includes("AI, SaaS, FinTech, and 4 more"));
  const highlight = summary.highlights.find((h) => h.label === "Investment Focus");
  assert.equal(highlight?.value, "AI, SaaS, FinTech, Healthcare, +3 more");
  // Display-only: the caller's own contact object is never mutated.
  assert.deepEqual(contact.custom_fields.investment_industry, industries);
});
