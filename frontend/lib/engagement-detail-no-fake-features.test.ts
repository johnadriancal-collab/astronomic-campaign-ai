import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// Client CRM Stage 1E's Engagement detail page must not fabricate data for
// features that don't exist yet -- scanned directly from source, same
// "no component-render harness" convention as client-detail-no-fake-
// features.test.ts. Stage 1F adds a REAL Closeout section (the
// EngagementCloseout feature actually exists now), so "no fake Closeout"
// is no longer part of this guard -- see the Closeout-specific tests
// below instead. Stage 1G adds a REAL Participants section (the
// EngagementParticipant feature actually exists now) -- see the
// Participants-specific tests below instead. Day 3/30/90 follow-ups and
// Notes/Activity remain unbuilt and stay forbidden.

const ENGAGEMENT_DETAIL_PAGE = readFileSync(
  new URL("../app/clients/[id]/engagements/[engagementId]/page.tsx", import.meta.url),
  "utf-8"
);

test("no hardcoded/fake tab navigation or sections for unbuilt features exists yet", () => {
  for (const forbidden of [/>Guests</, />Notes</, />Activity</, /Day 3/, /Day 30/, /Day 90/, /TabsTab/]) {
    assert.doesNotMatch(ENGAGEMENT_DETAIL_PAGE, forbidden);
  }
});

test("no client-satisfaction language or fabricated bulk guest-list import UI", () => {
  for (const forbidden of [/client satisfaction/i, /import.{0,20}guest/i, /csv/i]) {
    assert.doesNotMatch(ENGAGEMENT_DETAIL_PAGE, forbidden);
  }
});

// --- Stage 1F: real Closeout section ---------------------------------------

test("a real Closeout section is present, using the shared aggregate-count/free-text fields, not a fake placeholder", () => {
  assert.match(ENGAGEMENT_DETAIL_PAGE, /Closeout/);
  assert.match(ENGAGEMENT_DETAIL_PAGE, /No closeout recorded yet/);
  assert.match(ENGAGEMENT_DETAIL_PAGE, /Add Closeout/);
  assert.match(ENGAGEMENT_DETAIL_PAGE, /Edit Closeout/);
});

test("Closeout Archive/Restore is present, distinct from any delete action", () => {
  assert.match(ENGAGEMENT_DETAIL_PAGE, /Archive Closeout/);
  assert.match(ENGAGEMENT_DETAIL_PAGE, /Restore Closeout/);
});

test("no hard-delete control exists for a Closeout either", () => {
  assert.doesNotMatch(ENGAGEMENT_DETAIL_PAGE, /Delete Closeout/);
  assert.doesNotMatch(ENGAGEMENT_DETAIL_PAGE, /deleteEngagementCloseout/);
});

test("Closeout turnout fields use the exact labels this stage specified", () => {
  for (const label of [/Confirmed/, /Attended/, /No-Shows/, /Cancelled/, /Unexpected\/Walk-ins/, /Attendance rate/]) {
    assert.match(ENGAGEMENT_DETAIL_PAGE, label);
  }
});

test("no hard-delete control exists on the Engagement detail page", () => {
  assert.doesNotMatch(ENGAGEMENT_DETAIL_PAGE, /Delete Engagement/);
  assert.doesNotMatch(ENGAGEMENT_DETAIL_PAGE, /deleteClientEngagement/);
});

test("Archive/Restore is present, distinct from any delete action", () => {
  assert.match(ENGAGEMENT_DETAIL_PAGE, /Archive Engagement/);
  assert.match(ENGAGEMENT_DETAIL_PAGE, /Restore Engagement/);
});

test("no Luma picker/sync UI exists on the Engagement form -- luma_event_id is not part of the user-facing form", () => {
  const modal = readFileSync(new URL("../components/engagement-form-modal.tsx", import.meta.url), "utf-8");
  assert.doesNotMatch(modal, /luma_event_id/);
  assert.doesNotMatch(modal, /Luma/);
});

// --- Stage 1H-A: real Linked Luma Event section (link only) ----------------
// The picker deliberately lives on the DETAIL page (this section), not the
// Add/Engagement form above -- the test right above this one keeps proving
// that boundary. Link-only: no participant sync/backfill control exists
// anywhere on this page yet (that's a future, separate stage).

test("a real Linked Luma Event section is present, using the real picker component", () => {
  assert.match(ENGAGEMENT_DETAIL_PAGE, /Linked Luma Event/);
  assert.match(ENGAGEMENT_DETAIL_PAGE, /LumaEventPicker/);
});

test("the linked state shows the event's name/date and an explicit Unlink action, not a raw id field", () => {
  assert.match(ENGAGEMENT_DETAIL_PAGE, /lumaEvent\.name/);
  assert.match(ENGAGEMENT_DETAIL_PAGE, /formatEngagementDate\(lumaEvent\.start_at\)/);
  assert.match(ENGAGEMENT_DETAIL_PAGE, /Unlink/);
  assert.match(ENGAGEMENT_DETAIL_PAGE, /handleUnlinkLumaEvent/);
  assert.doesNotMatch(ENGAGEMENT_DETAIL_PAGE, /<Input[^>]*luma_event_id/);
});

test("a stored Luma event URL renders as an Open-in-Luma link", () => {
  assert.match(ENGAGEMENT_DETAIL_PAGE, /Open in Luma/);
  assert.match(ENGAGEMENT_DETAIL_PAGE, /lumaEvent\.url/);
});

test("no hard-delete control or participant-sync control exists for the Luma link", () => {
  assert.doesNotMatch(ENGAGEMENT_DETAIL_PAGE, /Delete.{0,10}Luma/i);
  assert.doesNotMatch(ENGAGEMENT_DETAIL_PAGE, /Sync Participants/i);
  assert.doesNotMatch(ENGAGEMENT_DETAIL_PAGE, /Backfill/i);
});

test("the picker component itself is never imported into the Engagement or Participant forms", () => {
  const engagementModal = readFileSync(new URL("../components/engagement-form-modal.tsx", import.meta.url), "utf-8");
  const participantModal = readFileSync(new URL("../components/engagement-participant-form-modal.tsx", import.meta.url), "utf-8");
  assert.doesNotMatch(engagementModal, /LumaEventPicker/);
  assert.doesNotMatch(participantModal, /LumaEventPicker/);
});

test("retired Supernova/Galaxy/Aurora program terminology does not appear anywhere in Client CRM's frontend source (Stage 1E.1)", () => {
  const files = [
    "../lib/client-crm.ts",
    "../lib/api.ts",
    "../components/engagement-form-modal.tsx",
    "../components/engagement-participant-form-modal.tsx",
    "../app/clients/[id]/page.tsx",
    "../app/clients/[id]/engagements/[engagementId]/page.tsx",
  ];
  for (const relativePath of files) {
    const source = readFileSync(new URL(relativePath, import.meta.url), "utf-8");
    for (const forbidden of [/supernova/i, /galaxy/i, /aurora/i]) {
      assert.doesNotMatch(source, forbidden, `${relativePath} must not reference retired dinner-program terminology`);
    }
  }
});

// --- Stage 1G: real Participants section -----------------------------------

test("a real Participants section is present, using the shared EngagementParticipant fields, not a fake placeholder", () => {
  assert.match(ENGAGEMENT_DETAIL_PAGE, /Participants/);
  assert.match(ENGAGEMENT_DETAIL_PAGE, /No Participants recorded yet/);
  assert.match(ENGAGEMENT_DETAIL_PAGE, /Add Participant/);
});

test("Participants table uses the exact columns this stage specified", () => {
  for (const label of [/>Name</, />Title</, />Company</, />Role</, />RSVP</, />Attendance</]) {
    assert.match(ENGAGEMENT_DETAIL_PAGE, label);
  }
});

test("Participants table renders the resolved (canonical-Contact-preferring) display fields, not the raw snapshot fields", () => {
  assert.match(ENGAGEMENT_DETAIL_PAGE, /participant\.resolved_name/);
  assert.match(ENGAGEMENT_DETAIL_PAGE, /participant\.resolved_title/);
  assert.match(ENGAGEMENT_DETAIL_PAGE, /participant\.resolved_company/);
});

test("a Walk-in indicator is present, distinct from attendance status", () => {
  assert.match(ENGAGEMENT_DETAIL_PAGE, /Walk-in/);
});

test("Participant Archive/Restore is present, distinct from any delete action", () => {
  assert.match(ENGAGEMENT_DETAIL_PAGE, /handleArchiveParticipantToggle/);
  assert.doesNotMatch(ENGAGEMENT_DETAIL_PAGE, /Delete Participant/);
  assert.doesNotMatch(ENGAGEMENT_DETAIL_PAGE, /deleteEngagementParticipant/);
});

test("a linked participant's name navigates to its canonical Contact", () => {
  assert.match(ENGAGEMENT_DETAIL_PAGE, /\/crm\/\$\{participant\.crm_contact_id\}/);
});

test("no Luma picker/sync UI exists on the Participant form -- Stage 1G creates only MANUAL records", () => {
  const modal = readFileSync(new URL("../components/engagement-participant-form-modal.tsx", import.meta.url), "utf-8");
  assert.doesNotMatch(modal, /Luma/);
  assert.doesNotMatch(modal, /luma_guest_id/);
});

test("the Participant form strongly favors selecting an existing Contact -- free-text entry is a secondary link, not a button", () => {
  const modal = readFileSync(new URL("../components/engagement-participant-form-modal.tsx", import.meta.url), "utf-8");
  assert.match(modal, /Add an unresolved participant instead/);
  assert.match(modal, /CrmContactPicker/);
});
