import assert from "node:assert/strict";
import { test } from "node:test";

test("'/astro-ai' redirects to '/' -- Astro AI lives at the root using the original hero design", async () => {
  const { default: nextConfig } = await import("../next.config.ts");
  const redirects = await nextConfig.redirects!();
  const astroRedirect = redirects.find((r) => r.source === "/astro-ai");
  assert.ok(astroRedirect, "expected a redirect rule for '/astro-ai'");
  assert.equal(astroRedirect.destination, "/");
});

test("'/' is not redirected away -- it is Astro AI's real page, not a redirect target", async () => {
  const { default: nextConfig } = await import("../next.config.ts");
  const redirects = await nextConfig.redirects!();
  const rootRedirect = redirects.find((r) => r.source === "/");
  assert.equal(rootRedirect, undefined);
});

test("/campaign-builder is not redirected away -- Campaign Manager's Apollo flow still needs it", async () => {
  const { default: nextConfig } = await import("../next.config.ts");
  const redirects = await nextConfig.redirects!();
  const hijacked = redirects.find((r) => r.source === "/campaign-builder");
  assert.equal(hijacked, undefined);
});
