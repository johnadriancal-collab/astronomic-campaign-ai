import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// Source-level regression coverage for inline Steps-tab editing (a saved
// step's subject/body/delay can be edited in place, without delete+recreate)
// -- same source-level-assertion pattern as mail-campaign-schedule.test.ts,
// since this project has no DOM render harness (see package.json's test
// script). The backend PATCH /steps/{step_id} route and update_step()
// service method already existed before this change (DRAFT-only, preserves
// step_id/step_number) -- this is a frontend-only wiring fix, so there is no
// corresponding backend source read here.

const TAB_SOURCE = readFileSync(new URL("../components/mail-campaign-steps-tab.tsx", import.meta.url), "utf-8");
const PAGE_SOURCE = readFileSync(new URL("../app/manager/campaigns/mail/[id]/page.tsx", import.meta.url), "utf-8");
const API_SOURCE = readFileSync(new URL("./api.ts", import.meta.url), "utf-8");

// --- Edit control exists and is DRAFT-gated ---------------------------------

test("a Pencil edit control is rendered per step, gated behind the same `editable` flag as move/delete", () => {
  assert.match(TAB_SOURCE, /Pencil/);
  assert.match(TAB_SOURCE, /onStartEditStep\(step\)/);
  // The Edit/Up/Down/Delete button group for a non-editing step is a single
  // block gated by one `editable &&` -- not a separately-gated Edit button
  // that could drift out of sync with the existing DRAFT-only gate.
  const editableBlock = TAB_SOURCE.slice(TAB_SOURCE.indexOf("{editable && ("), TAB_SOURCE.indexOf("</div>\n              )}\n            </div>"));
  assert.match(editableBlock, /onStartEditStep/);
  assert.match(editableBlock, /onMoveStep/);
  assert.match(editableBlock, /onDeleteStep/);
});

test("`editable` is still derived from campaign.status === draft -- READY/ARCHIVED steps stay read-only", () => {
  assert.match(PAGE_SOURCE, /const editable = campaign\?\.status === "draft"/);
});

// --- Starting an edit populates the form from the saved step ---------------

test("starting an edit seeds the edit form from the step's own current values", () => {
  const fn = PAGE_SOURCE.slice(PAGE_SOURCE.indexOf("function handleStartEditStep"), PAGE_SOURCE.indexOf("function handleCancelEditStep"));
  assert.match(fn, /setEditingStepId\(step\.step_id\)/);
  assert.match(fn, /setEditSubject\(step\.subject\)/);
  assert.match(fn, /setEditBody\(step\.body\)/);
  assert.match(fn, /setEditDelay\(step\.delay_days\)/);
});

// --- Cancel: no backend write, restores saved values ------------------------

test("Cancel makes no backend write -- it only clears local edit state", () => {
  const fn = PAGE_SOURCE.slice(PAGE_SOURCE.indexOf("function handleCancelEditStep"), PAGE_SOURCE.indexOf("async function handleSaveEditStep"));
  assert.match(fn, /setEditingStepId\(null\)/);
  assert.doesNotMatch(fn, /updateMailSequenceStep/);
  assert.doesNotMatch(fn, /await/);
});

// --- Save: persists subject/body/delay, preserves id/position/other steps --

test("Save calls updateMailSequenceStep with this step's id and the edited fields", () => {
  const fn = PAGE_SOURCE.slice(PAGE_SOURCE.indexOf("async function handleSaveEditStep"), PAGE_SOURCE.indexOf("async function handleMoveStep"));
  assert.match(fn, /updateMailSequenceStep\(campaignId, stepId, \{/);
  assert.match(fn, /subject: editSubject/);
  assert.match(fn, /body: editBody/);
  assert.match(fn, /delay_days: editDelay/);
});

test("Save's patch never includes step_number -- editing never reorders the sequence", () => {
  const fn = PAGE_SOURCE.slice(PAGE_SOURCE.indexOf("async function handleSaveEditStep"), PAGE_SOURCE.indexOf("async function handleMoveStep"));
  assert.doesNotMatch(fn, /step_number/);
  assert.doesNotMatch(fn, /reorderMailSequenceSteps/);
});

test("Save updates only the matching step in local state, leaving every other step's object untouched", () => {
  const fn = PAGE_SOURCE.slice(PAGE_SOURCE.indexOf("async function handleSaveEditStep"), PAGE_SOURCE.indexOf("async function handleMoveStep"));
  assert.match(fn, /setSteps\(\(prev\) => prev\.map\(\(s\) => \(s\.step_id === stepId \? updated : s\)\)\)/);
  // Not a full re-fetch of the list (which would still be correct, but a
  // targeted map is the more conservative, explicitly-required-behavior
  // choice: it can't accidentally reflect a concurrent reorder as an edit).
  assert.doesNotMatch(fn, /setSteps\(await listMailSequenceSteps/);
});

// --- Failed save: keep the form open with unsaved text, show the error -----

test("a failed save keeps the edit form open with the unsaved text -- it does not clear editingStepId", () => {
  const fn = PAGE_SOURCE.slice(PAGE_SOURCE.indexOf("async function handleSaveEditStep"), PAGE_SOURCE.indexOf("async function handleMoveStep"));
  const catchBlock = fn.slice(fn.indexOf("} catch"), fn.indexOf("} finally"));
  assert.doesNotMatch(catchBlock, /setEditingStepId\(null\)/);
  assert.match(catchBlock, /setStepEditError/);
});

test("double-save is prevented by an early guard and a disabled Save button while saving", () => {
  const fn = PAGE_SOURCE.slice(PAGE_SOURCE.indexOf("async function handleSaveEditStep"), PAGE_SOURCE.indexOf("async function handleMoveStep"));
  assert.match(fn, /if \(!editSubject\.trim\(\) \|\| !editBody\.trim\(\) \|\| savingStepEdit\) return;/);
  assert.match(TAB_SOURCE, /disabled=\{savingStepEdit \|\| !editSubject\.trim\(\) \|\| !editBody\.trim\(\)\}/);
});

// --- Merge variables are never transformed ----------------------------------

test("the edit form is a plain controlled input/textarea -- {{variables}} pass through unrendered", () => {
  const editFormStart = TAB_SOURCE.indexOf("Editing Step");
  const editFormBlock = TAB_SOURCE.slice(editFormStart, TAB_SOURCE.indexOf("Cancel", editFormStart));
  assert.match(editFormBlock, /value=\{editSubject\}/);
  assert.match(editFormBlock, /value=\{editBody\}/);
  assert.doesNotMatch(editFormBlock, /\.replace\(/);
  assert.doesNotMatch(TAB_SOURCE, /render.*variable/i);
});

// --- Move/Delete/Add remain wired for non-editing steps ---------------------

test("move up/down and delete remain wired to their existing handlers", () => {
  assert.match(TAB_SOURCE, /onClick=\{\(\) => onMoveStep\(i, -1\)\}/);
  assert.match(TAB_SOURCE, /onClick=\{\(\) => onMoveStep\(i, 1\)\}/);
  assert.match(TAB_SOURCE, /onClick=\{\(\) => onDeleteStep\(step\.step_id\)\}/);
});

// --- Backend capability this wiring relies on already existed --------------

test("the frontend already has an updateMailSequenceStep client calling PATCH .../steps/{id}", () => {
  assert.match(API_SOURCE, /export function updateMailSequenceStep/);
  assert.match(API_SOURCE, /method: "PATCH"/);
});

// --- No Apollo regression ----------------------------------------------------

test("Apollo remains completely absent from the Steps tab and its new edit wiring", () => {
  for (const source of [TAB_SOURCE, PAGE_SOURCE]) {
    assert.doesNotMatch(source, /Apollo/);
  }
});
