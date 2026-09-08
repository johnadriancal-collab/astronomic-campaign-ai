import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// Client CRM Stage 1E's Engagement detail page must not fabricate data for
// features that don't exist yet (Closeout, Day 3/30/90 follow-ups,
// Guests, Notes/Activity) -- scanned directly from source, same
// "no component-render harness" convention as client-detail-no-fake-
// features.test.ts.

const ENGAGEMENT_DETAIL_PAGE = readFileSync(
  new URL("../app/clients/[id]/engagements/[engagementId]/page.tsx", import.meta.url),
  "utf-8"
);

test("no hardcoded/fake tab navigation or sections for unbuilt features exists yet", () => {
  for (const forbidden of [/>Closeout</, />Guests</, />Notes</, />Activity</, /Day 3/, /Day 30/, /Day 90/, /TabsTab/]) {
    assert.doesNotMatch(ENGAGEMENT_DETAIL_PAGE, forbidden);
  }
});

test("no fabricated turnout/attendance/guest-quality/satisfaction data anywhere", () => {
  for (const forbidden of [
    /turnout/i,
    /attendance rate/i,
    /guest quality/i,
    /dinner dynamics/i,
    /client satisfaction/i,
    /no-show/i,
  ]) {
    assert.doesNotMatch(ENGAGEMENT_DETAIL_PAGE, forbidden);
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
