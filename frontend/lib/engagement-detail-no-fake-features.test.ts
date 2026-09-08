import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// Client CRM Stage 1E's Engagement detail page must not fabricate data for
// features that don't exist yet -- scanned directly from source, same
// "no component-render harness" convention as client-detail-no-fake-
// features.test.ts. Stage 1F adds a REAL Closeout section (the
// EngagementCloseout feature actually exists now), so "no fake Closeout"
// is no longer part of this guard -- see the Closeout-specific tests
// below instead. Per-person attendance (Stage 1G), Day 3/30/90
// follow-ups, and Notes/Activity remain unbuilt and stay forbidden.

const ENGAGEMENT_DETAIL_PAGE = readFileSync(
  new URL("../app/clients/[id]/engagements/[engagementId]/page.tsx", import.meta.url),
  "utf-8"
);

test("no hardcoded/fake tab navigation or sections for unbuilt features exists yet", () => {
  for (const forbidden of [/>Guests</, />Notes</, />Activity</, /Day 3/, /Day 30/, /Day 90/, /TabsTab/]) {
    assert.doesNotMatch(ENGAGEMENT_DETAIL_PAGE, forbidden);
  }
});

test("no fabricated per-person attendance (Stage 1G, not built yet) or client-satisfaction language", () => {
  for (const forbidden of [/EngagementParticipant/, /client satisfaction/i, /walk-in guest list/i]) {
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

test("retired Supernova/Galaxy/Aurora program terminology does not appear anywhere in Client CRM's frontend source (Stage 1E.1)", () => {
  const files = [
    "../lib/client-crm.ts",
    "../lib/api.ts",
    "../components/engagement-form-modal.tsx",
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
