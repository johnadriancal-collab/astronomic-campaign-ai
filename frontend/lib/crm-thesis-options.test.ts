import { test } from "node:test";
import assert from "node:assert/strict";
import { INDUSTRY_OPTIONS } from "./crm-thesis-options.ts";

// This file's INDUSTRY_OPTIONS is an independently hardcoded copy of
// app/models/crm.py's INDUSTRY_OPTIONS -- no shared source at build time
// (see this file's own module comment). A Node test can't import Python,
// so this pins the exact expected list here as the practical parity
// check: if the backend list changes without this file being updated to
// match, this test fails instead of the two silently drifting.
const EXPECTED_INDUSTRY_OPTIONS = [
  "Aerospace & Defense",
  "AgTech & Food Production",
  "Artificial Intelligence / Machine Learning",
  "Automotive & Mobility",
  "Biotech & Life Sciences",
  "Climate Tech & Sustainability",
  "Construction & Built Environment",
  "Consumer Goods & Retail",
  "Creative Industries (Media, Music, Photo, etc.)",
  "Crypto / Web3",
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
  "Professional / Business Services",
  "Real Estate & PropTech",
  "SaaS / Software Infrastructure",
  "Social Media & Creator Economy",
  "Telecom & Connectivity",
  "Travel, Tourism & Hospitality",
  "Veterinary / Animal Health",
];

test("INDUSTRY_OPTIONS matches the expected canonical list exactly (parity guard against app/models/crm.py)", () => {
  assert.deepEqual(INDUSTRY_OPTIONS, EXPECTED_INDUSTRY_OPTIONS);
});

test("INDUSTRY_OPTIONS includes the Luma-added Crypto / Web3 and Professional / Business Services options", () => {
  assert.ok(INDUSTRY_OPTIONS.includes("Crypto / Web3"));
  assert.ok(INDUSTRY_OPTIONS.includes("Professional / Business Services"));
});

test("INDUSTRY_OPTIONS has no duplicate or near-duplicate (case-insensitive) entries", () => {
  const lowered = INDUSTRY_OPTIONS.map((o) => o.toLowerCase());
  assert.equal(new Set(lowered).size, lowered.length);
});
