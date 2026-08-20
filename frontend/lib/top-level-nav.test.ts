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

test("Campaign Manager and CRM remain as top-level areas", () => {
  const hrefs = TOP_LEVEL_NAV_AREAS.map((a) => a.href);
  assert.ok(hrefs.includes("/manager"));
  assert.ok(hrefs.includes("/crm"));
});

test("exactly these three top-level areas exist, in this order", () => {
  assert.deepEqual(
    TOP_LEVEL_NAV_AREAS.map((a) => a.href),
    ["/", "/manager", "/crm"]
  );
});
