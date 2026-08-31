import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// Source-level regression coverage for two things added on top of the
// two-column sequence builder:
//
// 1. Auto-selecting Step 1 on load WITHOUT a useEffect->setState (the
//    earlier design was rejected for tripping the react-hooks/set-state-
//    in-effect lint rule -- see page.tsx's `effectiveSelection` comment).
//    Selection is instead DERIVED during render: the user's explicit
//    choice (`selection`, nullable) if present, else Step 1, else null for
//    an empty sequence -- computed fresh every render, never written back.
//
// 2. Protecting an unsaved draft from being silently discarded by a click
//    elsewhere -- mail-campaign-steps-tab.tsx gates every selection-
//    changing/delete action behind an `isEditorDirty` check, reported by
//    the editor via a plain callback prop (never a useEffect->OWN-setState
//    call, so this doesn't reintroduce the same lint risk either).
//
// Same source-level-assertion pattern as mail-campaign-schedule.test.ts,
// since this project has no DOM render harness (see package.json's test
// script).

const TAB_SOURCE = readFileSync(new URL("../components/mail-campaign-steps-tab.tsx", import.meta.url), "utf-8");
const EDITOR_SOURCE = readFileSync(new URL("../components/mail-campaign-step-editor.tsx", import.meta.url), "utf-8");
const TIMELINE_SOURCE = readFileSync(new URL("../components/mail-campaign-steps-timeline.tsx", import.meta.url), "utf-8");
const PAGE_SOURCE = readFileSync(new URL("../app/manager/campaigns/mail/[id]/page.tsx", import.meta.url), "utf-8");

function fn(name, source = PAGE_SOURCE) {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start !== -1, `function ${name} not found`);
  const rest = source.slice(start + 1);
  const nextMatch = rest.match(/\n  (async )?function /);
  return source.slice(start, nextMatch ? start + 1 + nextMatch.index : undefined);
}

// --- Selection is derived, not effect-synced --------------------------------

test("selection auto-defaults to Step 1 via a plain derived value, never a useEffect calling this component's own setSelection", () => {
  assert.match(PAGE_SOURCE, /const effectiveSelection: StepSelection = selection \?\? \(steps\.length > 0 \? \{ type: "email", stepId: steps\[0\]\.step_id \} : null\);/);
  // The only useEffect in this file that could plausibly touch selection is
  // the initial campaign/steps load -- confirm no OTHER effect calls
  // setSelection anywhere (which is exactly the pattern that tripped the
  // lint rule the first time around).
  const setSelectionCalls = [...PAGE_SOURCE.matchAll(/setSelection\(/g)];
  assert.ok(setSelectionCalls.length > 0, "setSelection should still be called from event handlers");
  for (const match of setSelectionCalls) {
    const before = PAGE_SOURCE.slice(0, match.index);
    const enclosingEffectStart = before.lastIndexOf("useEffect(");
    const enclosingFunctionStart = before.lastIndexOf("function ");
    // Every setSelection call must be nested inside a named function
    // declaration (an event handler), not directly inside a useEffect body.
    assert.ok(enclosingFunctionStart > enclosingEffectStart, `setSelection call at index ${match.index} appears to be inside a useEffect, not a handler`);
  }
});

test("the Steps tab is passed the derived effectiveSelection, not the raw nullable override", () => {
  assert.match(PAGE_SOURCE, /<MailCampaignStepsTab[\s\S]*?selection=\{effectiveSelection\}/);
});

// --- Explicit selection survives a steps[] refetch/reorder by identity -----

test("saving an Email never resets `selection` -- the same stepId stays the effective selection after a refetch", () => {
  const body = fn("handleSaveEmail");
  assert.doesNotMatch(body, /setSelection\(/);
});

test("saving a Wait delay never resets `selection` -- the same stepId stays the effective selection after a refetch", () => {
  const body = fn("handleSaveWaitDelay");
  assert.doesNotMatch(body, /setSelection\(/);
});

test("reordering (handleMoveStep) never touches selection -- identity survives a position change untouched", () => {
  const body = fn("handleMoveStep");
  assert.doesNotMatch(body, /setSelection/);
});

// --- Adding a step selects it ------------------------------------------------

test("a successful add selects the newly-created step by id", () => {
  const body = fn("handleAddStep");
  assert.match(body, /setSelection\(\{ type: "email", stepId: created\.step_id \}\)/);
});

// --- Dirty-state detection ---------------------------------------------------

test("Email dirtiness compares the local draft against the selected step's persisted subject/body", () => {
  assert.match(
    EDITOR_SOURCE,
    /selection\?\.type === "email"\s*\n\s*\? step !== null && \(emailSubject !== step\.subject \|\| emailBody !== step\.body\)/
  );
});

test("Wait dirtiness compares the local draft against the selected step's persisted delay_days", () => {
  assert.match(EDITOR_SOURCE, /\? step !== null && waitDelay !== step\.delay_days/);
});

test("a new-unsaved-Email's dirtiness is based on differing from its own blank/default initial state, not a persisted step", () => {
  assert.match(
    EDITOR_SOURCE,
    /emailSubject\.trim\(\) !== "" \|\| emailBody\.trim\(\) !== "" \|\| \(steps\.length > 0 && newStepDelay !== DEFAULT_FOLLOWUP_DELAY_DAYS\)/
  );
});

test("dirtiness is reported to the parent via a plain callback prop from an effect, never this component's own setState", () => {
  assert.match(EDITOR_SOURCE, /useEffect\(\(\) => \{\s*onDirtyChange\(isDirty\);/);
  // onDirtyChange is a prop (typed as a plain function), not a useState
  // setter declared in this component -- confirm no local `isDirty` state
  // setter exists that the effect could instead be accused of calling.
  assert.doesNotMatch(EDITOR_SOURCE, /const \[isDirty, setIsDirty\]/);
});

// --- The Tab gates selection-changing/delete actions behind the dirty check -

test("the Tab wraps onSelectEmail/onSelectWait/onStartAddStep/onDeleteStep in a dirty-check gate before invoking them", () => {
  assert.match(TAB_SOURCE, /function attempt\(action: \(\) => void\) \{/);
  assert.match(TAB_SOURCE, /if \(isEditorDirty\) \{/);
  assert.match(TAB_SOURCE, /const guardedSelectEmail = \(step: MailSequenceStep\) => attempt\(\(\) => onSelectEmail\(step\)\);/);
  assert.match(TAB_SOURCE, /const guardedSelectWait = \(step: MailSequenceStep\) => attempt\(\(\) => onSelectWait\(step\)\);/);
  assert.match(TAB_SOURCE, /const guardedStartAddStep = \(\) => attempt\(\(\) => onStartAddStep\(\)\);/);
  assert.match(TAB_SOURCE, /const guardedDeleteStep = \(stepId: string\) => attempt\(\(\) => onDeleteStep\(stepId\)\);/);
});

test("the timeline receives the GUARDED select/add handlers, not the raw page.tsx ones directly", () => {
  const timelineElement = TAB_SOURCE.slice(TAB_SOURCE.indexOf("<MailCampaignStepsTimeline"), TAB_SOURCE.indexOf("<MailCampaignStepEditor"));
  assert.match(timelineElement, /onSelectEmail=\{guardedSelectEmail\}/);
  assert.match(timelineElement, /onSelectWait=\{guardedSelectWait\}/);
  assert.match(timelineElement, /onStartAddStep=\{guardedStartAddStep\}/);
});

test("the editor receives the GUARDED delete handler but the UNGATED move handler -- reorder never prompts", () => {
  const editorElement = TAB_SOURCE.slice(TAB_SOURCE.indexOf("<MailCampaignStepEditor"));
  assert.match(editorElement, /onDeleteStep=\{guardedDeleteStep\}/);
  assert.match(editorElement, /onMoveStep=\{onMoveStep\}/);
});

test("a clean (non-dirty) editor lets an action through immediately, with no dialog involved", () => {
  const attemptFn = TAB_SOURCE.slice(TAB_SOURCE.indexOf("function attempt("), TAB_SOURCE.indexOf("const guardedSelectEmail"));
  assert.match(attemptFn, /\} else \{\s*action\(\);\s*\}/);
});

// --- The editor remounts (fresh draft) on every selection change ------------

test("the editor is keyed by selection identity so React remounts (and re-seeds local state) on every selection change", () => {
  assert.match(TAB_SOURCE, /const editorKey = selection === null \? "empty" : selection\.type === "new-email" \? "new-email" : `\$\{selection\.type\}-\$\{selection\.stepId\}`;/);
  assert.match(TAB_SOURCE, /<MailCampaignStepEditor\s*\n\s*key=\{editorKey\}/);
});

// --- The discard-confirmation dialog itself ---------------------------------

test("the dialog uses this project's existing Dialog primitive, not a bare window.confirm()", () => {
  assert.match(TAB_SOURCE, /from "@\/components\/ui\/dialog"/);
  assert.doesNotMatch(TAB_SOURCE, /window\.confirm/);
});

test("the dialog's copy matches the requested wording exactly", () => {
  assert.match(TAB_SOURCE, /<DialogTitle>Discard unsaved changes\?<\/DialogTitle>/);
  assert.match(TAB_SOURCE, /You have changes that haven&apos;t been saved\./);
  assert.match(TAB_SOURCE, />Keep editing<\/Button>/);
  assert.match(TAB_SOURCE, />\s*Discard changes\s*<\/Button>/);
});

test("the dialog is open exactly when there's a pending (blocked) action", () => {
  assert.match(TAB_SOURCE, /<Dialog open=\{pendingAction !== null\}/);
});

test("\"Keep editing\" clears the pending action without ever invoking it -- Discard runs it then clears it", () => {
  const dialogBlock = TAB_SOURCE.slice(TAB_SOURCE.indexOf("<Dialog open="));
  // "Keep editing" is a DialogClose -- closing sets pendingAction back to
  // null via onOpenChange, but never calls the stashed action.
  assert.match(dialogBlock, /onOpenChange=\{\(open\) => \{ if \(!open\) setPendingAction\(null\); \}\}/);
  assert.match(dialogBlock, /render=\{<Button type="button" variant="outline">Keep editing<\/Button>\}/);
  // "Discard changes" is a real onClick that invokes the stashed action.
  const discardButton = dialogBlock.slice(dialogBlock.indexOf("Discard changes") - 200);
  assert.match(discardButton, /onClick=\{\(\) => \{\s*pendingAction\?\.\(\);\s*setPendingAction\(null\);/);
});

// --- No Apollo regression ----------------------------------------------------

test("Apollo remains completely absent from the selection/dirty-gate wiring", () => {
  for (const source of [TAB_SOURCE, EDITOR_SOURCE, TIMELINE_SOURCE, PAGE_SOURCE]) {
    assert.doesNotMatch(source, /Apollo/);
  }
});
