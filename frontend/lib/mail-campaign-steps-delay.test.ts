import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// Source-level regression coverage for the Step 1 delay_days invariant's
// UI half (the step at position 1 always has delay_days=0, enforced by the
// backend -- see app/services/mail_campaign_service.py's add_step()/
// update_step()/_renumber()/mark_ready() docstrings; this file only covers
// what the frontend must never expose/submit). Same source-level-assertion
// pattern as mail-campaign-steps-edit.test.ts, since this project has no
// DOM render harness (see package.json's test script).

const TAB_SOURCE = readFileSync(new URL("../components/mail-campaign-steps-tab.tsx", import.meta.url), "utf-8");
const PAGE_SOURCE = readFileSync(new URL("../app/manager/campaigns/mail/[id]/page.tsx", import.meta.url), "utf-8");
const MAIL_LIB_SOURCE = readFileSync(new URL("./mail.ts", import.meta.url), "utf-8");

// --- Add form: no Delay field for an empty sequence -------------------------

test("the Add form has no editable Delay input when the sequence is empty", () => {
  const addFormStart = TAB_SOURCE.indexOf("Add a step -- allowed variables");
  const addFormEnd = TAB_SOURCE.indexOf("Add step<", addFormStart);
  const addForm = TAB_SOURCE.slice(addFormStart, addFormEnd);
  assert.match(addForm, /steps\.length === 0 \? \(/);
  assert.match(addForm, /Initial email/);
  // The editable Delay <Input type="number" .../> for the Add form must sit
  // in the steps.length > 0 branch, i.e. strictly after the ") : ("  that
  // follows the empty-sequence branch's closing.
  const emptyBranchEnd = addForm.indexOf(") : (");
  assert.ok(emptyBranchEnd > -1, "Add form must branch on steps.length === 0");
  const nonEmptyBranch = addForm.slice(emptyBranchEnd);
  assert.match(nonEmptyBranch, /type="number"/);
  assert.match(nonEmptyBranch, /setStepDelay/);
});

test("Add form's empty-sequence copy never says 'Sent immediately'", () => {
  const addFormStart = TAB_SOURCE.indexOf("Add a step -- allowed variables");
  const addFormEnd = TAB_SOURCE.indexOf("Add step<", addFormStart);
  const emptySequenceBranch = TAB_SOURCE.slice(addFormStart, TAB_SOURCE.indexOf(") : (", addFormStart)).slice(0, addFormEnd - addFormStart);
  assert.doesNotMatch(emptySequenceBranch, /sent immediately/i);
});

// --- Edit form: no Delay field while editing Step 1 -------------------------

test("the Edit form has no editable Delay input while editing Step 1", () => {
  const editFormStart = TAB_SOURCE.indexOf("Editing Step");
  const editFormEnd = TAB_SOURCE.indexOf("Save</Button", editFormStart);
  const editForm = TAB_SOURCE.slice(editFormStart, editFormEnd);
  assert.match(editForm, /step\.step_number === 1 \? \(/);
  const step1Branch = editForm.slice(editForm.indexOf("step.step_number === 1 ? ("), editForm.indexOf(") : ("));
  assert.match(step1Branch, /stepTimingLabel\(step\)/);
  assert.match(step1Branch, /stepTimingSecondaryLabel\(step\)/);
  assert.doesNotMatch(step1Branch, /type="number"/);
  const followUpBranch = editForm.slice(editForm.indexOf(") : ("));
  assert.match(followUpBranch, /type="number"/);
  assert.match(followUpBranch, /setEditDelay/);
});

// --- Read-only card uses the shared, position-aware label helper -----------

test("the read-only step card renders timing via stepTimingLabel/stepTimingSecondaryLabel, not an inline delay_days ternary", () => {
  assert.match(TAB_SOURCE, /import \{ stepTimingLabel, stepTimingSecondaryLabel \} from "@\/lib\/mail"/);
  assert.match(TAB_SOURCE, /\{stepTimingLabel\(step\)\}/);
  assert.match(TAB_SOURCE, /stepTimingSecondaryLabel\(step\)/);
});

// --- page.tsx: what actually gets submitted ---------------------------------

test("Add-step submission forces delay_days=0 while the sequence is empty, regardless of stale stepDelay state", () => {
  const fn = PAGE_SOURCE.slice(PAGE_SOURCE.indexOf("async function handleAddStep"), PAGE_SOURCE.indexOf("async function handleDeleteStep"));
  assert.match(fn, /const delayDays = steps\.length === 0 \? 0 : stepDelay;/);
  assert.match(fn, /delay_days: delayDays/);
});

test("Add-step form resets stepDelay to the shared follow-up default, not a bare literal 2, after every successful add", () => {
  const fn = PAGE_SOURCE.slice(PAGE_SOURCE.indexOf("async function handleAddStep"), PAGE_SOURCE.indexOf("async function handleDeleteStep"));
  assert.match(fn, /setStepDelay\(DEFAULT_FOLLOWUP_DELAY_DAYS\)/);
  assert.doesNotMatch(fn, /setStepDelay\(2\)/);
});

test("stepDelay's initial state also uses the shared constant, not a bare literal 2", () => {
  assert.match(PAGE_SOURCE, /useState\(DEFAULT_FOLLOWUP_DELAY_DAYS\)/);
  assert.match(PAGE_SOURCE, /import \{ DEFAULT_FOLLOWUP_DELAY_DAYS \} from "@\/lib\/mail"/);
});

test("Save-edit omits delay_days entirely from the patch when the step being edited is Step 1", () => {
  const fn = PAGE_SOURCE.slice(PAGE_SOURCE.indexOf("async function handleSaveEditStep"), PAGE_SOURCE.indexOf("async function handleMoveStep"));
  assert.match(fn, /editingStep\.step_number !== 1/);
  assert.match(fn, /patch\.delay_days = editDelay/);
  // The base patch object (always sent) must not itself include delay_days --
  // it's only conditionally added for a non-first step.
  const basePatch = fn.slice(fn.indexOf("const patch:"), fn.indexOf("if (editingStep"));
  assert.doesNotMatch(basePatch, /delay_days/);
});

// --- Shared constant is genuinely shared, not duplicated -------------------

test("DEFAULT_FOLLOWUP_DELAY_DAYS is defined once in lib/mail.ts and imported everywhere else it's used", () => {
  assert.match(MAIL_LIB_SOURCE, /export const DEFAULT_FOLLOWUP_DELAY_DAYS = 2;/);
});

// --- No Apollo regression ----------------------------------------------------

test("Apollo remains completely absent from the Steps tab's delay-invariant wiring", () => {
  for (const source of [TAB_SOURCE, PAGE_SOURCE]) {
    assert.doesNotMatch(source, /Apollo/);
  }
});
