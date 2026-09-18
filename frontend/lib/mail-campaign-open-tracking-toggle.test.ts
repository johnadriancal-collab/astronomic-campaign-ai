import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// Open tracking campaign-level toggle (2026-09-18) -- source-level
// assertions (no DOM render harness in this project -- see package.json's
// test script), same pattern as mail-campaign-dashboard.test.ts. Real
// send-path gating (pixel injected iff campaign.open_tracking_enabled) is
// unit-tested directly against MailSendingService in
// tests/test_mail_sending_service.py; these tests verify the frontend
// wires the SAME setting in as an editable, DRAFT-only toggle -- default
// OFF, never pre-checked, never silently enabled for an existing campaign.

const SETTINGS_TAB_SOURCE = readFileSync(new URL("../components/mail-campaign-settings-tab.tsx", import.meta.url), "utf-8");
const PAGE_SOURCE = readFileSync(new URL("../app/manager/campaigns/mail/[id]/page.tsx", import.meta.url), "utf-8");
const API_SOURCE = readFileSync(new URL("./api.ts", import.meta.url), "utf-8");
const MODEL_SOURCE = readFileSync(new URL("../../app/models/mail.py", import.meta.url), "utf-8");
const SERVICE_SOURCE = readFileSync(new URL("../../app/services/mail_campaign_service.py", import.meta.url), "utf-8");

// --- Settings tab component --------------------------------------------------

test("the settings tab renders a 'Track email opens' switch, wired to openTrackingEnabled/setOpenTrackingEnabled props", () => {
  assert.match(SETTINGS_TAB_SOURCE, /Track email opens/);
  assert.match(SETTINGS_TAB_SOURCE, /checked=\{openTrackingEnabled\}/);
  assert.match(SETTINGS_TAB_SOURCE, /setOpenTrackingEnabled\(Boolean\(v\)\)/);
});

test("the toggle is locked exactly like every other DRAFT-only preference field -- disabled={!editable}", () => {
  const toggleBlockStart = SETTINGS_TAB_SOURCE.indexOf("Track email opens");
  const toggleBlock = SETTINGS_TAB_SOURCE.slice(toggleBlockStart, toggleBlockStart + 700);
  assert.match(toggleBlock, /disabled=\{!editable\}/);
});

test("the toggle's own copy discloses the approximate-tracking limitation", () => {
  const toggleBlockStart = SETTINGS_TAB_SOURCE.indexOf("Track email opens");
  const toggleBlock = SETTINGS_TAB_SOURCE.slice(toggleBlockStart, toggleBlockStart + 700);
  assert.match(toggleBlock, /invisible tracking image to measure approximate opens/);
  assert.match(toggleBlock, /some email clients may preload or block images/);
});

test("the toggle never auto-checks -- it is a controlled Switch driven entirely by props, no hardcoded checked=true/defaultChecked", () => {
  const toggleBlockStart = SETTINGS_TAB_SOURCE.indexOf("Track email opens");
  const toggleBlock = SETTINGS_TAB_SOURCE.slice(toggleBlockStart, toggleBlockStart + 700);
  assert.doesNotMatch(toggleBlock, /checked=\{true\}|defaultChecked/);
});

// --- Campaign detail page wiring ---------------------------------------------

test("the page seeds openTrackingEnabled from the campaign's own real, persisted field on load", () => {
  assert.match(PAGE_SOURCE, /const \[openTrackingEnabled, setOpenTrackingEnabled\] = useState\(false\)/);
  assert.match(PAGE_SOURCE, /setOpenTrackingEnabled\(c\.open_tracking_enabled\)/);
});

test("saving settings sends open_tracking_enabled through the same PATCH as every other preference field", () => {
  const handlerStart = PAGE_SOURCE.indexOf("async function handleSaveSettings");
  const handlerBlock = PAGE_SOURCE.slice(handlerStart, handlerStart + 800);
  assert.match(handlerBlock, /open_tracking_enabled: openTrackingEnabled/);
});

test("the settings tab call site passes openTrackingEnabled/setOpenTrackingEnabled through", () => {
  const callSiteStart = PAGE_SOURCE.indexOf("<MailCampaignSettingsTab");
  const callSiteBlock = PAGE_SOURCE.slice(callSiteStart, PAGE_SOURCE.indexOf("/>", callSiteStart));
  assert.match(callSiteBlock, /openTrackingEnabled=\{openTrackingEnabled\}/);
  assert.match(callSiteBlock, /setOpenTrackingEnabled=\{setOpenTrackingEnabled\}/);
});

// --- API client ---------------------------------------------------------------

test("MailCampaign (api.ts) carries the real, persisted open_tracking_enabled field", () => {
  const typeBlock = API_SOURCE.slice(API_SOURCE.indexOf("export interface MailCampaign {"), API_SOURCE.indexOf("export interface MailCampaign {") + 1200);
  assert.match(typeBlock, /open_tracking_enabled: boolean/);
});

// --- Backend model / lifecycle lock -------------------------------------------

test("MailCampaign (backend model) defaults open_tracking_enabled to False -- opt-in, never opt-out", () => {
  assert.match(MODEL_SOURCE, /open_tracking_enabled: bool = False/);
});

test("open_tracking_enabled is patchable through the exact same DRAFT-only allow-list as every other preference field", () => {
  const patchFieldsBlock = SERVICE_SOURCE.slice(
    SERVICE_SOURCE.indexOf("_CAMPAIGN_PATCH_FIELDS = {"),
    SERVICE_SOURCE.indexOf("}", SERVICE_SOURCE.indexOf("_CAMPAIGN_PATCH_FIELDS = {"))
  );
  assert.match(patchFieldsBlock, /"open_tracking_enabled"/);
});

test("no bespoke/second lock exists for open_tracking_enabled -- it relies on the same whole-method _require_draft() gate", () => {
  // The field appears in the allow-list (checked above) but NOT in the
  // stricter legacy-schedule lock set, which would be a sign of an
  // accidentally-duplicated, second locking mechanism.
  const legacyScheduleFieldsBlock = SERVICE_SOURCE.slice(
    SERVICE_SOURCE.indexOf("_LEGACY_SCHEDULE_PATCH_FIELDS = {"),
    SERVICE_SOURCE.indexOf("}", SERVICE_SOURCE.indexOf("_LEGACY_SCHEDULE_PATCH_FIELDS = {"))
  );
  assert.doesNotMatch(legacyScheduleFieldsBlock, /open_tracking_enabled/);
});
