import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// Client CRM Stage 1D's own explicit architecture rule: a Primary/
// additional Contact is always linked to an existing CrmContact via the
// shared CrmContactPicker -- never a free-text "contact person" field on
// Client, and never a duplicate/simplified person-creation form. Scanned
// directly from source, same "no component-render harness" convention as
// sidebar-nav-source.test.ts.

const CLIENT_FORM_MODAL = readFileSync(new URL("../components/client-form-modal.tsx", import.meta.url), "utf-8");
const CLIENT_CONTACT_FORM_MODAL = readFileSync(
  new URL("../components/client-contact-form-modal.tsx", import.meta.url),
  "utf-8"
);
const API_TS = readFileSync(new URL("../lib/api.ts", import.meta.url), "utf-8");

test("the New Client modal's Primary Contact field uses the shared CrmContactPicker", () => {
  assert.match(CLIENT_FORM_MODAL, /CrmContactPicker/);
});

test("the Add/Edit Contact modal's person field uses the shared CrmContactPicker", () => {
  assert.match(CLIENT_CONTACT_FORM_MODAL, /CrmContactPicker/);
});

test("Client itself has no free-text contact_person/primary_contact_name field in the API layer", () => {
  const clientInterfaceMatch = API_TS.match(/export interface Client \{([\s\S]*?)\n\}/);
  assert.ok(clientInterfaceMatch, "expected to find the Client interface");
  const body = clientInterfaceMatch![1];
  for (const forbidden of [/contact_person/, /primary_contact_name/, /\bcontact_name\b/]) {
    assert.doesNotMatch(body, forbidden);
  }
});

test("ClientContactCreateInput requires linking an existing crm_contact_id, not a free-text name", () => {
  const match = API_TS.match(/export interface ClientContactCreateInput \{([\s\S]*?)\}/);
  assert.ok(match, "expected to find the ClientContactCreateInput interface");
  const body = match![1];
  assert.match(body, /crm_contact_id:\s*string;/);
  assert.doesNotMatch(body, /first_name/);
  assert.doesNotMatch(body, /last_name/);
});
