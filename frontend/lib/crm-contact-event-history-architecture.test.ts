import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// Contacts CRM Stage 3A -- the Contact detail page's Event History card
// must be REWIRED to the canonical, EngagementParticipant-derived
// GET /crm/contacts/{id}/events endpoint, not a second card, and must
// stop calling the old LumaRegistration-derived endpoint (which stays in
// lib/api.ts for compatibility -- just unused by this page). Scanned
// directly from source, same "no component-render harness" convention as
// sidebar-nav-source.test.ts / client-touchpoint-architecture.test.ts.

const CONTACT_DETAIL_PAGE = readFileSync(new URL("../app/crm/[id]/page.tsx", import.meta.url), "utf-8");

test("the page calls getCrmContactEvents (the canonical Stage 3A endpoint)", () => {
  assert.match(CONTACT_DETAIL_PAGE, /getCrmContactEvents\(/);
});

test("the page no longer calls getCrmContactLumaRegistrations -- Event History stopped using it", () => {
  assert.doesNotMatch(CONTACT_DETAIL_PAGE, /getCrmContactLumaRegistrations/);
  assert.doesNotMatch(CONTACT_DETAIL_PAGE, /CrmContactLumaRegistration/);
});

test("getCrmContactLumaRegistrations itself is untouched and still exported (kept for compatibility)", () => {
  const API_TS = readFileSync(new URL("../lib/api.ts", import.meta.url), "utf-8");
  assert.match(API_TS, /export function getCrmContactLumaRegistrations\(/);
  assert.match(API_TS, /\/crm\/contacts\/\$\{crmContactId\}\/luma-registrations/);
});

test("exactly one Event History card exists on the page", () => {
  const matches = CONTACT_DETAIL_PAGE.match(/<CardTitle className="text-sm">Event History<\/CardTitle>/g) ?? [];
  assert.equal(matches.length, 1);
});

test("the card uses buildParticipantEventHistory, the new EngagementParticipant-derived helper", () => {
  assert.match(CONTACT_DETAIL_PAGE, /buildParticipantEventHistory\(/);
  assert.doesNotMatch(CONTACT_DETAIL_PAGE, /buildEventHistory\(/);
});

test("the page never re-sorts the event history it receives", () => {
  assert.doesNotMatch(CONTACT_DETAIL_PAGE, /eventHistory\.sort/);
  assert.doesNotMatch(CONTACT_DETAIL_PAGE, /\[\.\.\.eventHistory\]\.sort/);
  assert.doesNotMatch(CONTACT_DETAIL_PAGE, /\.sort\(/);
});

test("the card renders Role/RSVP/Attendance/Source labels, not raw enum strings", () => {
  const cardMatch = CONTACT_DETAIL_PAGE.match(/<CardTitle className="text-sm">Event History<\/CardTitle>[\s\S]*?<\/Card>/);
  assert.ok(cardMatch, "expected to find the Event History card");
  const card = cardMatch![0];
  assert.match(card, /entry\.roleLabel/);
  assert.match(card, /entry\.rsvpLabel/);
  assert.match(card, /entry\.attendanceLabel/);
  assert.match(card, /entry\.sourceLabel/);
});

test("existing loading/empty states remain present", () => {
  const cardMatch = CONTACT_DETAIL_PAGE.match(/<CardTitle className="text-sm">Event History<\/CardTitle>[\s\S]*?<\/Card>/);
  assert.ok(cardMatch, "expected to find the Event History card");
  assert.match(cardMatch![0], /Loading…/);
  assert.match(cardMatch![0], /No event history yet\./);
});

test("a fetch failure falls back to an empty list, never leaves the section stuck loading forever", () => {
  assert.match(CONTACT_DETAIL_PAGE, /getCrmContactEvents\(contact\.crm_contact_id\)\s*\n\s*\.then\(setContactEvents\)\s*\n\s*\.catch\(\(\) => setContactEvents\(\[\]\)\)/);
});
