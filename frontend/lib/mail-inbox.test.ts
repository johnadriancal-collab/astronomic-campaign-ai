import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// Source-level regression coverage for Inbox V1 (2026-09-17) -- same
// source-inspection pattern as mail-campaign-channels.test.ts, since this
// project has no DOM render harness (see package.json's test script).

const INBOX_PAGE_SOURCE = readFileSync(new URL("../app/manager/inbox/page.tsx", import.meta.url), "utf-8");
const DETAIL_PAGE_SOURCE = readFileSync(new URL("../app/manager/inbox/[enrollment_id]/page.tsx", import.meta.url), "utf-8");
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

test("the Inbox list renders the real contact name/email, campaign name, and replied timestamp per row", () => {
  assert.match(INBOX_PAGE_SOURCE, /contact_name/);
  assert.match(INBOX_PAGE_SOURCE, /\.email\b/);
  assert.match(INBOX_PAGE_SOURCE, /campaign_name/);
  assert.match(INBOX_PAGE_SOURCE, /replied_at/);
});

test("the reply detail page renders the Gmail thread id (a detail-only field, not shown in the list row)", () => {
  assert.match(DETAIL_PAGE_SOURCE, /gmail_thread_id/);
});

test("the Inbox page never fabricates unread/read state (still no such model anywhere in this codebase)", () => {
  for (const forbidden of [/\bunreadCount\b/i, /isUnread/i]) {
    assert.doesNotMatch(INBOX_PAGE_SOURCE, forbidden);
  }
});

test("neither the Inbox list nor the reply detail page ever renders body content as raw HTML (no dangerouslySetInnerHTML anywhere)", () => {
  // Inbox V2 (2026-09-17) does show real reply text now, but only ever
  // as plain text plus explicitly-constructed <a> links -- the backend
  // already converts any HTML-only reply to plain text (see
  // gmail_message_body_client.py), so the frontend has no legitimate
  // reason to ever interpret markup.
  assert.doesNotMatch(INBOX_PAGE_SOURCE, /dangerouslySetInnerHTML/);
  assert.doesNotMatch(DETAIL_PAGE_SOURCE, /dangerouslySetInnerHTML/);
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

test("the Inbox list no longer fetches or renders reply bodies at all (moved to the dedicated detail page)", () => {
  assert.doesNotMatch(INBOX_PAGE_SOURCE, /getInboxReplyBody/);
  assert.doesNotMatch(INBOX_PAGE_SOURCE, /body_text/);
});

test("the reply detail page fetches and renders the real reply body on demand (Inbox V2, 2026-09-17)", () => {
  assert.match(DETAIL_PAGE_SOURCE, /getInboxReplyBody/);
  assert.match(DETAIL_PAGE_SOURCE, /body_text/);
  // On-demand means fetched when the detail page mounts for THIS one
  // reply, not eagerly for the whole list -- the fetch call must live
  // inside the per-reply body section, keyed off the route param.
  assert.match(DETAIL_PAGE_SOURCE, /getInboxReplyBody\(enrollmentId\)/);
});

test("the reply detail page handles every expected non-ok body status distinctly", () => {
  for (const status of ["scope_missing", "needs_reauth", "not_found", "provider_error"]) {
    assert.match(DETAIL_PAGE_SOURCE, new RegExp(status));
  }
  assert.match(DETAIL_PAGE_SOURCE, /Reconnect this mailbox/);
});

test("a body-fetch failure never blocks the rest of the reply page (metadata sidebar) from working", () => {
  // The reply-body fetch lives in its own component with its own
  // loading/error state, never gating the metadata sidebar's own render.
  assert.match(DETAIL_PAGE_SOURCE, /function ReplyBodySection/);
});

test("the reply body is the visually dominant element -- larger text than the metadata sidebar, given its own Card", () => {
  const bodySection = DETAIL_PAGE_SOURCE.slice(
    DETAIL_PAGE_SOURCE.indexOf("function ReplyBodySection"),
    DETAIL_PAGE_SOURCE.indexOf("function RetryButton")
  );
  assert.match(bodySection, /text-base leading-relaxed/);
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

test("reply reading no longer happens in a modal anywhere (Inbox V2 UX change, 2026-09-17)", () => {
  for (const modalSource of [INBOX_PAGE_SOURCE, DETAIL_PAGE_SOURCE]) {
    assert.doesNotMatch(modalSource, /<Dialog[\s>]/);
    assert.doesNotMatch(modalSource, /DialogPopup/);
  }
});

test("the Inbox list row is a real navigation (next/link Link), not a modal-opening button", () => {
  const rowBlock = INBOX_PAGE_SOURCE.slice(
    INBOX_PAGE_SOURCE.indexOf("filtered.map((reply)"),
    INBOX_PAGE_SOURCE.indexOf("filtered.map((reply)") + 800
  );
  // Every cell wraps its content in a <Link> to the SAME row href (2026-
  // 09-18: real <table> with an explicit header row, per cell clickable
  // rather than one div-shaped anchor) -- still a real next/link
  // navigation, never a modal.
  assert.match(rowBlock, /const href = `\/manager\/inbox\/\$\{reply\.enrollment_id\}`/);
  assert.match(rowBlock, /<Link\b/);
  assert.match(rowBlock, /href=\{href\}/);
  assert.doesNotMatch(rowBlock, /onClick=\{\(\) => setSelected/);
});

test("the reply detail page is a comfortable reading width, not huge/edge-to-edge", () => {
  assert.match(DETAIL_PAGE_SOURCE, /max-w-6xl/);
  assert.doesNotMatch(DETAIL_PAGE_SOURCE, /max-w-full\b|w-screen\b/);
  // The reading column itself is capped to a real prose measure, not
  // stretched across the whole wide page.
  assert.match(DETAIL_PAGE_SOURCE, /max-w-\[65ch\]/);
});

test("the reply detail page has a Back to Inbox link", () => {
  assert.match(DETAIL_PAGE_SOURCE, /Back to Inbox/);
  assert.match(DETAIL_PAGE_SOURCE, /href="\/manager\/inbox"/);
});

test("the reply detail page splits new reply text from quoted/previous content using the real, tested heuristic -- never a fabricated ad hoc regex", () => {
  assert.match(DETAIL_PAGE_SOURCE, /splitReplyQuote/);
  assert.match(DETAIL_PAGE_SOURCE, /New reply/);
  assert.match(DETAIL_PAGE_SOURCE, /Previous \/ quoted message/);
  // Only renders the quoted block when one was actually found.
  assert.match(DETAIL_PAGE_SOURCE, /\{quoted && \(/);
});

test("the reply detail page linkifies URLs via the tested pure helper, never dangerouslySetInnerHTML or a raw regex inline", () => {
  assert.match(DETAIL_PAGE_SOURCE, /linkifySegments/);
  assert.match(DETAIL_PAGE_SOURCE, /target="_blank"/);
  assert.match(DETAIL_PAGE_SOURCE, /rel="noopener noreferrer nofollow"/);
});

test("the reply detail page's metadata sidebar renders every required field and the campaign/contact links", () => {
  for (const field of [
    "mailbox_email",
    "subject",
    "replied_at",
    "gmail_thread_id",
    "gmail_message_id",
    "enrollment_status",
    "skipped_step_numbers",
  ]) {
    assert.match(DETAIL_PAGE_SOURCE, new RegExp(field));
  }
  assert.match(DETAIL_PAGE_SOURCE, /href=\{`\/manager\/campaigns\/mail\/\$\{reply\.mail_campaign_id\}`\}/);
  assert.match(DETAIL_PAGE_SOURCE, /href=\{`\/crm\/\$\{reply\.crm_contact_id\}`\}/);
});

test("the reply detail page only links to a contact when one was actually resolved (contact_name present)", () => {
  const contactLinkBlock = DETAIL_PAGE_SOURCE.slice(
    DETAIL_PAGE_SOURCE.indexOf("reply.contact_name &&"),
    DETAIL_PAGE_SOURCE.indexOf("reply.contact_name &&") + 350
  );
  assert.match(contactLinkBlock, /View in Contacts/);
});

test("the reply detail page's layout stacks the metadata sidebar below the body on narrow screens and sits beside it on large screens", () => {
  assert.match(DETAIL_PAGE_SOURCE, /grid gap-8 lg:grid-cols-\[1fr_320px\]/);
});

test("the reply detail page handles a missing/404 reply gracefully rather than crashing", () => {
  assert.match(DETAIL_PAGE_SOURCE, /No reply found for this enrollment/);
});

test("GET /mail/inbox/replies/{enrollment_id} (single-reply metadata) exists, is read-only, and is distinct from the /body route", () => {
  assert.match(API_ROUTE_SOURCE, /@router\.get\("\/inbox\/replies\/\{enrollment_id\}"/);
  assert.doesNotMatch(API_ROUTE_SOURCE, /@router\.(post|patch|put|delete)\("\/inbox\/replies\/\{enrollment_id\}"/);
});

test("MailInboxService.get_reply is the single-row counterpart to list_replies, sharing the same join logic", () => {
  assert.match(SERVICE_SOURCE, /async def get_reply\(self, enrollment_id: str\)/);
  assert.match(SERVICE_SOURCE, /_build_view/);
});

test("the toolbar's search and campaign filter widen on desktop and stack on narrow widths -- same convention as the Campaigns/Leads toolbars (2026-09-18 density pass)", () => {
  assert.match(INBOX_PAGE_SOURCE, /flex-col gap-3 lg:flex-row/);
  assert.match(INBOX_PAGE_SOURCE, /w-full min-w-0 sm:w-64/); // search, matching Leads' own search input width
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

// --- Explicit table headers + Subject column (2026-09-18) --------------------

test("the Inbox list renders an explicit header row with exactly Lead, Email, Subject, Campaign, Last reply, in that order", () => {
  const theadBlock = INBOX_PAGE_SOURCE.slice(INBOX_PAGE_SOURCE.indexOf("<thead"), INBOX_PAGE_SOURCE.indexOf("</thead>"));
  const headers = [...theadBlock.matchAll(/<th[^>]*>([^<]+)<\/th>/g)].map((m) => m[1].trim());
  assert.deepEqual(headers, ["Lead", "Email", "Subject", "Campaign", "Last reply"]);
});

test("the Replied badge no longer renders in the Inbox list", () => {
  assert.doesNotMatch(INBOX_PAGE_SOURCE, />Replied</);
  assert.doesNotMatch(INBOX_PAGE_SOURCE, /bg-emerald-100/);
});

test("each row renders the real subject (falling back to an em dash, never a fabricated string built from the campaign name)", () => {
  assert.match(INBOX_PAGE_SOURCE, /const subject = reply\.subject \?\? "—"/);
  assert.doesNotMatch(INBOX_PAGE_SOURCE, /reply\.campaign_name.*subject|subject.*=.*campaign_name/);
});

test("every cell (Lead, Email, Subject, Campaign, Last reply) wraps its content in a Link to the same row href -- the whole row is clickable, still a real navigation", () => {
  const mapStart = INBOX_PAGE_SOURCE.indexOf("filtered.map((reply)");
  const rowBlock = INBOX_PAGE_SOURCE.slice(mapStart, INBOX_PAGE_SOURCE.indexOf("</tr>", mapStart) + 10);
  const linkCount = [...rowBlock.matchAll(/<Link\b/g)].length;
  assert.equal(linkCount, 5);
  const hrefCount = [...rowBlock.matchAll(/href=\{href\}/g)].length;
  assert.equal(hrefCount, 5);
});

test("Lead, Email, Subject, and Campaign each truncate to one line with their own title tooltip", () => {
  assert.match(INBOX_PAGE_SOURCE, /title=\{name\}/);
  assert.match(INBOX_PAGE_SOURCE, /title=\{reply\.email\}/);
  assert.match(INBOX_PAGE_SOURCE, /title=\{subject\}/);
  assert.match(INBOX_PAGE_SOURCE, /title=\{reply\.campaign_name\}/);
  // Every truncating cell also carries the CSS that actually makes
  // truncation happen -- not just a tooltip with no ellipsis behind it.
  for (const marker of ["title={name}", "title={reply.email}", "title={subject}", "title={reply.campaign_name}"]) {
    const idx = INBOX_PAGE_SOURCE.indexOf(marker);
    const cellBlock = INBOX_PAGE_SOURCE.slice(idx - 40, idx + 200);
    assert.match(cellBlock, /truncate/);
    assert.match(cellBlock, /whitespace-nowrap/);
  }
});

test("stored contact names/emails/subjects/campaign names are never manually shortened -- truncation is CSS-only", () => {
  assert.doesNotMatch(INBOX_PAGE_SOURCE, /\.slice\(0,|\.substring\(0,/);
});

test("Last reply shows a real relative-time string derived from replied_at, with the exact local date/time as a title tooltip", () => {
  assert.match(INBOX_PAGE_SOURCE, /formatRelativeTime\(reply\.replied_at\)/);
  assert.match(INBOX_PAGE_SOURCE, /const exactReplyTime = new Date\(reply\.replied_at\)\.toLocaleString\(\)/);
  assert.match(INBOX_PAGE_SOURCE, /title=\{exactReplyTime\}/);
});

test("formatRelativeTime produces QuickMail-style phrasing and never mutates/replaces the stored timestamp", () => {
  const fnBlock = INBOX_PAGE_SOURCE.slice(
    INBOX_PAGE_SOURCE.indexOf("function formatRelativeTime"),
    INBOX_PAGE_SOURCE.indexOf("export default function InboxPage")
  );
  assert.match(fnBlock, /just now/);
  assert.match(fnBlock, /about \$\{minutes\} minute/);
  assert.match(fnBlock, /about \$\{hours\} hour/);
  assert.match(fnBlock, /about 1 day ago/);
  assert.match(fnBlock, /\$\{days\} days ago/);
  // Purely a derived display string -- never writes back to `reply` or
  // calls a setter with a recomputed timestamp.
  assert.doesNotMatch(fnBlock, /setReplies|reply\.replied_at\s*=/);
});

test("search now also matches on subject, in addition to the existing name/email/campaign fields", () => {
  const filterBlock = INBOX_PAGE_SOURCE.slice(INBOX_PAGE_SOURCE.indexOf("const filtered = useMemo"), INBOX_PAGE_SOURCE.indexOf("}, [replies, search, campaignFilter]);"));
  assert.match(filterBlock, /r\.contact_name/);
  assert.match(filterBlock, /r\.email\.toLowerCase/);
  assert.match(filterBlock, /r\.campaign_name\.toLowerCase/);
  assert.match(filterBlock, /r\.subject \?\? ""/);
});

test("the reply list scrolls horizontally in its own container rather than corrupting the page on narrow screens", () => {
  assert.match(INBOX_PAGE_SOURCE, /overflow-x-auto/);
  assert.match(INBOX_PAGE_SOURCE, /min-w-\[1080px\]/);
});

test("row cell padding is tighter than the original px-6/px-8 py-4 pass", () => {
  assert.doesNotMatch(INBOX_PAGE_SOURCE, /px-6 py-4/);
  assert.doesNotMatch(INBOX_PAGE_SOURCE, /sm:px-8/);
  assert.match(INBOX_PAGE_SOURCE, /px-3 py-1\.5/);
});

test("Subject and Campaign are the wider columns; Last reply stays compact", () => {
  const theadBlock = INBOX_PAGE_SOURCE.slice(INBOX_PAGE_SOURCE.indexOf("<thead"), INBOX_PAGE_SOURCE.indexOf("</thead>"));
  assert.match(theadBlock, /max-w-\[360px\][^>]*>Subject/);
  assert.match(theadBlock, /w-\[220px\][^>]*>Campaign/);
  assert.match(theadBlock, /w-\[140px\][^>]*>Last reply/);
});
