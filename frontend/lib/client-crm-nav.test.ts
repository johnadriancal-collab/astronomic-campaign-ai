import assert from "node:assert/strict";
import { test } from "node:test";
import { CLIENT_CRM_NAV_SECTIONS } from "./client-crm-nav.ts";

test("Stage 1C exposes exactly one nav section: Clients", () => {
  assert.deepEqual(
    CLIENT_CRM_NAV_SECTIONS.map((s) => s.href),
    ["/clients"]
  );
  assert.equal(CLIENT_CRM_NAV_SECTIONS[0].label, "Clients");
  assert.equal(CLIENT_CRM_NAV_SECTIONS[0].exact, true);
});

test("no Pipeline/Follow-ups/Tasks/Analytics nav items exist yet", () => {
  const labels = CLIENT_CRM_NAV_SECTIONS.map((s) => s.label.toLowerCase());
  for (const forbidden of ["pipeline", "follow-up", "task", "analytics"]) {
    assert.ok(!labels.some((l) => l.includes(forbidden)), `did not expect a nav label mentioning "${forbidden}"`);
  }
});
