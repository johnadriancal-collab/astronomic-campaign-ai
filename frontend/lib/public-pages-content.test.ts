import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// Static source-text verification for the three public OAuth Branding
// pages -- no component-render harness exists in this frontend (see
// lib/sidebar-nav-source.test.ts's own comment for why source-scanning
// is this codebase's established substitute), so page content, metadata,
// and the internal-vs-public robots split are all verified by reading
// the .tsx source directly.

const ROOT_LAYOUT = readFileSync(new URL("../app/layout.tsx", import.meta.url), "utf-8");
const ABOUT_PAGE = readFileSync(new URL("../app/about/page.tsx", import.meta.url), "utf-8");
const PRIVACY_PAGE = readFileSync(new URL("../app/privacy/page.tsx", import.meta.url), "utf-8");
const TERMS_PAGE = readFileSync(new URL("../app/terms/page.tsx", import.meta.url), "utf-8");
const SHELL = readFileSync(new URL("../components/public-page-shell.tsx", import.meta.url), "utf-8");

// Rendered JSX only (from the default export's `return` onward) -- used
// for checks about what actually reaches the page, so an explanatory
// source comment (e.g. "deliberately does NOT name a registered
// address") can't trip its own guard by mentioning the forbidden phrase
// while explaining why it's absent.
function renderedContentOnly(source: string): string {
  const marker = source.indexOf("return (");
  return marker === -1 ? source : source.slice(marker);
}

const TERMS_RENDERED = renderedContentOnly(TERMS_PAGE);

// --- robots / indexability ---------------------------------------------------

test("the root layout still defaults every Hub page to noindex -- unchanged by this feature", () => {
  assert.match(ROOT_LAYOUT, /robots:\s*{\s*index:\s*false,\s*follow:\s*false/);
});

test("each of /about, /privacy, /terms overrides robots to index:true, follow:true", () => {
  for (const [name, source] of [
    ["about", ABOUT_PAGE],
    ["privacy", PRIVACY_PAGE],
    ["terms", TERMS_PAGE],
  ] as const) {
    assert.match(
      source,
      /robots:\s*{\s*index:\s*true,\s*follow:\s*true\s*}/,
      `expected ${name}/page.tsx to export robots:{index:true,follow:true}`
    );
  }
});

// --- server-renderable, not a client-only shell -------------------------------

test("none of the three public pages is a client component", () => {
  // A page marked "use client" still server-renders in Next.js, but the
  // whole point of this check is to catch an accidental future switch
  // toward client-only rendering for a page Google's crawler must be
  // able to read without executing JS.
  for (const [name, source] of [
    ["about", ABOUT_PAGE],
    ["privacy", PRIVACY_PAGE],
    ["terms", TERMS_PAGE],
  ] as const) {
    assert.doesNotMatch(source, /^"use client"/m, `expected ${name}/page.tsx to be a server component`);
  }
});

// --- required copy -------------------------------------------------------------

test("/about explains Astronomic Hub and Astronomic Mail, and that Google authorization is user-initiated", () => {
  assert.match(ABOUT_PAGE, /Astronomic Hub is Astronomic.s internal platform/);
  assert.match(ABOUT_PAGE, /Astronomic Mail/);
  assert.match(ABOUT_PAGE, /connect their own Google account/);
  assert.match(ABOUT_PAGE, /always initiated explicitly/);
});

test("/about links to both /privacy and /terms", () => {
  assert.match(ABOUT_PAGE, /href="\/privacy"/);
  assert.match(ABOUT_PAGE, /href="\/terms"/);
});

test("/privacy states the exact scope used and includes an effective date", () => {
  assert.match(PRIVACY_PAGE, /gmail\.send/);
  assert.match(PRIVACY_PAGE, /Last updated/);
});

test("/privacy explicitly disclaims reading Gmail messages, inbox contents, or contacts", () => {
  assert.match(PRIVACY_PAGE, /does not request, and does not have, access to read your Gmail messages/);
  assert.match(PRIVACY_PAGE, /access your Google Contacts/);
});

test("/privacy never mentions a Gmail/Google scope other than the ones this app actually requests", () => {
  // openid/email/profile (base identity) and gmail.send (sending) are the
  // ONLY scopes this app's OAuth client ever requests -- see
  // app/google/oauth_client.py's SCOPES / GMAIL_SEND_SCOPE. Any of these
  // appearing here would mean the policy has drifted from the code (or
  // vice versa) without the other being updated.
  for (const forbiddenScope of ["gmail.readonly", "gmail.modify", "gmail.metadata", "gmail.compose", "contacts.readonly"]) {
    assert.doesNotMatch(PRIVACY_PAGE, new RegExp(forbiddenScope.replace(".", "\\.")));
  }
});

test("/privacy covers refresh-token storage, access-token handling, retention/deletion, and Google's User Data Policy", () => {
  assert.match(PRIVACY_PAGE, /encrypted/);
  assert.match(PRIVACY_PAGE, /not stored afterward/);
  assert.match(PRIVACY_PAGE, /disconnect/i);
  assert.match(PRIVACY_PAGE, /Google API Services User Data Policy/);
  assert.match(PRIVACY_PAGE, /Limited Use/);
});

test("/privacy does not claim a compliance certification this app doesn't have", () => {
  for (const uncheckedClaim of ["SOC 2", "SOC2", "ISO 27001", "HIPAA", "GDPR compliant", "CCPA compliant"]) {
    assert.doesNotMatch(PRIVACY_PAGE, new RegExp(uncheckedClaim.replace(/\s/g, "\\s*")));
  }
});

test("/terms covers authorized use, connected Google services, acceptable use, availability, and termination", () => {
  assert.match(TERMS_PAGE, /[Aa]uthorized/);
  assert.match(TERMS_PAGE, /Google/);
  assert.match(TERMS_PAGE, /[Aa]cceptable use/);
  assert.match(TERMS_PAGE, /available|availability/i);
  assert.match(TERMS_PAGE, /[Tt]ermination/);
});

test("/terms does not invent a legal entity type, registered address, or governing-law jurisdiction", () => {
  for (const invented of [/\bLLC\b/, /\bInc\.\b/, /\bCorp(oration)?\b/, /governing law/i, /State of [A-Z]/, /registered address/i]) {
    assert.doesNotMatch(TERMS_RENDERED, invented);
  }
});

// --- cross-linking / shared shell ------------------------------------------------

test("the shared public-page shell cross-links About, Privacy, Terms, and Sign in", () => {
  assert.match(SHELL, /href="\/about"/);
  assert.match(SHELL, /href="\/privacy"/);
  assert.match(SHELL, /href="\/terms"/);
  assert.match(SHELL, /href="\/login"/);
});

test("every public page uses descriptive titles and meta descriptions, not the generic Hub default", () => {
  for (const [name, source] of [
    ["about", ABOUT_PAGE],
    ["privacy", PRIVACY_PAGE],
    ["terms", TERMS_PAGE],
  ] as const) {
    assert.match(source, /title:\s*"[^"]{5,}"/, `expected ${name}/page.tsx to export a real title`);
    assert.match(source, /description:\s*\n?\s*"[^"]{20,}"/, `expected ${name}/page.tsx to export a real description`);
  }
});
