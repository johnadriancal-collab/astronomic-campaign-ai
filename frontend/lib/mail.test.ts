import { test } from "node:test";
import assert from "node:assert/strict";
import {
  DEFAULT_SENDING_DAYS,
  formatScheduleSummary,
  formatSendingDays,
  formatTimeOfDay,
  isAllSendingDaysSelected,
  mailCampaignStatusBadgeClass,
  mailCampaignStatusLabel,
  mailEnrollmentStatusLabel,
  mailSuppressionReasonLabel,
  toggleAllSendingDays,
  toggleSendingDay,
} from "./mail.ts";

test("mailCampaignStatusLabel maps every Phase 1 status", () => {
  assert.equal(mailCampaignStatusLabel("draft"), "Draft");
  assert.equal(mailCampaignStatusLabel("ready"), "Ready");
  assert.equal(mailCampaignStatusLabel("archived"), "Archived");
});

test("mailCampaignStatusBadgeClass returns a non-empty class for every status", () => {
  for (const status of ["draft", "ready", "archived"] as const) {
    assert.ok(mailCampaignStatusBadgeClass(status).length > 0);
  }
});

test("mailEnrollmentStatusLabel", () => {
  assert.equal(mailEnrollmentStatusLabel("pending"), "Pending");
  assert.equal(mailEnrollmentStatusLabel("suppressed"), "Suppressed");
});

test("mailSuppressionReasonLabel maps every reason", () => {
  assert.equal(mailSuppressionReasonLabel("manual"), "Manual");
  assert.equal(mailSuppressionReasonLabel("unsubscribed"), "Unsubscribed");
  assert.equal(mailSuppressionReasonLabel("hard_bounce"), "Hard Bounce");
  assert.equal(mailSuppressionReasonLabel("complaint"), "Complaint");
});

test("formatSendingDays handles empty, every-day, and a specific subset", () => {
  assert.equal(formatSendingDays([]), "No sending days configured");
  assert.equal(formatSendingDays([0, 1, 2, 3, 4, 5, 6]), "Every day");
  assert.equal(formatSendingDays([2, 0, 4]), "Mon, Wed, Fri"); // sorted, 0=Monday
});

test("formatTimeOfDay trims seconds and handles null", () => {
  assert.equal(formatTimeOfDay("09:00:00"), "09:00");
  assert.equal(formatTimeOfDay("17:30:00"), "17:30");
  assert.equal(formatTimeOfDay(null), "—");
});

test("formatScheduleSummary reports incomplete configuration honestly", () => {
  assert.equal(
    formatScheduleSummary({ sending_days: [], start_time: null, end_time: null, timezone: null }),
    "Schedule not fully configured"
  );
  assert.equal(
    formatScheduleSummary({ sending_days: [0, 1], start_time: "09:00:00", end_time: null, timezone: "UTC" }),
    "Schedule not fully configured"
  );
});

test("formatScheduleSummary renders a complete schedule", () => {
  const summary = formatScheduleSummary({
    sending_days: [0, 1, 2, 3, 4],
    start_time: "09:00:00",
    end_time: "17:00:00",
    timezone: "America/Chicago",
  });
  assert.equal(summary, "Mon, Tue, Wed, Thu, Fri · 09:00–17:00 (America/Chicago)");
});

// --- Campaign Manager Integration Phase: Send Days picker logic ----------

test("DEFAULT_SENDING_DAYS is Monday through Friday", () => {
  assert.deepEqual(DEFAULT_SENDING_DAYS, [0, 1, 2, 3, 4]);
});

test("isAllSendingDaysSelected is true only for all seven days", () => {
  assert.equal(isAllSendingDaysSelected([]), false);
  assert.equal(isAllSendingDaysSelected([0, 1, 2, 3, 4]), false);
  assert.equal(isAllSendingDaysSelected([0, 1, 2, 3, 4, 5, 6]), true);
});

test("toggleSendingDay adds a missing day and keeps the array sorted", () => {
  assert.deepEqual(toggleSendingDay([0, 2], 1), [0, 1, 2]);
  assert.deepEqual(toggleSendingDay([], 5), [5]);
});

test("toggleSendingDay removes an already-selected day", () => {
  assert.deepEqual(toggleSendingDay([0, 1, 2], 1), [0, 2]);
});

test("deselecting a day after 'All days' was selected makes isAllSendingDaysSelected false", () => {
  const allDays = toggleAllSendingDays([]);
  assert.equal(isAllSendingDaysSelected(allDays), true);
  const afterManualDeselect = toggleSendingDay(allDays, 3);
  assert.equal(isAllSendingDaysSelected(afterManualDeselect), false);
  assert.equal(afterManualDeselect.length, 6);
});

test("toggleAllSendingDays selects all seven days when not already all selected", () => {
  assert.deepEqual(toggleAllSendingDays([0, 1]), [0, 1, 2, 3, 4, 5, 6]);
  assert.deepEqual(toggleAllSendingDays([]), [0, 1, 2, 3, 4, 5, 6]);
});

test("toggleAllSendingDays clears to none when all seven are already selected", () => {
  assert.deepEqual(toggleAllSendingDays([0, 1, 2, 3, 4, 5, 6]), []);
});
