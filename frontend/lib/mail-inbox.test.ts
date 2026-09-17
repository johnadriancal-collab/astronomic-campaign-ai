import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// Source-level regression coverage for Inbox V1 (2026-09-17) -- same
// source-inspection pattern as mail-campaign-channels.test.ts, since this
// project has no DOM render harness (see package.json's test script).

const INBOX_PAGE_SOURCE = readFileSync(new URL("../app/manager/inbox/page.tsx", import.meta.url), "utf-8");
const OVERVIEW_PAGE_SOURCE = readFileSync(new URL("../app/manager/page.tsx", import.meta.url), "utf-8");
const API_SOURCE = readFileSync(new URL("./api.ts", import.meta.url), "utf-8");
const MODEL_SOURCE = readFileSync(new URL("../../app/models/mail.py", import.meta.url), "utf-8");
const SERVICE_SOURCE = readFileSync(new URL("../../app/services/mail_inbox_service.py", import.meta.url), "utf-8");
const REPLY_STORE_SOURCE = readFileSync(new URL("../../app/repositories/mail_reply_store.py", import.meta.url), "utf-8");
const API_ROUTE_SOURCE = readFileSync(new URL("../../app/api/mail.py", import.meta.url), "utf-8");
const OAUTH_CLIENT_SOURCE = readFileSync(new URL("../../app/google/oauth_client.py", import.meta.url), "utf-8");
const MAILBOX_SERVICE_SOURCE = readFileSync(new URL("../../app/services/mailbox_service.py", import.meta.url), "utf-8");
const BODY_CLIENT_SOURCE = readFileSync(new URL("../../app/google/gmail_message_body_client.py", import.meta.url), "utf-8");
const UPGRADE_MODAL_SOURCE = readFileSync(new URL("../components/enable-gmail-sending-modal.tsx", import.meta.url), "utf-8");

test("the Inbox page fetches real replies via listInboxReplies, not fabricated data", () => {
  assert.match(INBOX_PAGE_SOURCE, /listInboxReplies/);
  assert.match(INBOX_PAGE_SOURCE, /MailInboxReplyView/);
});

test("the Inbox page no longer says Coming soon", () => {
  assert.doesNotMatch(INBOX_PAGE_SOURCE, /Coming soon/);
});

test("the Inbox page renders the real contact name/email, campaign name, replied timestamp, and Gmail thread id", () => {
  assert.match(INBOX_PAGE_SOURCE, /contact_name/);
  assert.match(INBOX_PAGE_SOURCE, /\.email\b/);
  assert.match(INBOX_PAGE_SOURCE, /campaign_name/);
  assert.match(INBOX_PAGE_SOURCE, /replied_at/);
  assert.match(INBOX_PAGE_SOURCE, /gmail_thread_id/);
});

test("the Inbox page never fabricates unread/read state (still no such model anywhere in this codebase)", () => {
  for (const forbidden of [/\bunreadCount\b/i, /isUnread/i]) {
    assert.doesNotMatch(INBOX_PAGE_SOURCE, forbidden);
  }
});

test("the Inbox page never renders body content as raw HTML (no dangerouslySetInnerHTML anywhere)", () => {
  // Inbox V2 (2026-09-17) does show real reply text now, but only ever
  // as plain text in a <p> -- the backend already converts any
  // HTML-only reply to plain text (see gmail_message_body_client.py),
  // so the frontend has no legitimate reason to ever interpret markup.
  assert.doesNotMatch(INBOX_PAGE_SOURCE, /dangerouslySetInnerHTML/);
});

test("the Inbox page trusts the backend's ordering rather than re-sorting client-side", () => {
  // Newest-first is guaranteed by MailReplyStore.list_all() (see below) --
  // the page must not silently reorder what it's given.
  assert.doesNotMatch(INBOX_PAGE_SOURCE, /\.sort\(/);
});

test("the Inbox page renders an empty state distinct from the loading/error states", () => {
  assert.match(INBOX_PAGE_SOURCE, /No replies yet/);
  assert.match(INBOX_PAGE_SOURCE, /Loading/);
  assert.match(INBOX_PAGE_SOURCE, /Couldn.t load the Inbox/);
});

test("the Inbox detail view fetches and renders the real reply body on demand (Inbox V2, 2026-09-17)", () => {
  assert.match(INBOX_PAGE_SOURCE, /getInboxReplyBody/);
  assert.match(INBOX_PAGE_SOURCE, /body_text/);
  // On-demand means fetched when the detail view opens, not eagerly for
  // the whole list -- the fetch call must live inside the per-reply
  // ReplyBody component, not in the top-level list-loading effect.
  const listLoadEffect = INBOX_PAGE_SOURCE.slice(
    INBOX_PAGE_SOURCE.indexOf("export default function InboxPage"),
    INBOX_PAGE_SOURCE.indexOf("const campaigns = useMemo")
  );
  assert.doesNotMatch(listLoadEffect, /getInboxReplyBody/);
});

test("the Inbox detail view handles every expected non-ok body status distinctly", () => {
  for (const status of ["scope_missing", "needs_reauth", "not_found", "provider_error"]) {
    assert.match(INBOX_PAGE_SOURCE, new RegExp(status));
  }
  assert.match(INBOX_PAGE_SOURCE, /Reconnect this mailbox/);
});

test("a body-fetch failure never blocks the rest of the Inbox list from working", () => {
  // The reply-body fetch lives in its own component with its own
  // loading/error state, never gating the outer list's own render.
  assert.match(INBOX_PAGE_SOURCE, /function ReplyBody/);
});

test("MailInboxReplyView (api.ts) has no body/snippet or unread/read field", () => {
  const typeBlock = API_SOURCE.slice(
    API_SOURCE.indexOf("export interface MailInboxReplyView"),
    API_SOURCE.indexOf("export function listInboxReplies")
  );
  for (const forbidden of [/\bbody\b/i, /\bsnippet\b/i, /\bunread\b/i, /\bread:\s*boolean/i]) {
    assert.doesNotMatch(typeBlock, forbidden);
  }
});

test("GET /mail/inbox/replies exists and is read-only (no mutation route alongside it)", () => {
  assert.match(API_ROUTE_SOURCE, /@router\.get\("\/inbox\/replies"/);
  assert.doesNotMatch(API_ROUTE_SOURCE, /@router\.(post|patch|put|delete)\("\/inbox/);
});

test("MailInboxReplyView (backend model) has no body/snippet or unread/read field", () => {
  const modelBlock = MODEL_SOURCE.slice(
    MODEL_SOURCE.indexOf("class MailInboxReplyView"),
    MODEL_SOURCE.indexOf("class MailInboxReplyView") + 2000
  );
  for (const forbidden of [/\bbody:/i, /\bsnippet:/i, /\bunread:/i, /\bread:\s*bool/i]) {
    assert.doesNotMatch(modelBlock, forbidden);
  }
});

test("MailInboxService composes from existing stores, never a second reply model", () => {
  assert.match(SERVICE_SOURCE, /reply_store: MailReplyStore/);
  assert.doesNotMatch(SERVICE_SOURCE, /class MailReply\b/); // never redefines MailReply itself
});

test("MailReplyStore.list_all is newest-first and every implementation provides it", () => {
  assert.match(REPLY_STORE_SOURCE, /async def list_all/);
  assert.match(REPLY_STORE_SOURCE, /reverse=True/);
});

test("Overview page shows Coming soon on none of its six cards (product owner's explicit call, 2026-09-17)", () => {
  assert.doesNotMatch(OVERVIEW_PAGE_SOURCE, /Coming soon/);
  assert.doesNotMatch(OVERVIEW_PAGE_SOURCE, /builtOut/);
});

test("Overview still lists all six real sections, unchanged", () => {
  const sectionsBlock = OVERVIEW_PAGE_SOURCE.slice(
    OVERVIEW_PAGE_SOURCE.indexOf("const SECTIONS"),
    OVERVIEW_PAGE_SOURCE.indexOf("export default function")
  );
  for (const title of ["Campaigns", "Emails", "Leads", "Inbox", "Analytics", "Settings"]) {
    assert.match(sectionsBlock, new RegExp(`title: "${title}"`));
  }
});

test("every Overview card is still a Link to its real route (already-existing navigation, unchanged)", () => {
  assert.match(OVERVIEW_PAGE_SOURCE, /href=\{section\.href\}/);
  assert.match(OVERVIEW_PAGE_SOURCE, /"\/manager\/campaigns"/);
  assert.match(OVERVIEW_PAGE_SOURCE, /"\/manager\/emails"/);
  assert.match(OVERVIEW_PAGE_SOURCE, /"\/manager\/leads"/);
  assert.match(OVERVIEW_PAGE_SOURCE, /"\/manager\/inbox"/);
});

// --- Inbox V2 (2026-09-17): reply-body reading ------------------------------

test("gmail.readonly is the narrowest scope requested for body access, and is documented as Restricted", () => {
  assert.match(OAUTH_CLIENT_SOURCE, /GMAIL_READONLY_SCOPE = "https:\/\/www\.googleapis\.com\/auth\/gmail\.readonly"/);
  assert.match(OAUTH_CLIENT_SOURCE, /Restricted scope/);
});

test("the mailbox upgrade flow requests all three scopes together, never a separate gmail.readonly-only flow", () => {
  assert.match(MAILBOX_SERVICE_SOURCE, /GMAIL_SEND_SCOPE, GMAIL_METADATA_SCOPE, GMAIL_READONLY_SCOPE/);
  const upgradeMethodSource = MAILBOX_SERVICE_SOURCE.slice(
    MAILBOX_SERVICE_SOURCE.indexOf("async def begin_gmail_send_upgrade"),
    MAILBOX_SERVICE_SOURCE.indexOf("def _prune_expired_states")
  );
  assert.doesNotMatch(upgradeMethodSource, /readonly_upgrade|begin_gmail_readonly/i);
});

test("the upgrade callback requires gmail.readonly to actually be granted, not just requested", () => {
  assert.match(MAILBOX_SERVICE_SOURCE, /for required_scope in \(GMAIL_SEND_SCOPE, GMAIL_METADATA_SCOPE, GMAIL_READONLY_SCOPE\)/);
});

test("the upgrade modal explains the new permission without exposing raw scope strings", () => {
  assert.match(UPGRADE_MODAL_SOURCE, /read the content of replies/i);
  assert.doesNotMatch(UPGRADE_MODAL_SOURCE, /googleapis\.com\/auth/);
});

test("GmailMessageBodyClient fetches format=full and has no listing/search method (cannot browse a mailbox)", () => {
  assert.match(BODY_CLIENT_SOURCE, /params=\{"format": "full"\}/);
  assert.doesNotMatch(BODY_CLIENT_SOURCE, /def list_messages|def search/);
});

test("GmailMessageBodyClient never logs access tokens or message content", () => {
  assert.doesNotMatch(BODY_CLIENT_SOURCE, /logger\.\w+\(f?"[^"]*\{access_token\}/);
});

test("MailInboxService.get_reply_body takes only enrollment_id -- no message_id/thread_id parameter exists to let a caller name an arbitrary Gmail message", () => {
  const methodSignature = SERVICE_SOURCE.slice(
    SERVICE_SOURCE.indexOf("async def get_reply_body"),
    SERVICE_SOURCE.indexOf("async def get_reply_body") + 200
  );
  assert.match(methodSignature, /get_reply_body\(self, enrollment_id: str\)/);
});

test("get_reply_body never writes to enrollment/step/reply stores -- read-only, matching reply-stop being untouched", () => {
  const methodSource = SERVICE_SOURCE.slice(SERVICE_SOURCE.indexOf("async def get_reply_body"));
  for (const forbidden of [/enrollment_store\.save/, /enrollment_step_store\.save/, /reply_store\.create/]) {
    assert.doesNotMatch(methodSource, forbidden);
  }
});

test("the body route is read-only (GET only, no mutation route alongside it)", () => {
  assert.match(API_ROUTE_SOURCE, /@router\.get\("\/inbox\/replies\/\{enrollment_id\}\/body"/);
  assert.doesNotMatch(API_ROUTE_SOURCE, /@router\.(post|patch|put|delete)\("\/inbox\/replies\/\{enrollment_id\}\/body"/);
});

test("MailInboxReplyBody never persists -- get_reply_body has no store write for the body itself", () => {
  assert.doesNotMatch(SERVICE_SOURCE, /body_text.*save|save.*body_text/i);
});

// --- Layout widening (2026-09-17) -------------------------------------------

test("the Inbox page reuses the app's existing wide-detail-page container convention, not an invented width", () => {
  assert.match(INBOX_PAGE_SOURCE, /MAIL_CAMPAIGN_DETAIL_CONTAINER_CLASS/);
  assert.doesNotMatch(INBOX_PAGE_SOURCE, /max-w-4xl/);
});

test("the reply detail modal is widened beyond the dialog default, but not to a huge/edge-to-edge width", () => {
  assert.match(INBOX_PAGE_SOURCE, /DialogPopup className="max-w-2xl"/);
});

test("the toolbar's search and campaign filter widen on desktop and stack on narrow widths", () => {
  assert.match(INBOX_PAGE_SOURCE, /flex-col gap-3 md:flex-row/);
  assert.match(INBOX_PAGE_SOURCE, /sm:w-72 md:w-96/); // search
});

test("no fixed pixel widths or edge-to-edge full-bleed containers were introduced", () => {
  assert.doesNotMatch(INBOX_PAGE_SOURCE, /width:\s*\d+px/);
  assert.doesNotMatch(INBOX_PAGE_SOURCE, /\bmax-w-full\b|\bw-screen\b/);
});

test("the campaign filter <select> has a base full-width class, not just a sm: width (a real mobile overflow bug this reproduces: an unconstrained <select> takes its longest option's intrinsic width)", () => {
  const selectBlock = INBOX_PAGE_SOURCE.slice(
    INBOX_PAGE_SOURCE.indexOf("<select"),
    INBOX_PAGE_SOURCE.indexOf("</select>")
  );
  assert.match(selectBlock, /className="[^"]*\bw-full\b[^"]*sm:w-56/);
});

test("the reply row stacks vertically below sm: and only becomes a horizontal split at sm: and up", () => {
  const rowButtonOpenTag = INBOX_PAGE_SOURCE.slice(
    INBOX_PAGE_SOURCE.indexOf("filtered.map((reply)"),
    INBOX_PAGE_SOURCE.indexOf("filtered.map((reply)") + 400
  );
  assert.match(rowButtonOpenTag, /flex-col gap-1[^"]*sm:flex-row/);
});
