import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// Regression coverage for the native Mail campaign detail page's dashboard
// redesign (tabs: Dashboard | Leads | Steps | Schedule | Settings). These
// are source-level assertions (no DOM render harness exists in this
// project -- see package.json's test script), same pattern as
// astro-campaign-isolation.test.ts and campaign-manager-apollo-disabled.test.ts.

const PAGE_SOURCE = readFileSync(new URL("../app/manager/campaigns/mail/[id]/page.tsx", import.meta.url), "utf-8");
const DASHBOARD_TAB_SOURCE = readFileSync(new URL("../components/mail-campaign-dashboard-tab.tsx", import.meta.url), "utf-8");
const LEADS_TAB_SOURCE = readFileSync(new URL("../components/mail-campaign-leads-tab.tsx", import.meta.url), "utf-8");

test("the campaign detail page renders all five required tabs", () => {
  for (const tab of ["Dashboard", "Leads", "Steps", "Schedule", "Settings"]) {
    assert.match(PAGE_SOURCE, new RegExp(`>${tab}<`));
  }
});

test("the campaign detail page preserves every native handler (nothing dropped in the split)", () => {
  for (const handler of [
    "handleSaveDetails",
    "handleSaveSchedule",
    "handleSaveSettings",
    "handleAddStep",
    "handleDeleteStep",
    "handleMoveStep",
    "handleMarkReady",
    "handleUnlock",
    "handleArchive",
  ]) {
    assert.match(PAGE_SOURCE, new RegExp(`\\b${handler}\\b`));
  }
});

test("the campaign detail page still fetches enrollments, steps, review, and CRM lists", () => {
  assert.match(PAGE_SOURCE, /listMailEnrollments/);
  assert.match(PAGE_SOURCE, /listMailSequenceSteps/);
  assert.match(PAGE_SOURCE, /getMailCampaignReview/);
  assert.match(PAGE_SOURCE, /listCrmLists/);
});

test("the Dashboard tab never fabricates an engagement metric", () => {
  for (const forbidden of [
    /Open Rate/i,
    /Click Rate/i,
    /Reply Rate/i,
    /Unsubscribe Rate/i,
    /Bounce Rate/i,
    /Connection Rate/i,
    /email touches/i,
    /send activity/i,
  ]) {
    assert.doesNotMatch(DASHBOARD_TAB_SOURCE, forbidden);
  }
});

test("the Dashboard tab has no Future Emails section", () => {
  assert.doesNotMatch(DASHBOARD_TAB_SOURCE, /Future Emails/i);
  assert.doesNotMatch(DASHBOARD_TAB_SOURCE, /Future emails/i);
});

test("the Dashboard tab renders real Review/enrollment fields, not invented ones", () => {
  for (const realField of [
    "total_contacts",
    "contacts_missing_email",
    "contacts_eligible",
    "sequence_step_count",
    "theoretical_total_sends",
    "readiness_warnings",
  ]) {
    assert.match(DASHBOARD_TAB_SOURCE, new RegExp(realField));
  }
  // Pending/Suppressed progress counts must come from real enrollment rows,
  // not from a separately-invented "in progress"/"completed" status that
  // doesn't exist in MailEnrollmentStatus.
  assert.doesNotMatch(DASHBOARD_TAB_SOURCE, /in.progress/i);
  assert.doesNotMatch(DASHBOARD_TAB_SOURCE, /"completed"/i);
});

test("the Dashboard tab explains Draft campaigns have no enrollments yet, rather than showing a bare 0", () => {
  assert.match(DASHBOARD_TAB_SOURCE, /Not enrolled yet/);
});

test("the Leads tab gives Draft campaigns an accurate empty state instead of an empty table", () => {
  assert.match(LEADS_TAB_SOURCE, /status === "draft"/);
  assert.match(LEADS_TAB_SOURCE, /enrollments are created when this campaign is marked Ready/);
});

test("the Leads tab never invents a send/open/reply enrollment state", () => {
  for (const forbidden of [/\bsent\b/i, /\bopened\b/i, /\breplied\b/i, /\bclicked\b/i]) {
    assert.doesNotMatch(LEADS_TAB_SOURCE, forbidden);
  }
});

test("Apollo remains completely absent from the redesigned campaign detail page and its tabs", () => {
  for (const source of [PAGE_SOURCE, DASHBOARD_TAB_SOURCE, LEADS_TAB_SOURCE]) {
    assert.doesNotMatch(source, /Apollo/);
    assert.doesNotMatch(source, /\/sync\/campaigns/);
    assert.doesNotMatch(source, /campaign-builder/);
  }
});
