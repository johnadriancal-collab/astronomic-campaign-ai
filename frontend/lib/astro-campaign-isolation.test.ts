import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// Regression coverage for the incident where a plain question typed into
// Astro AI ("what is a family office investor") appeared to trigger
// Campaign Builder's Apollo pipeline. Investigation (production request
// logs + source review) showed the two pages were already fully isolated
// in code -- the message had actually been submitted on the old "/" page,
// which shared "Ask Astro" branding with the real Astro AI assistant.
// These tests assert that isolation structurally, at the source level, so
// it can never silently regress even without a component-render harness.

const ASTRO_PAGE_SOURCE = readFileSync(
  new URL("../app/astro-ai/page.tsx", import.meta.url),
  "utf-8"
);
const ASTRO_LOGIC_SOURCE = readFileSync(new URL("./astro-ai-chat.ts", import.meta.url), "utf-8");

test("the Astro AI page calls the Astro chat API, not Campaign Builder's preview API", () => {
  assert.match(ASTRO_PAGE_SOURCE, /sendAstroChatMessage/);
  assert.doesNotMatch(ASTRO_PAGE_SOURCE, /previewCampaign/);
});

test("the Astro AI page never touches the campaign store or Apollo pipeline", () => {
  assert.doesNotMatch(ASTRO_PAGE_SOURCE, /useCampaignStore/);
  assert.doesNotMatch(ASTRO_PAGE_SOURCE, /searchProspects|buildCampaign/);
});

test("the Astro AI page never navigates to /results or /campaign-builder", () => {
  assert.doesNotMatch(ASTRO_PAGE_SOURCE, /\/results/);
  assert.doesNotMatch(ASTRO_PAGE_SOURCE, /\/campaign-builder/);
  // No router at all -- a plain chat page has nothing to navigate for.
  assert.doesNotMatch(ASTRO_PAGE_SOURCE, /useRouter/);
});

test("Astro AI's pure submit logic has no campaign/Apollo/routing concerns", () => {
  assert.doesNotMatch(ASTRO_LOGIC_SOURCE, /campaign|apollo|router|prospect/i);
});

test("Campaign Manager's Apollo choice card no longer says 'Continue to Astro'", () => {
  const source = readFileSync(
    new URL("../app/manager/campaigns/new/page.tsx", import.meta.url),
    "utf-8"
  );
  assert.doesNotMatch(source, /Continue to Astro\b/);
  assert.match(source, /\/campaign-builder/);
});
