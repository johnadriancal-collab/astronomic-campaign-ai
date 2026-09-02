import { test } from "node:test";
import assert from "node:assert/strict";
import {
  DEFAULT_FOLLOWUP_DELAY_DAYS,
  DEFAULT_SENDING_DAYS,
  campaignLockedBannerDescription,
  campaignLockedBannerTitle,
  canOrdinaryUnsuppress,
  formatDayCount,
  formatScheduleSummary,
  formatSendingDays,
  formatTimeOfDay,
  isAllSendingDaysSelected,
  mailCampaignStatusBadgeClass,
  mailCampaignStatusLabel,
  mailEnrollmentStatusLabel,
  mailSuppressionReasonLabel,
  nextSuppressionAction,
  stepTimingLabel,
  stepTimingSecondaryLabel,
  suppressionToggleLabel,
  toggleAllSendingDays,
  toggleSendingDay,
} from "./mail.ts";

// All 6 real MailCampaignStatus values -- mirrors app/models/mail.py's
// MailCampaignStatus exactly (DRAFT -> READY -> ACTIVE <-> PAUSED ->
// COMPLETED, ARCHIVED terminal from any non-archived status).
const ALL_MAIL_CAMPAIGN_STATUSES = ["draft", "ready", "active", "paused", "completed", "archived"] as const;

test("mailCampaignStatusLabel maps every status", () => {
  assert.equal(mailCampaignStatusLabel("draft"), "Draft");
  assert.equal(mailCampaignStatusLabel("ready"), "Ready");
  assert.equal(mailCampaignStatusLabel("active"), "Active");
  assert.equal(mailCampaignStatusLabel("paused"), "Paused");
  assert.equal(mailCampaignStatusLabel("completed"), "Completed");
  assert.equal(mailCampaignStatusLabel("archived"), "Archived");
});

test("mailCampaignStatusBadgeClass returns a non-empty class for every status", () => {
  for (const status of ALL_MAIL_CAMPAIGN_STATUSES) {
    assert.ok(mailCampaignStatusBadgeClass(status).length > 0);
  }
});

// --- Campaign detail page's "locked" banner (all non-draft statuses) ------
//
// Regression coverage for the bug where every status past "ready" (active,
// paused, completed, archived) fell through a two-way ternary and rendered
// the literal title "Archived" -- most visibly, a genuinely COMPLETED
// campaign (one that had actually finished sending) showed an incorrect
// "Archived" banner even though its status badge correctly read
// "Completed" and it had never been archived.

test("campaignLockedBannerTitle: each non-draft status gets its own real title", () => {
  assert.equal(campaignLockedBannerTitle("ready"), "Ready -- locked for editing");
  assert.equal(campaignLockedBannerTitle("active"), "Active -- locked while sending");
  assert.equal(campaignLockedBannerTitle("paused"), "Paused -- locked while paused");
  assert.equal(campaignLockedBannerTitle("completed"), "Completed");
  assert.equal(campaignLockedBannerTitle("archived"), "Archived");
});

test("regression: COMPLETED must never render the Archived banner title", () => {
  assert.notEqual(campaignLockedBannerTitle("completed"), "Archived");
  assert.notEqual(campaignLockedBannerDescription("completed"), "This campaign is archived.");
});

test("regression: ACTIVE and PAUSED must never render the Archived banner title either", () => {
  assert.notEqual(campaignLockedBannerTitle("active"), "Archived");
  assert.notEqual(campaignLockedBannerTitle("paused"), "Archived");
});

test("campaignLockedBannerDescription: only the truly archived status says 'archived'", () => {
  for (const status of ALL_MAIL_CAMPAIGN_STATUSES) {
    if (status === "draft") continue;
    const description = campaignLockedBannerDescription(status);
    assert.ok(description.length > 0);
    if (status !== "archived") {
      assert.doesNotMatch(description.toLowerCase(), /\barchived\b/);
    }
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

// --- CRM contact header suppression toggle ------------------------------

test("suppressionToggleLabel reflects current state clearly, both directions", () => {
  assert.equal(suppressionToggleLabel(false), "Not suppressed from Mail");
  assert.equal(suppressionToggleLabel(true), "Suppressed from Mail");
});

test("nextSuppressionAction: not-suppressed -> clicking suppresses", () => {
  assert.equal(nextSuppressionAction(false), "suppress");
});

test("nextSuppressionAction: suppressed -> clicking unsuppresses", () => {
  assert.equal(nextSuppressionAction(true), "unsuppress");
});

// Phase B3: an explicit recipient unsubscribe is not reversible through
// the ordinary toggle -- see MailSuppressionService.unsuppress()'s
// UnsubscribeReversalNotAllowedError (backend, source of truth).
test("canOrdinaryUnsuppress: false only for an unsubscribed reason", () => {
  assert.equal(canOrdinaryUnsuppress("unsubscribed"), false);
  assert.equal(canOrdinaryUnsuppress("manual"), true);
  assert.equal(canOrdinaryUnsuppress("hard_bounce"), true);
  assert.equal(canOrdinaryUnsuppress("complaint"), true);
  assert.equal(canOrdinaryUnsuppress(null), true);
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

// --- Sequence step timing (Step 1 delay_days invariant) -------------------

test("DEFAULT_FOLLOWUP_DELAY_DAYS is 2", () => {
  assert.equal(DEFAULT_FOLLOWUP_DELAY_DAYS, 2);
});

test("formatDayCount pluralizes correctly, including the zero case", () => {
  assert.equal(formatDayCount(0), "0 days");
  assert.equal(formatDayCount(1), "1 day");
  assert.equal(formatDayCount(2), "2 days");
  assert.equal(formatDayCount(5), "5 days");
});

test("stepTimingLabel always reads Step 1 as 'Initial email', regardless of its stored delay_days", () => {
  assert.equal(stepTimingLabel({ step_number: 1, delay_days: 0 }), "Initial email");
  // A legacy record with a stale nonzero delay_days must still display
  // correctly -- this never inspects delay_days for position 1 at all.
  assert.equal(stepTimingLabel({ step_number: 1, delay_days: 2 }), "Initial email");
});

test("stepTimingLabel for a follow-up step reports its real delay_days", () => {
  assert.equal(stepTimingLabel({ step_number: 2, delay_days: 0 }), "Sent immediately");
  assert.equal(stepTimingLabel({ step_number: 2, delay_days: 1 }), "1 day after previous step");
  assert.equal(stepTimingLabel({ step_number: 3, delay_days: 5 }), "5 days after previous step");
});

test("stepTimingSecondaryLabel is Step-1-only explanatory copy, never a delivery promise", () => {
  assert.equal(stepTimingSecondaryLabel({ step_number: 1 }), "Eligible when the lead enters the campaign");
  assert.equal(stepTimingSecondaryLabel({ step_number: 2 }), null);
});

test("Step 1's secondary copy never claims immediate delivery", () => {
  const secondary = stepTimingSecondaryLabel({ step_number: 1 });
  assert.doesNotMatch(secondary ?? "", /sent immediately/i);
});
