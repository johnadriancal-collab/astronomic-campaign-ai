import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// Regression coverage for two incidents in a row:
//
// 1. A plain question typed into Astro AI ("what is a family office
//    investor") appeared to trigger Campaign Builder's Apollo pipeline.
//    Investigation (production request logs + source review) showed the
//    Astro AI page and Campaign Builder's page were already fully
//    isolated in code -- the message had actually been submitted on the
//    old "/" page, which shared "Ask Astro" branding with the real Astro
//    AI assistant.
// 2. The fix for #1 introduced a brand-new, separately-designed chat page
//    at /astro-ai instead of reusing the existing hero design at "/".
//    That was reverted -- Astro AI now lives at "/" using the original
//    hero design, wired to the chat API.
//
// These tests assert the isolation structurally, at the source level, so
// it can never silently regress even without a component-render harness.

const ASTRO_PAGE_SOURCE = readFileSync(new URL("../app/page.tsx", import.meta.url), "utf-8");
const ASTRO_LOGIC_SOURCE = readFileSync(new URL("./astro-ai-chat.ts", import.meta.url), "utf-8");

test("there is no separate Astro AI page -- app/astro-ai/page.tsx no longer exists", () => {
  assert.throws(() => readFileSync(new URL("../app/astro-ai/page.tsx", import.meta.url), "utf-8"));
});

test("the Astro AI page (at '/') calls the Astro chat API, not Campaign Builder's preview API", () => {
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

test("the Astro AI page keeps the original hero copy and chips", () => {
  assert.match(ASTRO_PAGE_SOURCE, /ASTRONOMIC INTELLIGENCE/);
  assert.match(ASTRO_PAGE_SOURCE, /What can Astro do for you\?/);
  assert.match(ASTRO_PAGE_SOURCE, /Ask Astro anything\.\.\./);
  assert.match(ASTRO_PAGE_SOURCE, /Ask Astro/);
  for (const chip of ["Create a campaign", "Find investors", "Check a prospect", "Analyze campaign"]) {
    assert.match(ASTRO_PAGE_SOURCE, new RegExp(chip.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  }
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
