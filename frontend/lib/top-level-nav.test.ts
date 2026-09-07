import assert from "node:assert/strict";
import { test } from "node:test";
import { TOP_LEVEL_NAV_AREAS } from "./top-level-nav.ts";

test("Astro AI is present at '/' -- the Hub's AI entry point", () => {
  const astro = TOP_LEVEL_NAV_AREAS.find((a) => a.label === "Astro AI");
  assert.ok(astro, "expected an Astro AI top-level area");
  assert.equal(astro.href, "/");
});

test("Campaign Builder is no longer a top-level nav area", () => {
  const hrefs = TOP_LEVEL_NAV_AREAS.map((a) => a.href);
  const labels = TOP_LEVEL_NAV_AREAS.map((a) => a.label);
  assert.ok(!hrefs.includes("/campaign-builder"), "Campaign Builder must not be a top-level nav destination");
  assert.ok(!labels.includes("Campaign Builder"));
});

test("Campaign Manager and Contacts (/crm) remain as top-level areas", () => {
  const hrefs = TOP_LEVEL_NAV_AREAS.map((a) => a.href);
  assert.ok(hrefs.includes("/manager"));
  assert.ok(hrefs.includes("/crm"));
});

test("the /crm area's display label is Contacts, not CRM -- the route itself is unchanged", () => {
  const contacts = TOP_LEVEL_NAV_AREAS.find((a) => a.href === "/crm");
  assert.ok(contacts, "expected a /crm top-level area");
  assert.equal(contacts.label, "Contacts");
});

test("Client CRM is present at '/clients' as the fourth top-level area (Stage 1C)", () => {
  const clientCrm = TOP_LEVEL_NAV_AREAS.find((a) => a.href === "/clients");
  assert.ok(clientCrm, "expected a /clients top-level area");
  assert.equal(clientCrm.label, "Client CRM");
});

test("exactly these four top-level areas exist, in this order", () => {
  assert.deepEqual(
    TOP_LEVEL_NAV_AREAS.map((a) => a.href),
    ["/", "/manager", "/crm", "/clients"]
  );
});
