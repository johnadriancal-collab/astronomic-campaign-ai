import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// Source-level regression coverage for the Steps tab's persistent Email
// editor. Same source-level-assertion pattern as mail-campaign-schedule.
// test.ts, since this project has no DOM render harness (see package.json's
// test script).
//
// The draft Subject/Body fields live as LOCAL state inside
// mail-campaign-step-editor.tsx (not lifted to page.tsx) -- see that
// file's own docstring for why (a key-based remount, not an effect, is
// what seeds them correctly whenever the selection changes, including the
// very first time a step is shown with no user click at all). page.tsx's
// handleSaveEmail() therefore receives the current subject/body as
// ARGUMENTS from the editor's Save button, not by reading its own state.

const TIMELINE_SOURCE = readFileSync(new URL("../components/mail-campaign-steps-timeline.tsx", import.meta.url), "utf-8");
const EDITOR_SOURCE = readFileSync(new URL("../components/mail-campaign-step-editor.tsx", import.meta.url), "utf-8");
const TAB_SOURCE = readFileSync(new URL("../components/mail-campaign-steps-tab.tsx", import.meta.url), "utf-8");
const PAGE_SOURCE = readFileSync(new URL("../app/manager/campaigns/mail/[id]/page.tsx", import.meta.url), "utf-8");

function fn(name, source = PAGE_SOURCE) {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start !== -1, `function ${name} not found`);
  const rest = source.slice(start + 1);
  const nextMatch = rest.match(/\n  (async )?function /);
  return source.slice(start, nextMatch ? start + 1 + nextMatch.index : undefined);
}

// --- Selecting an Email node calls the (guarded) select handler ------------

test("clicking an Email node in the timeline calls onSelectEmail with that step", () => {
  assert.match(TIMELINE_SOURCE, /onClick=\{\(\) => onSelectEmail\(node\.step\)\}/);
});

test("handleSelectEmail sets the selection by step id -- the editor's own key-based remount seeds Subject/Body from it", () => {
  const body = fn("handleSelectEmail");
  assert.match(body, /setSelection\(\{ type: "email", stepId: step\.step_id \}\)/);
});

test("the Email editor's Subject/Body local state initializes from the selected step's own persisted values", () => {
  assert.match(EDITOR_SOURCE, /useState\(selection\?\.type === "email" \? step\?\.subject \?\? "" : ""\)/);
  assert.match(EDITOR_SOURCE, /useState\(selection\?\.type === "email" \? step\?\.body \?\? "" : ""\)/);
});

// --- Cancel: no backend write, restores saved values ------------------------

test("Email Cancel resets the local draft to the step's persisted subject/body -- purely local, no backend call", () => {
  const emailBranchStart = EDITOR_SOURCE.indexOf("// selection.type === \"email\"");
  const emailBranch = EDITOR_SOURCE.slice(emailBranchStart);
  const cancelButton = emailBranch.slice(emailBranch.lastIndexOf("Cancel") - 300, emailBranch.lastIndexOf("Cancel") + 20);
  assert.match(cancelButton, /setEmailSubject\(step\.subject\)/);
  assert.match(cancelButton, /setEmailBody\(step\.body\)/);
  assert.doesNotMatch(emailBranch, /updateMailSequenceStep/);
});

// --- Save: persists subject/body, preserves id/position/other steps --------

test("Save calls onSaveEmail with this step's id and the CURRENT local subject/body", () => {
  assert.match(EDITOR_SOURCE, /onClick=\{\(\) => onSaveEmail\(step\.step_id, emailSubject, emailBody\)\}/);
});

test("handleSaveEmail calls updateMailSequenceStep with ONLY subject/body -- never delay_days or step_number", () => {
  const body = fn("handleSaveEmail");
  assert.match(body, /updateMailSequenceStep\(campaignId, stepId, \{ subject, body \}\)/);
  assert.doesNotMatch(body, /delay_days/);
  assert.doesNotMatch(body, /step_number/);
  assert.doesNotMatch(body, /reorderMailSequenceSteps/);
});

test("Save updates only the matching step in local state, leaving every other step's object untouched", () => {
  const body = fn("handleSaveEmail");
  assert.match(body, /setSteps\(\(prev\) => prev\.map\(\(s\) => \(s\.step_id === stepId \? updated : s\)\)\)/);
  assert.doesNotMatch(body, /setSteps\(await listMailSequenceSteps/);
});

// --- Failed save: keep the form open with unsaved text, show the error -----

test("a failed Email save keeps the editor showing the unsaved local text -- page.tsx never touches the draft on failure", () => {
  const body = fn("handleSaveEmail");
  const catchBlock = body.slice(body.indexOf("} catch"), body.indexOf("} finally"));
  assert.doesNotMatch(catchBlock, /setSelection\(/);
  assert.match(catchBlock, /setSelectionError/);
});

test("double-save is prevented by an early guard and a disabled Save button while saving", () => {
  const body = fn("handleSaveEmail");
  assert.match(body, /if \(!subject\.trim\(\) \|\| !body\.trim\(\) \|\| savingSelection\) return;/);
  const emailBranchStart = EDITOR_SOURCE.indexOf("// selection.type === \"email\"");
  const saveButton = EDITOR_SOURCE.slice(EDITOR_SOURCE.indexOf("onSaveEmail(step.step_id", emailBranchStart));
  assert.match(saveButton, /disabled=\{saving \|\| !emailSubject\.trim\(\) \|\| !emailBody\.trim\(\)\}/);
});

// --- Merge variables are never transformed ----------------------------------

test("the Email editor is a plain controlled input/textarea -- {{variables}} pass through unrendered", () => {
  const emailBranchStart = EDITOR_SOURCE.indexOf("// selection.type === \"email\"");
  const emailBranch = EDITOR_SOURCE.slice(emailBranchStart);
  assert.match(emailBranch, /value=\{emailSubject\}/);
  assert.match(emailBranch, /value=\{emailBody\}/);
  assert.doesNotMatch(emailBranch, /\.replace\(/);
  assert.doesNotMatch(EDITOR_SOURCE, /render.*variable/i);
});

// --- Move/Delete remain wired -----------------------------------------------

test("Move up/down and Delete are wired to the selected Email step's own handlers, in the editor's header", () => {
  assert.match(EDITOR_SOURCE, /onClick=\{\(\) => onMoveStep\(stepIndex, -1\)\}/);
  assert.match(EDITOR_SOURCE, /onClick=\{\(\) => onMoveStep\(stepIndex, 1\)\}/);
  assert.match(EDITOR_SOURCE, /onClick=\{\(\) => onDeleteStep\(step\.step_id\)\}/);
});

test("the Steps tab composes the timeline and editor, passing selection through to both", () => {
  assert.match(TAB_SOURCE, /<MailCampaignStepsTimeline/);
  assert.match(TAB_SOURCE, /<MailCampaignStepEditor/);
  assert.match(TAB_SOURCE, /selection=\{selection\}/);
});

// --- READY/ARCHIVED stay read-only -------------------------------------------

test("`editable` is still derived from campaign.status === draft -- READY/ARCHIVED steps stay read-only", () => {
  assert.match(PAGE_SOURCE, /const editable = campaign\?\.status === "draft"/);
});

test("the Email editor disables Subject/Body and hides Save/Cancel/Move/Delete when not editable", () => {
  const emailBranchStart = EDITOR_SOURCE.indexOf("// selection.type === \"email\"");
  const emailBranch = EDITOR_SOURCE.slice(emailBranchStart);
  assert.match(emailBranch, /disabled=\{!editable\}/);
  assert.match(emailBranch, /\{editable && \(/);
});

test("the timeline's \"+ Add a new step\" control only renders when editable", () => {
  assert.match(TIMELINE_SOURCE, /\{editable && \(/);
});

test("selecting a node for read-only viewing remains possible regardless of `editable` -- only the editor's write controls are gated", () => {
  const nodeButtons = TIMELINE_SOURCE.slice(0, TIMELINE_SOURCE.indexOf("Add a new step"));
  assert.doesNotMatch(nodeButtons, /editable &&[\s\S]{0,40}onClick=\{\(\) => onSelectEmail/);
});

// --- No Apollo regression ----------------------------------------------------

test("Apollo remains completely absent from the Steps tab and its editor/timeline", () => {
  for (const source of [TIMELINE_SOURCE, EDITOR_SOURCE, TAB_SOURCE, PAGE_SOURCE]) {
    assert.doesNotMatch(source, /Apollo/);
  }
});
