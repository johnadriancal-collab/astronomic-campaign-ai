import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// Source-level regression coverage for the Schedule tab (real per-day/
// multi-window send schedule) -- same source-level-assertion pattern as
// mail-campaign-channels.test.ts, since this project has no DOM render
// harness (see package.json's test script).

const TAB_SOURCE = readFileSync(new URL("../components/mail-campaign-schedule-tab.tsx", import.meta.url), "utf-8");
const DAY_ROW_SOURCE = readFileSync(new URL("../components/schedule-day-row.tsx", import.meta.url), "utf-8");
const BLOCK_SOURCE = readFileSync(new URL("../components/schedule-window-block.tsx", import.meta.url), "utf-8");
const PAGE_SOURCE = readFileSync(new URL("../app/manager/campaigns/mail/[id]/page.tsx", import.meta.url), "utf-8");
const API_SOURCE = readFileSync(new URL("./api.ts", import.meta.url), "utf-8");
const SCHEDULE_LIB_SOURCE = readFileSync(new URL("./schedule.ts", import.meta.url), "utf-8");
const SERVICE_SOURCE = readFileSync(new URL("../../app/services/mail_campaign_service.py", import.meta.url), "utf-8");
const API_ROUTE_SOURCE = readFileSync(new URL("../../app/api/mail.py", import.meta.url), "utf-8");

const ALL_SCHEDULE_UI_SOURCE = [TAB_SOURCE, DAY_ROW_SOURCE, BLOCK_SOURCE];

// --- No fake Trigger / capacity / quota UI ---------------------------------

test("no + Trigger control exists anywhere in the Schedule UI", () => {
  for (const source of ALL_SCHEDULE_UI_SOURCE) {
    assert.doesNotMatch(source, /trigger/i);
  }
});

test("the Schedule UI never renders a fabricated sending-capacity/quota number", () => {
  for (const source of ALL_SCHEDULE_UI_SOURCE) {
    assert.doesNotMatch(source, /\b300\b/);
    assert.doesNotMatch(source, /quota/i);
    assert.doesNotMatch(source, /capacity/i);
    assert.doesNotMatch(source, /emails? sent/i);
  }
});

test("the Schedule tab reads real MailCampaignSchedule/MailSendWindow fields only", () => {
  assert.match(API_SOURCE, /getMailCampaignSchedule/);
  assert.match(API_SOURCE, /setMailCampaignSchedule/);
  assert.match(API_SOURCE, /day_of_week/);
});

// --- Monday-Sunday, inactive days stay visible ------------------------------

test("the Schedule tab renders all seven weekdays", () => {
  assert.match(TAB_SOURCE, /ALL_DAYS\s*=\s*\[0,\s*1,\s*2,\s*3,\s*4,\s*5,\s*6\]/);
});

test("a day with zero windows renders visually inactive rather than being removed from the DOM", () => {
  assert.match(DAY_ROW_SOURCE, /isActive/);
  assert.match(DAY_ROW_SOURCE, /opacity-60/);
  // The day label and track render unconditionally -- isActive only ever
  // affects a className, never an early return / conditional unmount.
  assert.doesNotMatch(DAY_ROW_SOURCE, /if\s*\(!isActive\)\s*return/);
});

// --- Drag whole window / resize both edges ----------------------------------

test("the window block supports dragging the whole block (move) and resizing both edges", () => {
  assert.match(BLOCK_SOURCE, /"move"/);
  assert.match(BLOCK_SOURCE, /"resize-start"/);
  assert.match(BLOCK_SOURCE, /"resize-end"/);
  assert.match(BLOCK_SOURCE, /clampMoveWindow/);
  assert.match(BLOCK_SOURCE, /clampResizeStart/);
  assert.match(BLOCK_SOURCE, /clampResizeEnd/);
});

test("dragging uses pointer capture, not manual document-level listener wiring", () => {
  assert.match(BLOCK_SOURCE, /setPointerCapture/);
});

test("a read-only window block never attaches drag handlers", () => {
  assert.match(BLOCK_SOURCE, /if\s*\(readOnly\)\s*return/);
});

// --- Manual inputs synchronized with the visual timeline --------------------

test("every window has accessible manual start/end time inputs, not just the draggable block", () => {
  assert.match(DAY_ROW_SOURCE, /type="time"/);
  assert.match(DAY_ROW_SOURCE, /aria-label/);
});

test("the manual inputs and the visual timeline read/write the exact same window state", () => {
  // Both the visual block (ScheduleWindowBlock) and the manual <input
  // type="time"> rows call the SAME onWindowsChange/updateWindow path --
  // there is no separate, parallel state for the two representations.
  assert.match(DAY_ROW_SOURCE, /updateWindow\(w\.id/);
  const blockUsesUpdate = /onChange=\{\(next\) => updateWindow\(w\.id, next\)\}/.test(DAY_ROW_SOURCE);
  assert.ok(blockUsesUpdate, "ScheduleWindowBlock must call the same updateWindow() the manual inputs call");
});

// --- Mobile fallback: manual controls, no draggable timeline ---------------

test("the draggable 24h timeline is hidden below the md breakpoint", () => {
  assert.match(DAY_ROW_SOURCE, /hidden md:block/);
});

test("the manual time inputs are NOT hidden on mobile -- they're the real editing surface everywhere", () => {
  // The manual-controls wrapper must not carry the same "hidden md:block"
  // (or any "hidden ... md:") gate the timeline itself uses.
  const manualControlsSection = DAY_ROW_SOURCE.split("Accessible manual controls")[1] ?? "";
  assert.doesNotMatch(manualControlsSection.split("</div>")[0] ?? "", /hidden md:/);
});

// --- + Send time / delete window --------------------------------------------

test("+ Send time is a per-day control, not one confusing global button", () => {
  assert.match(DAY_ROW_SOURCE, /addWindow/);
  assert.match(DAY_ROW_SOURCE, /Add a send time/);
});

test("windows are removable from both the visual block and the manual input row", () => {
  assert.match(BLOCK_SOURCE, /onRemove/);
  assert.match(DAY_ROW_SOURCE, /removeWindow/);
  assert.match(DAY_ROW_SOURCE, /Remove/);
});

// --- 15-minute snapping ------------------------------------------------------

test("drag interactions snap through the shared snapping helper, not ad-hoc rounding", () => {
  assert.match(BLOCK_SOURCE, /clampMoveWindow|clampResizeStart|clampResizeEnd/);
});

// --- Lifecycle: DRAFT editable, READY/ARCHIVED read-only --------------------

test("the Schedule tab hides the Save action and shows a locked notice when not editable", () => {
  assert.match(TAB_SOURCE, /\{editable &&/);
  assert.match(TAB_SOURCE, /!editable &&/);
  assert.match(TAB_SOURCE, /locked/i);
});

test("the page wires Schedule's editable flag to the same DRAFT-only flag every other DRAFT-only tab uses", () => {
  assert.match(PAGE_SOURCE, /editable=\{editable\}/);
});

// --- Timezone always visible -------------------------------------------------

test("the timezone selector is always rendered, not conditionally hidden", () => {
  assert.match(TAB_SOURCE, /Timezone/);
  assert.match(TAB_SOURCE, /timezoneOptionsIncluding/);
});

// --- Isolation: no Apollo, no sending-engine implementation -----------------

test("the Schedule UI never references Apollo or campaign-builder", () => {
  for (const source of [...ALL_SCHEDULE_UI_SOURCE, PAGE_SOURCE]) {
    assert.doesNotMatch(source, /Apollo/);
    assert.doesNotMatch(source, /campaign-builder/);
  }
});

test("the Schedule UI never references sending-engine concepts (Gmail, workers, cron, queue)", () => {
  for (const source of ALL_SCHEDULE_UI_SOURCE) {
    assert.doesNotMatch(source, /gmail/i);
    assert.doesNotMatch(source, /\bcron\b/i);
    assert.doesNotMatch(source, /\bqueue\b/i);
    assert.doesNotMatch(source, /\bworker\b/i);
  }
});

// --- Stable window IDs across saves -----------------------------------

test("the page sends window_id for real windows, letting the backend preserve their identity", () => {
  assert.match(PAGE_SOURCE, /window_id:\s*isLocalWindowId\(w\.id\)\s*\?\s*undefined\s*:\s*w\.id/);
});

test("a local (not-yet-saved) window's id is recognizable and excluded from the save payload", () => {
  assert.match(SCHEDULE_LIB_SOURCE, /export function isLocalWindowId/);
  assert.match(SCHEDULE_LIB_SOURCE, /export function newLocalWindowId/);
  assert.match(DAY_ROW_SOURCE, /newLocalWindowId/);
});

test("the API client's schedule window type carries an optional window_id, not a required one", () => {
  assert.match(API_SOURCE, /window_id\?:\s*string/);
});

test("the backend enforces stable window ids server-side, not just as a frontend convention", () => {
  assert.match(SERVICE_SOURCE, /existing_by_id/);
  assert.match(SERVICE_SOURCE, /is not an existing send window on this campaign/);
  assert.match(SERVICE_SOURCE, /Duplicate window_id/);
});

// --- Legacy schedule fields locked once in window mode ------------------

test("the backend rejects legacy schedule PATCH fields once a campaign has explicit windows", () => {
  assert.match(SERVICE_SOURCE, /MailCampaignLegacyScheduleLockedError/);
  assert.match(SERVICE_SOURCE, /_LEGACY_SCHEDULE_PATCH_FIELDS/);
  assert.match(API_ROUTE_SOURCE, /MailCampaignLegacyScheduleLockedError/);
  assert.match(API_ROUTE_SOURCE, /status_code=409/);
});

test("timezone is included in the legacy schedule lock, not just the four shape fields", () => {
  assert.match(SERVICE_SOURCE, /_LEGACY_SCHEDULE_PATCH_FIELDS\s*=\s*\{[^}]*"timezone"/s);
});

test("daily_lead_start_limit is explicitly NOT part of the legacy schedule lock", () => {
  const constantBlock = SERVICE_SOURCE.match(/_LEGACY_SCHEDULE_PATCH_FIELDS\s*=\s*\{[^}]*\}/s)?.[0] ?? "";
  assert.doesNotMatch(constantBlock, /daily_lead_start_limit/);
});
