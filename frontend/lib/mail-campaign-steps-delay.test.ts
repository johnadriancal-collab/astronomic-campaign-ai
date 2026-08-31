import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// Source-level regression coverage for the Step 1 delay_days invariant's UI
// half, expressed as the sequence builder's Wait nodes (see
// lib/mail-campaign-steps.ts's buildStepTimeline() for the data model) and
// the new-email Add flow. Same source-level-assertion pattern as
// mail-campaign-schedule.test.ts, since this project has no DOM render
// harness (see package.json's test script).

const TIMELINE_SOURCE = readFileSync(new URL("../components/mail-campaign-steps-timeline.tsx", import.meta.url), "utf-8");
const EDITOR_SOURCE = readFileSync(new URL("../components/mail-campaign-step-editor.tsx", import.meta.url), "utf-8");
const PAGE_SOURCE = readFileSync(new URL("../app/manager/campaigns/mail/[id]/page.tsx", import.meta.url), "utf-8");
const MAIL_LIB_SOURCE = readFileSync(new URL("./mail.ts", import.meta.url), "utf-8");
const STEPS_LIB_SOURCE = readFileSync(new URL("./mail-campaign-steps.ts", import.meta.url), "utf-8");

function fn(name, source = PAGE_SOURCE) {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start !== -1, `function ${name} not found`);
  const rest = source.slice(start + 1);
  const nextMatch = rest.match(/\n  (async )?function /);
  return source.slice(start, nextMatch ? start + 1 + nextMatch.index : undefined);
}

// --- Step 1 never has a Wait node before it ---------------------------------

test("buildStepTimeline never places a Wait node before the first email (Step 1's delay_days is always 0)", () => {
  assert.match(STEPS_LIB_SOURCE, /if \(i > 0\) nodes\.push\(\{ kind: "wait", step \}\);/);
});

// --- Add form: no Delay field for an empty sequence -------------------------

test("the new-email editor has no editable Delay input when the sequence is empty, and shows the Initial-email copy instead", () => {
  const newEmailStart = EDITOR_SOURCE.indexOf('selection.type === "new-email"');
  const newEmailBranch = EDITOR_SOURCE.slice(newEmailStart, EDITOR_SOURCE.indexOf('if (selection.type === "wait")'));
  assert.match(newEmailBranch, /isFirstEver \? \(/);
  assert.match(newEmailBranch, /Initial email -- Eligible when the lead enters the campaign/);
  const nonFirstBranch = newEmailBranch.slice(newEmailBranch.indexOf(") : ("));
  assert.match(nonFirstBranch, /type="number"/);
  assert.match(nonFirstBranch, /setNewStepDelay/);
});

test("the new-email delay draft's local initial state is 0 for an empty sequence, else the shared follow-up default", () => {
  assert.match(EDITOR_SOURCE, /useState\(steps\.length === 0 \? 0 : DEFAULT_FOLLOWUP_DELAY_DAYS\)/);
});

test("Add-step submission (handleAddStep) forces delay_days=0 while the sequence is empty, regardless of the submitted draft value", () => {
  const body = fn("handleAddStep");
  assert.match(body, /const delayDays = steps\.length === 0 \? 0 : delayDaysInput;/);
  assert.match(body, /delay_days: delayDays/);
});

test("Add step calls onAddStep with the current local subject/body/delay draft", () => {
  assert.match(EDITOR_SOURCE, /onClick=\{\(\) => onAddStep\(emailSubject, emailBody, newStepDelay\)\}/);
});

test("the editor imports DEFAULT_FOLLOWUP_DELAY_DAYS from lib/mail rather than hardcoding it", () => {
  assert.match(EDITOR_SOURCE, /import \{ DEFAULT_FOLLOWUP_DELAY_DAYS,/);
});

// --- A newly-added step is selected immediately, so it's straight into edit-mode

test("a successful add selects the newly-created step as the active Email node", () => {
  const body = fn("handleAddStep");
  assert.match(body, /setSelection\(\{ type: "email", stepId: created\.step_id \}\)/);
});

// --- Wait node: selecting it, editing it, saving it -------------------------

test("clicking a Wait node in the timeline calls onSelectWait with the email step it represents", () => {
  assert.match(TIMELINE_SOURCE, /onClick=\{\(\) => onSelectWait\(node\.step\)\}/);
});

test("handleSelectWait sets the selection by step id -- the editor's own key-based remount seeds the Delay draft from it", () => {
  const body = fn("handleSelectWait");
  assert.match(body, /setSelection\(\{ type: "wait", stepId: step\.step_id \}\)/);
});

test("the Wait editor's local Delay state initializes from the selected step's own persisted delay_days", () => {
  assert.match(EDITOR_SOURCE, /useState\(selection\?\.type === "wait" \? step\?\.delay_days \?\? 0 : 0\)/);
});

test("the Wait editor has an editable Delay input, saved via onSaveWait with the current local value", () => {
  const waitStart = EDITOR_SOURCE.indexOf('selection.type === "wait"');
  const waitBranch = EDITOR_SOURCE.slice(waitStart, EDITOR_SOURCE.indexOf('// selection.type === "email"'));
  assert.match(waitBranch, /type="number"/);
  assert.match(waitBranch, /value=\{waitDelay\}/);
  assert.match(waitBranch, /onClick=\{\(\) => onSaveWait\(step\.step_id, waitDelay\)\}/);

  const saveBody = fn("handleSaveWaitDelay");
  assert.match(saveBody, /updateMailSequenceStep\(campaignId, stepId, \{ delay_days: delayDays \}\)/);
  assert.doesNotMatch(saveBody, /subject|body:/);
});

test("Wait Cancel resets the local Delay draft to the persisted delay_days -- purely local, no backend call", () => {
  const waitStart = EDITOR_SOURCE.indexOf('selection.type === "wait"');
  const waitBranch = EDITOR_SOURCE.slice(waitStart, EDITOR_SOURCE.indexOf('// selection.type === "email"'));
  assert.match(waitBranch, /onClick=\{\(\) => setWaitDelay\(step\.delay_days\)\}/);
  assert.doesNotMatch(waitBranch, /updateMailSequenceStep/);
});

test("a negative Wait delay is blocked client-side (min=0, disabled Save) as well as server-side", () => {
  const waitStart = EDITOR_SOURCE.indexOf('selection.type === "wait"');
  const waitBranch = EDITOR_SOURCE.slice(waitStart, EDITOR_SOURCE.indexOf('// selection.type === "email"'));
  assert.match(waitBranch, /min=\{0\}/);
  assert.match(waitBranch, /disabled=\{saving \|\| waitDelay < 0\}/);
  assert.match(fn("handleSaveWaitDelay"), /if \(delayDays < 0 \|\| savingSelection\) return;/);
});

// --- Deleting the selected step picks a sensible surviving selection -------

test("deleting the selected step picks whatever now occupies its old position, else the previous one, else nothing", () => {
  const body = fn("handleDeleteStep");
  assert.match(body, /const wasSelected = effectiveSelection !== null && effectiveSelection\.type !== "new-email" && effectiveSelection\.stepId === stepId;/);
  assert.match(body, /const deletedIndex = steps\.findIndex\(\(s\) => s\.step_id === stepId\);/);
  assert.match(body, /const survivor = remaining\[deletedIndex\] \?\? remaining\[deletedIndex - 1\] \?\? null;/);
  assert.match(body, /setSelection\(survivor \? \{ type: "email", stepId: survivor\.step_id \} : null\);/);
});

// --- The Email editor never duplicates delay editing ------------------------

test("the Email editor shows timing as read-only text and never renders an editable Delay input for it", () => {
  const emailStart = EDITOR_SOURCE.indexOf('// selection.type === "email"');
  const emailBranch = EDITOR_SOURCE.slice(emailStart);
  assert.match(emailBranch, /stepTimingLabel\(step\)/);
  assert.doesNotMatch(emailBranch, /setWaitDelay/);
});

// --- Step 1 card / read-only display -----------------------------------------

test("a Step 1 timeline node's preview never renders a raw delay number -- only its subject/body preview", () => {
  const emailNodeBranch = TIMELINE_SOURCE.slice(TIMELINE_SOURCE.indexOf('node.kind === "email"'), TIMELINE_SOURCE.indexOf('const isSelected = selection?.type === "wait"'));
  assert.doesNotMatch(emailNodeBranch, /delay_days/);
});

// --- Shared constants are genuinely shared, not duplicated ------------------

test("DEFAULT_FOLLOWUP_DELAY_DAYS and formatDayCount are each defined once in lib/mail.ts", () => {
  assert.match(MAIL_LIB_SOURCE, /export const DEFAULT_FOLLOWUP_DELAY_DAYS = 2;/);
  assert.match(MAIL_LIB_SOURCE, /export function formatDayCount/);
});

test("the timeline's Wait node label reuses formatDayCount rather than re-deriving pluralization", () => {
  assert.match(TIMELINE_SOURCE, /formatDayCount\(node\.step\.delay_days\)/);
});

// --- No Apollo regression ----------------------------------------------------

test("Apollo remains completely absent from the Steps tab's delay-invariant wiring", () => {
  for (const source of [TIMELINE_SOURCE, EDITOR_SOURCE, PAGE_SOURCE]) {
    assert.doesNotMatch(source, /Apollo/);
  }
});
