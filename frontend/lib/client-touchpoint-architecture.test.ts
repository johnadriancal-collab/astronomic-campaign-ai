import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// Client CRM Stage 2A's own explicit backend rule: a Touchpoint's
// crm_contact_id must already be an active ClientContact of THIS Client --
// an arbitrary global AstroHub Contact is never attachable directly. The
// Stage 2B frontend picker must reflect that restriction structurally: it
// filters this Client's own already-loaded ClientContacts, and never
// reaches for the shared CrmContactPicker (which searches every Contact
// in the system). Scanned directly from source, same "no
// component-render harness" convention as sidebar-nav-source.test.ts /
// client-contact-architecture.test.ts.

const TOUCHPOINT_FORM_MODAL = readFileSync(new URL("../components/touchpoint-form-modal.tsx", import.meta.url), "utf-8");
const CLIENT_DETAIL_PAGE = readFileSync(new URL("../app/clients/[id]/page.tsx", import.meta.url), "utf-8");

test("the Touchpoint form modal never imports or renders the shared (global-search) CrmContactPicker", () => {
  // The component's own docstring explains, by name, why it deliberately
  // does NOT use CrmContactPicker -- so this checks for an actual import
  // or JSX usage, not bare text presence.
  assert.doesNotMatch(TOUCHPOINT_FORM_MODAL, /from "@\/components\/crm-contact-picker"/);
  assert.doesNotMatch(TOUCHPOINT_FORM_MODAL, /<CrmContactPicker/);
});

test("the Touchpoint form modal's Contact field is a plain <select> over an activeContacts prop", () => {
  assert.match(TOUCHPOINT_FORM_MODAL, /activeContacts/);
  assert.match(TOUCHPOINT_FORM_MODAL, /<select/);
});

test("the Touchpoint form modal excludes archived Client Contacts from the Contact picker", () => {
  assert.match(TOUCHPOINT_FORM_MODAL, /!c\.archived/);
});

test("the Touchpoint form modal supports saving with no Contact selected", () => {
  assert.match(TOUCHPOINT_FORM_MODAL, /-- none --/);
});

test("the Client detail page passes only its already-loaded (this-Client-scoped) Contacts into the Touchpoint form, not a separate global fetch", () => {
  const match = CLIENT_DETAIL_PAGE.match(/<TouchpointFormModal([\s\S]*?)\/>/);
  assert.ok(match, "expected to find <TouchpointFormModal ... />");
  assert.match(match![1], /activeContacts=\{activeClientContacts\}/);
});

test("the Client detail page's activeClientContacts is derived from the same contacts state as the Contacts section, filtered to non-archived", () => {
  assert.match(CLIENT_DETAIL_PAGE, /const activeClientContacts = contacts !== null \? contacts\.filter\(\(c\) => !c\.archived\) : \[\];/);
});

// --- Touchpoints section itself ---------------------------------------------

test("a real Touchpoints section is present with Last Contact, Log Touchpoint, and Touchpoint History", () => {
  assert.match(CLIENT_DETAIL_PAGE, />Touchpoints</);
  assert.match(CLIENT_DETAIL_PAGE, /Log Touchpoint/);
  assert.match(CLIENT_DETAIL_PAGE, />Last Contact</);
  assert.match(CLIENT_DETAIL_PAGE, />Touchpoint History</);
});

test("Last Contact is derived client-side from the loaded touchpoints list, not a separate field on Client", () => {
  assert.match(CLIENT_DETAIL_PAGE, /latestActiveTouchpoint\(touchpoints\)/);
  // Client's own interface (lib/api.ts) is never touched by this stage --
  // confirmed separately by the backend/frontend not adding a
  // last_contact-shaped field to Client anywhere in this file.
  assert.doesNotMatch(CLIENT_DETAIL_PAGE, /client\.last_contact/);
});

test("the empty Last Contact state shows the exact required copy, not a fabricated value", () => {
  assert.match(CLIENT_DETAIL_PAGE, /No contact has been logged yet\./);
});

test("Touchpoint History shows Date/Type/Activity/Contact/Contacted By/Actions columns", () => {
  const match = CLIENT_DETAIL_PAGE.match(/Touchpoint History[\s\S]*?<\/table>/);
  assert.ok(match, "expected to find the Touchpoint History table");
  for (const column of [">Date<", ">Type<", ">Activity<", ">Contact<", ">Contacted By<", ">Actions<"]) {
    assert.match(match![0], new RegExp(column));
  }
});

test("Touchpoint History falls back to an em dash for a missing note/contact snapshot/contacted_by", () => {
  assert.match(CLIENT_DETAIL_PAGE, /touchpoint\.note \|\| "—"/);
  assert.match(CLIENT_DETAIL_PAGE, /touchpoint\.contact_name \|\| "—"/);
  assert.match(CLIENT_DETAIL_PAGE, /touchpoint\.contacted_by \|\| "—"/);
});

test("no hard-delete control exists for a Touchpoint -- Archive only", () => {
  assert.doesNotMatch(CLIENT_DETAIL_PAGE, /Delete Touchpoint/);
  assert.doesNotMatch(CLIENT_DETAIL_PAGE, /deleteClientTouchpoint/);
  assert.match(CLIENT_DETAIL_PAGE, /handleArchiveTouchpoint/);
});

test("archiving a Touchpoint uses PATCH {archived: true}, never DELETE", () => {
  assert.match(CLIENT_DETAIL_PAGE, /updateClientTouchpoint\(client\.client_id, touchpoint\.touchpoint_id, \{ archived: true \}\)/);
});

// --- regression: existing sections untouched --------------------------------

test("the existing Contacts section (Add Contact / Set Primary) is still present", () => {
  assert.match(CLIENT_DETAIL_PAGE, /Add Contact/);
  assert.match(CLIENT_DETAIL_PAGE, /Set Primary/);
});

test("the existing Engagements section (Add Engagement) is still present", () => {
  assert.match(CLIENT_DETAIL_PAGE, /Add Engagement/);
  assert.match(
    CLIENT_DETAIL_PAGE,
    /\/clients\/\$\{client\.client_id\}\/engagements\/\$\{engagement\.engagement_id\}/
  );
});
