import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// No component-render harness exists in this frontend (see lib/*.test.ts
// generally -- everything here is plain-function testing), so the CRM and
// Campaign Manager sidebars' hardcoded top-area links are verified by
// scanning their source directly. This guards the "Campaign Builder nav
// item removed, Astro AI present at '/'" requirement that
// lib/top-level-nav.test.ts covers for the shared site header data array.

const MANAGER_SIDEBAR = readFileSync(
  new URL("../components/manager-sidebar.tsx", import.meta.url),
  "utf-8"
);
const CRM_SIDEBAR = readFileSync(new URL("../components/crm-sidebar.tsx", import.meta.url), "utf-8");
const CLIENT_CRM_SIDEBAR = readFileSync(new URL("../components/client-crm-sidebar.tsx", import.meta.url), "utf-8");

test("Campaign Builder is not linked from the Campaign Manager sidebar", () => {
  assert.doesNotMatch(MANAGER_SIDEBAR, /Campaign Builder/);
});

test("Campaign Builder is not linked from the CRM sidebar", () => {
  assert.doesNotMatch(CRM_SIDEBAR, /Campaign Builder/);
});

test("Campaign Builder is not linked from the Client CRM sidebar", () => {
  assert.doesNotMatch(CLIENT_CRM_SIDEBAR, /Campaign Builder/);
});

test("Astro AI remains linked from all three sidebars, pointing at '/'", () => {
  for (const sidebar of [MANAGER_SIDEBAR, CRM_SIDEBAR, CLIENT_CRM_SIDEBAR]) {
    assert.match(sidebar, /Astro AI/);
    assert.match(sidebar, /href="\/"/);
    // The Astro AI link itself must not point at the retired /astro-ai page.
    assert.doesNotMatch(sidebar, /href="\/astro-ai"/);
  }
});

test("Client CRM (Stage 1C) is cross-linked from the Campaign Manager and CRM sidebars", () => {
  assert.match(MANAGER_SIDEBAR, /href="\/clients"/);
  assert.match(MANAGER_SIDEBAR, /Client CRM/);
  assert.match(CRM_SIDEBAR, /href="\/clients"/);
  assert.match(CRM_SIDEBAR, /Client CRM/);
});

test("no Pipeline/Follow-ups/Tasks/Analytics nav item is hardcoded in the Client CRM sidebar's own source", () => {
  for (const forbidden of [/Pipeline/, /Follow-up/, /\bTasks?\b/, /Analytics/]) {
    assert.doesNotMatch(CLIENT_CRM_SIDEBAR, forbidden);
  }
});

test("the /crm area is cross-linked/self-labeled as Contacts, not bare CRM, in all three sidebars", () => {
  for (const sidebar of [MANAGER_SIDEBAR, CRM_SIDEBAR, CLIENT_CRM_SIDEBAR]) {
    assert.match(sidebar, /Contacts/);
    // "Client CRM" (display text) and "CLIENT_CRM_..." (import identifiers)
    // legitimately contain "CRM" -- strip both before asserting the bare
    // area-identifying display label "CRM" is gone.
    const withoutKnownUses = sidebar.replace(/Client CRM/g, "").replace(/CLIENT_CRM_\w*/g, "");
    assert.ok(!withoutKnownUses.includes("CRM"), "expected no bare 'CRM' label outside of 'Client CRM'");
  }
});
