import assert from "node:assert/strict";
import { test } from "node:test";

test("'/' redirects to /astro-ai now that Astro AI is the canonical AI entry point", async () => {
  const { default: nextConfig } = await import("../next.config.ts");
  const redirects = await nextConfig.redirects!();
  const rootRedirect = redirects.find((r) => r.source === "/");
  assert.ok(rootRedirect, "expected a redirect rule for '/'");
  assert.equal(rootRedirect.destination, "/astro-ai");
});

test("/campaign-builder is not redirected away -- Campaign Manager's Apollo flow still needs it", async () => {
  const { default: nextConfig } = await import("../next.config.ts");
  const redirects = await nextConfig.redirects!();
  const hijacked = redirects.find((r) => r.source === "/campaign-builder");
  assert.equal(hijacked, undefined);
});
