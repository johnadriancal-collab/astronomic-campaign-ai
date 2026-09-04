import { test } from "node:test";
import assert from "node:assert/strict";
import {
  formatLeadsToStart,
  formatTriggerTimezoneCopy,
  formatWaitingToStartCopy,
  hasZeroEnabledTriggers,
  isTriggerEditable,
  isTriggerFormClientValid,
  needsFirstTriggerConfirmation,
  showsLegacyDailyLimitNote,
  triggerFormValidationError,
  type TriggerFormState,
} from "./mail-trigger.ts";
import type { MailLeadStartTrigger } from "./api.ts";

// All 6 real MailCampaignStatus values -- mirrors app/models/mail.py's
// MailCampaignStatus exactly, same list mail.test.ts already uses.
const ALL_MAIL_CAMPAIGN_STATUSES = ["draft", "ready", "active", "paused", "completed", "archived"] as const;

function makeTrigger(overrides: Partial<MailLeadStartTrigger> = {}): MailLeadStartTrigger {
  return {
    trigger_id: "t1",
    mail_campaign_id: "c1",
    weekdays: [0, 1, 2, 3, 4],
    local_time: "09:00:00",
    leads_to_start: 20,
    enabled: true,
    created_at: "2026-09-04T00:00:00Z",
    updated_at: "2026-09-04T00:00:00Z",
    ...overrides,
  };
}

// --- isTriggerEditable: lifecycle matrix, and independence from the ---
// --- surrounding Schedule tab's own DRAFT-only `editable` flag --------

test("isTriggerEditable is true for draft/ready/active/paused, false for completed/archived", () => {
  const expected: Record<(typeof ALL_MAIL_CAMPAIGN_STATUSES)[number], boolean> = {
    draft: true,
    ready: true,
    active: true,
    paused: true,
    completed: false,
    archived: false,
  };
  for (const status of ALL_MAIL_CAMPAIGN_STATUSES) {
    assert.equal(isTriggerEditable(status), expected[status], `status=${status}`);
  }
});

test("isTriggerEditable is broader than Schedule's own DRAFT-only editable flag", () => {
  // Regression guard for the exact separation the approved design
  // requires: Triggers stay editable in ready/active/paused, while the
  // surrounding sending-hours Card (editable = status === "draft") does
  // not. If these two were ever accidentally unified, this test fails.
  const scheduleTabEditable = (status: string) => status === "draft";
  for (const status of ["ready", "active", "paused"] as const) {
    assert.equal(isTriggerEditable(status), true);
    assert.equal(scheduleTabEditable(status), false);
  }
});

// --- First-trigger confirmation -----------------------------------------

test("needsFirstTriggerConfirmation is true only while still immediate", () => {
  assert.equal(needsFirstTriggerConfirmation("immediate"), true);
  assert.equal(needsFirstTriggerConfirmation("triggered"), false);
});

// --- Zero-enabled-trigger warning ----------------------------------------

test("hasZeroEnabledTriggers requires triggered mode AND no enabled trigger", () => {
  assert.equal(hasZeroEnabledTriggers("triggered", []), true);
  assert.equal(hasZeroEnabledTriggers("triggered", [makeTrigger({ enabled: false })]), true);
  assert.equal(hasZeroEnabledTriggers("triggered", [makeTrigger({ enabled: true })]), false);
  // Never warns for an "immediate" campaign, even with zero triggers --
  // that's simply the ordinary, unremarkable starting state.
  assert.equal(hasZeroEnabledTriggers("immediate", []), false);
});

test("hasZeroEnabledTriggers is true if ANY trigger is enabled, even among several disabled", () => {
  const triggers = [makeTrigger({ trigger_id: "a", enabled: false }), makeTrigger({ trigger_id: "b", enabled: true })];
  assert.equal(hasZeroEnabledTriggers("triggered", triggers), false);
});

// --- Waiting-to-start workload copy ---------------------------------------

test("formatWaitingToStartCopy never invents a count while unavailable", () => {
  assert.equal(formatWaitingToStartCopy(null), null);
});

test("formatWaitingToStartCopy handles singular/plural correctly", () => {
  assert.equal(formatWaitingToStartCopy(0), "0 prospects waiting to start");
  assert.equal(formatWaitingToStartCopy(1), "1 prospect waiting to start");
  assert.equal(formatWaitingToStartCopy(37), "37 prospects waiting to start");
});

// --- Timezone presentation -------------------------------------------------

test("formatTriggerTimezoneCopy shows the campaign timezone when set", () => {
  assert.equal(
    formatTriggerTimezoneCopy("America/Los_Angeles"),
    "Trigger times use this campaign's timezone: America/Los_Angeles"
  );
});

test("formatTriggerTimezoneCopy is informational, not restrictive, when unset", () => {
  const copy = formatTriggerTimezoneCopy(null);
  assert.equal(copy, "Set a campaign timezone before activation so trigger times can run correctly.");
  // Must never read like a hard requirement/error -- no "must"/"required"/"cannot".
  assert.doesNotMatch(copy, /required|cannot|must\b/i);
});

// --- Leads-to-start formatting ---------------------------------------------

test("formatLeadsToStart handles singular/plural", () => {
  assert.equal(formatLeadsToStart(1), "Start 1 lead");
  assert.equal(formatLeadsToStart(20), "Start 20 leads");
});

// --- Legacy daily-limit note ------------------------------------------------

test("showsLegacyDailyLimitNote is true only once triggered, regardless of enabled-trigger count", () => {
  assert.equal(showsLegacyDailyLimitNote("immediate"), false);
  assert.equal(showsLegacyDailyLimitNote("triggered"), true);
});

// --- Client-side form validation (usability only, no collision logic) -----

function baseForm(overrides: Partial<TriggerFormState> = {}): TriggerFormState {
  return { weekdays: [0, 1, 2, 3, 4], localTime: "09:00", leadsToStart: "20", enabled: true, ...overrides };
}

test("triggerFormValidationError requires at least one weekday", () => {
  assert.equal(triggerFormValidationError(baseForm({ weekdays: [] })), "Select at least one day.");
});

test("triggerFormValidationError requires a time", () => {
  assert.equal(triggerFormValidationError(baseForm({ localTime: "" })), "Choose a time.");
});

test("triggerFormValidationError requires a positive integer leads-to-start", () => {
  assert.equal(
    triggerFormValidationError(baseForm({ leadsToStart: "" })),
    "Leads to start must be a positive integer."
  );
  assert.equal(
    triggerFormValidationError(baseForm({ leadsToStart: "0" })),
    "Leads to start must be a positive integer."
  );
  assert.equal(
    triggerFormValidationError(baseForm({ leadsToStart: "-5" })),
    "Leads to start must be a positive integer."
  );
  assert.equal(
    triggerFormValidationError(baseForm({ leadsToStart: "3.5" })),
    "Leads to start must be a positive integer."
  );
  assert.equal(triggerFormValidationError(baseForm({ leadsToStart: "1" })), null);
});

test("triggerFormValidationError accepts a fully valid form", () => {
  assert.equal(triggerFormValidationError(baseForm()), null);
});

test("isTriggerFormClientValid mirrors triggerFormValidationError", () => {
  assert.equal(isTriggerFormClientValid(baseForm()), true);
  assert.equal(isTriggerFormClientValid(baseForm({ weekdays: [] })), false);
});
