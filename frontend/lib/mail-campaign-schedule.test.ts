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

// --- Real Trigger integration only, no fake capacity/quota UI --------------
//
// Originally this guarded against a FABRICATED "+ Trigger" control (back
// when Astronomic had no automation-rule engine at all -- see the
// deliberately-dead git history for that original assertion). Stage 5D/5E
// built the real backend and Stage 5E/5F/5F.1 (all explicitly approved)
// added the real Lead-start Triggers Card and its own timeline markers
// directly into these files -- "no mention of trigger anywhere" is now
// the WRONG invariant to guard. What still matters, updated for that: the
// Schedule tab's own trigger markers must be driven by the real,
// already-fetched trigger list (triggerMarkersForDay from lib/mail-
// trigger.ts), never a hardcoded/fabricated marker list independent of it.

test("the Schedule tab's trigger markers are derived from the real trigger list, not fabricated", () => {
  assert.match(TAB_SOURCE, /triggerMarkersForDay\(triggers,\s*day\)/);
  assert.match(TAB_SOURCE, /from "@\/lib\/mail-trigger"/);
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

// --- Hourly (not 3-hourly) timeline gridlines/labels ------------------------

test("the day row sources its hour marks/labels from lib/schedule.ts, not a locally duplicated array", () => {
  assert.match(DAY_ROW_SOURCE, /TIMELINE_HOUR_MARKS/);
  assert.match(DAY_ROW_SOURCE, /formatHourMark/);
  // The old coarse 3-hour mark array must be gone, not just supplemented.
  assert.doesNotMatch(DAY_ROW_SOURCE, /\[0,\s*3,\s*6,\s*9,\s*12,\s*15,\s*18,\s*21,\s*24\]/);
});

test("lib/schedule.ts defines exactly 25 hour marks (one per hour, midnight to the closing midnight)", () => {
  assert.match(SCHEDULE_LIB_SOURCE, /TIMELINE_HOUR_MARKS[\s\S]{0,80}length:\s*25/);
});

test("hour labels are positioned by exact left-percentage, not flexbox justify-between", () => {
  // Regression guard: space-between drifts unequal-width labels ("12AM"/
  // "12PM" vs a bare "1") away from true alignment with their gridlines --
  // see this file's own comment in schedule-day-row.tsx.
  assert.match(DAY_ROW_SOURCE, /left:\s*`\$\{\(h \/ 24\) \* 100\}%`/);
  assert.doesNotMatch(DAY_ROW_SOURCE, /justify-between/);
});

test("hourly gridlines render at every interior hour, not just every third hour", () => {
  assert.match(DAY_ROW_SOURCE, /TIMELINE_HOUR_MARKS\.slice\(1, -1\)/);
});

test("the visible hourly grid is purely presentational -- window positioning still uses raw minutes, unaffected by the label granularity", () => {
  // ScheduleWindowBlock (actual window placement) computes percentages
  // straight from minutes via the shared minutesToTimelinePercent()
  // helper (lib/schedule.ts -- Stage 5F.1 extracted this out of an inline
  // `value.start / 1440` literal so schedule-trigger-marker.tsx's own
  // marker could reuse the EXACT same formula rather than re-deriving it;
  // see that helper's own docstring), never from TIMELINE_HOUR_MARKS --
  // the label change must not touch minute-accurate positioning or the
  // 15-minute snapping constant.
  assert.match(BLOCK_SOURCE, /minutesToTimelinePercent\(value\.start\)/);
  assert.match(SCHEDULE_LIB_SOURCE, /function minutesToTimelinePercent\(minutes: number\)[\s\S]{0,80}minutes\s*\/\s*MINUTES_PER_DAY/);
  assert.doesNotMatch(BLOCK_SOURCE, /TIMELINE_HOUR_MARKS/);
  assert.match(SCHEDULE_LIB_SOURCE, /SNAP_MINUTES\s*=\s*15/);
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

test("the draggable 24h timeline is hidden below the lg breakpoint", () => {
  // Bumped from `md` (768px) to `lg` (1024px) once every hour got its own
  // label -- below ~1024px wide, "12AM"/"12PM" no longer fit their slot
  // without crowding the adjacent hour (empirically confirmed).
  assert.match(DAY_ROW_SOURCE, /hidden lg:block/);
});

test("the manual time inputs are NOT hidden on narrow/mobile screens -- they're the real editing surface everywhere", () => {
  // The manual-controls wrapper must not carry the same "hidden lg:block"
  // (or any "hidden ... lg:"/"hidden ... md:") gate the timeline itself uses.
  const manualControlsSection = DAY_ROW_SOURCE.split("Accessible manual controls")[1] ?? "";
  const section = manualControlsSection.split("</div>")[0] ?? "";
  assert.doesNotMatch(section, /hidden lg:/);
  assert.doesNotMatch(section, /hidden md:/);
});

test("hour labels are centered uniformly on every tick, including the two 12AM endpoints -- no edge-anchoring that grows into a neighbor", () => {
  assert.match(DAY_ROW_SOURCE, /-translate-x-1\/2/);
  assert.doesNotMatch(DAY_ROW_SOURCE, /translateX\(-100%\)/);
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

// --- The timeline block's x button: click-through regression -----------
//
// Root cause (see schedule-window-block.tsx's own comment): the x button
// had no onPointerDown of its own, so a pointerdown on it bubbled to the
// BLOCK's onPointerDown (mode="move"), which called setPointerCapture on
// the block -- capture then retargeted the follow-up pointerup/click away
// from the button entirely, so onClick silently never ran. These guard
// against that regressing, and against the two "don't blindly fix this"
// wrong turns (z-index-only, or removing keyboard focusability).

test("the x button has its own onPointerDown that stops propagation before the block's drag handler can see it", () => {
  const buttonBlock = BLOCK_SOURCE.slice(BLOCK_SOURCE.indexOf("<button"));
  assert.match(buttonBlock, /onPointerDown=\{\(e\) => e\.stopPropagation\(\)\}/);
});

test("the x button's onClick also stops propagation and calls the same onRemove the manual Remove link uses", () => {
  const buttonBlock = BLOCK_SOURCE.slice(BLOCK_SOURCE.indexOf("<button"));
  assert.match(buttonBlock, /e\.stopPropagation\(\)/);
  assert.match(buttonBlock, /onRemove\(\)/);
});

test("the x button never calls beginDrag -- it must not be able to initiate a drag or resize", () => {
  const buttonBlock = BLOCK_SOURCE.slice(BLOCK_SOURCE.indexOf("<button"));
  assert.doesNotMatch(buttonBlock, /beginDrag/);
});

test("the x button has a real, specific aria-label naming the window it removes", () => {
  assert.match(BLOCK_SOURCE, /aria-label=\{`Remove \$\{label\} send time`\}/);
});

test("the x button is never display:none -- it stays focusable for keyboard users, only its opacity is hover/focus-revealed", () => {
  const buttonBlock = BLOCK_SOURCE.slice(BLOCK_SOURCE.indexOf("<button"), BLOCK_SOURCE.indexOf("</button>"));
  // Check the className attribute specifically -- the button intentionally
  // contains aria-hidden="true" on its decorative inner glyph, which would
  // false-positive a bare /\bhidden\b/ check across the whole block.
  const classNameMatch = buttonBlock.match(/className="([^"]*)"/);
  assert.ok(classNameMatch, "expected the x button to have a className attribute");
  const classNames = classNameMatch![1];
  assert.doesNotMatch(classNames, /\bhidden\b/);
  assert.match(classNames, /opacity-0/);
  assert.match(classNames, /group-hover:opacity-100/);
  assert.match(classNames, /focus-visible:opacity-100/);
});

test("the x button's clickable hit target is a real button element sized larger than the old 16px box", () => {
  const buttonBlock = BLOCK_SOURCE.slice(BLOCK_SOURCE.indexOf("<button"), BLOCK_SOURCE.indexOf("</button>"));
  assert.match(buttonBlock, /h-5 w-5/);
});

test("removeWindowById (the single remove implementation) is used by the day row, not a second inline filter", () => {
  assert.match(DAY_ROW_SOURCE, /removeWindowById/);
  assert.doesNotMatch(DAY_ROW_SOURCE, /windows\.filter\(\(w\) => w\.id !== id\)/);
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

test("editable is literally false for a READY campaign -- the flag is a plain draft-only equality check, not a broader allow-list", () => {
  assert.match(PAGE_SOURCE, /const editable = campaign\?\.status === "draft";/);
});

// --- Loading/viewing Schedule can never itself reach the mutation endpoint --

test("setMailCampaignSchedule is called from exactly one place: the explicit Save Schedule handler", () => {
  const callSites = [...PAGE_SOURCE.matchAll(/setMailCampaignSchedule\(/g)];
  assert.equal(callSites.length, 1, `expected exactly 1 call site, found ${callSites.length}`);
});

test("the page's mount-time load() never references the schedule write function", () => {
  const loadMatch = PAGE_SOURCE.match(/async function load\(\)[\s\S]*?\n  \}/);
  assert.ok(loadMatch, "load() function not found");
  assert.doesNotMatch(loadMatch[0], /setMailCampaignSchedule/);
});

test("no useEffect in the page calls the schedule write function -- only a user's own Save Schedule click can", () => {
  const effectBodies = [...PAGE_SOURCE.matchAll(/useEffect\(\(\) => \{[\s\S]*?\n  \}, \[[^\]]*\]\);/g)];
  assert.ok(effectBodies.length > 0, "expected at least one useEffect in the page");
  for (const match of effectBodies) {
    assert.doesNotMatch(match[0], /setMailCampaignSchedule/);
  }
});

// --- Stale scheduleError cleared on lifecycle transitions (production bug) -

test("scheduleError is cleared on successful Mark Ready, Unlock, and Archive -- a stale error from a prior DRAFT-time Save Schedule must not survive a lifecycle transition", () => {
  for (const handlerName of ["handleMarkReady", "handleUnlock", "handleArchive"]) {
    const match = PAGE_SOURCE.match(new RegExp(`async function ${handlerName}\\(\\)[\\s\\S]*?\\n  \\}`));
    assert.ok(match, `${handlerName} not found`);
    assert.match(match[0], /setScheduleError\(null\)/);
  }
});

test("scheduleError is cleared before a fresh Save Schedule attempt, so a successful save never leaves a stale error behind either", () => {
  const match = PAGE_SOURCE.match(/async function handleSaveSchedule\(\)[\s\S]*?\n  \}/);
  assert.ok(match, "handleSaveSchedule not found");
  assert.match(match[0], /setSavingSchedule\(true\);\s*\n\s*setScheduleError\(null\);/);
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
  assert.match(PAGE_SOURCE, /window_id:\s*isUnsavedWindowId\(w\.id\)\s*\?\s*undefined\s*:\s*w\.id/);
});

test("a local (not-yet-saved) window's id is recognizable and excluded from the save payload", () => {
  assert.match(SCHEDULE_LIB_SOURCE, /export function isUnsavedWindowId/);
  assert.match(SCHEDULE_LIB_SOURCE, /export function newLocalWindowId/);
  assert.match(DAY_ROW_SOURCE, /newLocalWindowId/);
});

// --- Production bug fix: the backend's legacy-schedule fallback id is ALSO
// synthetic, and must never be echoed back as an existing window_id -------
//
// 2026-09-04 production incident: a legacy campaign's GET .../schedule
// fallback (backend's _synthesize_legacy_windows(), "legacy-<campaign_id>-
// <day>", explicitly never persisted) was round-tripped back on Save as if
// it were a real window_id -- the backend correctly 400s an id it never
// told window_store about ("... is not an existing send window on this
// campaign"), since the whole point of that synthesis is that it's
// recomputed fresh on every read, never stored. The frontend's own
// "unsaved id" classification simply didn't know about this second,
// backend-owned synthetic-id convention -- see isUnsavedWindowId's own
// docstring in lib/schedule.ts.

test("isUnsavedWindowId recognizes the backend's exact legacy-window-id shape, not just its own new- prefix", () => {
  assert.match(SCHEDULE_LIB_SOURCE, /LEGACY_WINDOW_ID_PREFIX = "legacy-"/);
  assert.match(SCHEDULE_LIB_SOURCE, /startsWith\(LOCAL_WINDOW_ID_PREFIX\) \|\| id\.startsWith\(LEGACY_WINDOW_ID_PREFIX\)/);
});

test("the backend's own legacy-window-id template matches what the frontend now recognizes as unsaved", () => {
  // f"legacy-{campaign.mail_campaign_id}-{day}" -- confirms the frontend's
  // hardcoded "legacy-" prefix isn't a guess; it mirrors the ACTUAL backend
  // template, read directly from the service source.
  assert.match(SERVICE_SOURCE, /window_id=f"legacy-\{campaign\.mail_campaign_id\}-\{day\}"/);
});

test("the backend's legacy-schedule synthesis is documented as read-only/never-persisted -- the frontend fix doesn't change that contract", () => {
  assert.match(SERVICE_SOURCE, /NEVER persisted/);
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
