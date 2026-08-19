import assert from "node:assert/strict";
import { test } from "node:test";
import { MANAGER_NAV_SECTIONS } from "./manager-nav.ts";

test("Emails nav item points to /manager/emails with the label 'Emails'", () => {
  const emails = MANAGER_NAV_SECTIONS.find((s) => s.href === "/manager/emails");
  assert.ok(emails, "expected a /manager/emails section to exist");
  assert.equal(emails.label, "Emails");
});

test("the old 'Sequences / Emails' label and /manager/sequences href are gone", () => {
  const labels = MANAGER_NAV_SECTIONS.map((s) => s.label);
  const hrefs = MANAGER_NAV_SECTIONS.map((s) => s.href);
  assert.ok(!labels.includes("Sequences / Emails"));
  assert.ok(!hrefs.includes("/manager/sequences"));
});

test("every other existing Campaign Manager nav destination is unaffected", () => {
  const hrefs = MANAGER_NAV_SECTIONS.map((s) => s.href);
  assert.deepEqual(hrefs, [
    "/manager",
    "/manager/campaigns",
    "/manager/emails",
    "/manager/leads",
    "/manager/inbox",
    "/manager/analytics",
    "/manager/settings",
  ]);
});

test("Overview is the only exact-match section", () => {
  const exactSections = MANAGER_NAV_SECTIONS.filter((s) => s.exact).map((s) => s.href);
  assert.deepEqual(exactSections, ["/manager"]);
});
