import assert from "node:assert/strict";
import { test } from "node:test";
import { isPublicProxyPath, sanitizeNextPath } from "./auth.ts";

test("sanitizeNextPath accepts a plain relative path", () => {
  assert.equal(sanitizeNextPath("/crm"), "/crm");
  assert.equal(sanitizeNextPath("/manager/campaigns"), "/manager/campaigns");
});

test("sanitizeNextPath preserves a query string on the relative path", () => {
  assert.equal(sanitizeNextPath("/crm?tab=lists"), "/crm?tab=lists");
});

test("sanitizeNextPath falls back to / for null or empty input", () => {
  assert.equal(sanitizeNextPath(null), "/");
  assert.equal(sanitizeNextPath(undefined), "/");
  assert.equal(sanitizeNextPath(""), "/");
});

test("sanitizeNextPath rejects a protocol-relative URL (open-redirect attempt)", () => {
  assert.equal(sanitizeNextPath("//evil.com"), "/");
  assert.equal(sanitizeNextPath("//evil.com/phishing"), "/");
});

test("sanitizeNextPath rejects an absolute URL", () => {
  assert.equal(sanitizeNextPath("https://evil.com"), "/");
  assert.equal(sanitizeNextPath("http://evil.com/crm"), "/");
});

test("sanitizeNextPath rejects a path with no leading slash", () => {
  assert.equal(sanitizeNextPath("crm"), "/");
  assert.equal(sanitizeNextPath("evil.com"), "/");
});

// --- isPublicProxyPath ---------------------------------------------------
// Regression coverage for a real bug caught during manual verification:
// without excluding "/backend/*", every API fetch (including the login
// form's own POST /backend/auth/login) would be redirected to the /login
// PAGE's HTML instead of ever reaching the real backend, because Next.js's
// Proxy step runs before next.config.ts's rewrite to the backend.

test("isPublicProxyPath allows every /backend/* API call through", () => {
  assert.equal(isPublicProxyPath("/backend/health"), true);
  assert.equal(isPublicProxyPath("/backend/auth/login"), true);
  assert.equal(isPublicProxyPath("/backend/crm/contacts"), true);
  assert.equal(isPublicProxyPath("/backend/mailboxes"), true);
});

test("isPublicProxyPath allows the /login page itself through", () => {
  assert.equal(isPublicProxyPath("/login"), true);
});

test("isPublicProxyPath gates every real page route", () => {
  assert.equal(isPublicProxyPath("/"), false);
  assert.equal(isPublicProxyPath("/crm"), false);
  assert.equal(isPublicProxyPath("/manager"), false);
  assert.equal(isPublicProxyPath("/manager/emails"), false);
});

test("isPublicProxyPath does not accidentally match a path that merely starts with 'login'", () => {
  assert.equal(isPublicProxyPath("/loginhack"), false);
});
