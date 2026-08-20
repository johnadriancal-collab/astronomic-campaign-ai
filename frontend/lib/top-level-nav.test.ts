import assert from "node:assert/strict";
import { test } from "node:test";
import { TOP_LEVEL_NAV_AREAS } from "./top-level-nav.ts";

test("Astro AI is present and is the Hub's AI entry point", () => {
  const astro = TOP_LEVEL_NAV_AREAS.find((a) => a.href === "/astro-ai");
  assert.ok(astro, "expected an /astro-ai top-level area");
  assert.equal(astro.label, "Astro AI");
});

test("Campaign Builder is no longer a top-level nav area", () => {
  const hrefs = TOP_LEVEL_NAV_AREAS.map((a) => a.href);
  const labels = TOP_LEVEL_NAV_AREAS.map((a) => a.label);
  assert.ok(!hrefs.includes("/"), "root '/' must not be a top-level nav destination");
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
    ["/astro-ai", "/manager", "/crm"]
  );
});
