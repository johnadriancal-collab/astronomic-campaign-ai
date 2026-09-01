// Pure formatting/labeling logic for Astronomic Mail -- kept separate from
// page components so it's unit-testable without rendering React, same
// split as lib/email-intake.ts and lib/activity.ts.

import type { MailCampaignStatus, MailEnrollmentStatus, MailSuppressionReason } from "@/lib/api";

export const MAIL_CAMPAIGN_STATUS_OPTIONS: { value: MailCampaignStatus; label: string }[] = [
  { value: "draft", label: "Draft" },
  { value: "ready", label: "Ready" },
  { value: "archived", label: "Archived" },
];

export function mailCampaignStatusLabel(status: MailCampaignStatus): string {
  return MAIL_CAMPAIGN_STATUS_OPTIONS.find((s) => s.value === status)?.label ?? status;
}

// Same "bold color for the state that matters" convention as
// email-intake.ts's statusBadgeClass.
export function mailCampaignStatusBadgeClass(status: MailCampaignStatus): string {
  switch (status) {
    case "draft":
      return "bg-secondary text-muted-foreground";
    case "ready":
      return "bg-emerald-100 text-emerald-800";
    case "archived":
      return "bg-secondary text-muted-foreground";
    default:
      return "bg-secondary text-muted-foreground";
  }
}

export function mailEnrollmentStatusLabel(status: MailEnrollmentStatus): string {
  return status === "suppressed" ? "Suppressed" : "Pending";
}

export function mailEnrollmentStatusBadgeClass(status: MailEnrollmentStatus): string {
  return status === "suppressed" ? "bg-destructive/10 text-destructive" : "bg-secondary text-muted-foreground";
}

export const MAIL_SUPPRESSION_REASON_OPTIONS: { value: MailSuppressionReason; label: string }[] = [
  { value: "manual", label: "Manual" },
  { value: "unsubscribed", label: "Unsubscribed" },
  { value: "hard_bounce", label: "Hard Bounce" },
  { value: "complaint", label: "Complaint" },
];

export function mailSuppressionReasonLabel(reason: MailSuppressionReason): string {
  return MAIL_SUPPRESSION_REASON_OPTIONS.find((r) => r.value === reason)?.label ?? reason;
}

// The CRM contact header's one suppression toggle button -- label text and
// which of the two existing, unchanged actions (suppress/unsuppress) a
// click should invoke, both driven purely by the currently-loaded status.
// `suppressed` is `null` while the status hasn't loaded yet (or the contact
// has no usable email) -- callers must not render an actionable toggle in
// that case at all, per the "no email -> no suppression toggle" rule.
export function suppressionToggleLabel(suppressed: boolean): string {
  return suppressed ? "Suppressed from Mail" : "Not suppressed from Mail";
}

export function nextSuppressionAction(suppressed: boolean): "suppress" | "unsuppress" {
  return suppressed ? "unsuppress" : "suppress";
}

// Phase B3: an explicit recipient unsubscribe (reason "unsubscribed") is
// NOT reversible through the ordinary CRM toggle -- the backend enforces
// this as a service-level guard (MailSuppressionService.unsuppress()'s
// UnsubscribeReversalNotAllowedError, mapped to a 409 in app/api/mail.py)
// and would reject the request anyway; this just means the frontend never
// offers an action the backend will refuse. `reason` is `null` for a
// never-suppressed contact -- always allowed in that case (there's nothing
// to reverse), matching nextSuppressionAction's "suppress" default.
export function canOrdinaryUnsuppress(reason: MailSuppressionReason | null): boolean {
  return reason !== "unsubscribed";
}

// 0=Monday .. 6=Sunday -- matches app/models/mail.py's convention exactly
// (Python's date.weekday()), NOT JavaScript's Date.getDay() (0=Sunday) --
// callers must never pass a raw getDay() value here.
export const WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

export const ALL_SENDING_DAYS = [0, 1, 2, 3, 4, 5, 6];

// The Create Campaign modal's default -- Monday through Friday.
export const DEFAULT_SENDING_DAYS = [0, 1, 2, 3, 4];

export function isAllSendingDaysSelected(days: number[]): boolean {
  return days.length === 7;
}

// Used by the day-pill row's single-day click -- always returns a new,
// sorted array.
export function toggleSendingDay(days: number[], day: number): number[] {
  return days.includes(day) ? days.filter((d) => d !== day) : [...days, day].sort((a, b) => a - b);
}

// "All days" behaves like a standard select-all checkbox: selects every day
// if not already all selected, clears to none if all seven are currently
// selected. Manually deselecting a single day afterward (via
// toggleSendingDay) naturally makes isAllSendingDaysSelected() false again --
// there is no separate "all days" flag to keep in sync with sendingDays.
export function toggleAllSendingDays(days: number[]): number[] {
  return isAllSendingDaysSelected(days) ? [] : [...ALL_SENDING_DAYS];
}

export function formatSendingDays(days: number[]): string {
  if (days.length === 0) return "No sending days configured";
  if (days.length === 7) return "Every day";
  return [...days].sort((a, b) => a - b).map((d) => WEEKDAY_LABELS[d] ?? "?").join(", ");
}

// `time` strings come back from the API as "HH:MM:SS" -- trims to "HH:MM"
// for display. Never throws on an unexpected shape; falls back to the raw string.
export function formatTimeOfDay(time: string | null): string {
  if (!time) return "—";
  const match = time.match(/^(\d{2}):(\d{2})/);
  return match ? `${match[1]}:${match[2]}` : time;
}

export function formatScheduleSummary(campaign: {
  sending_days: number[];
  start_time: string | null;
  end_time: string | null;
  timezone: string | null;
}): string {
  if (!campaign.sending_days.length || !campaign.start_time || !campaign.end_time || !campaign.timezone) {
    return "Schedule not fully configured";
  }
  return `${formatSendingDays(campaign.sending_days)} · ${formatTimeOfDay(campaign.start_time)}–${formatTimeOfDay(
    campaign.end_time
  )} (${campaign.timezone})`;
}

// --- Sequence step timing -----------------------------------------------
//
// The step at position 1 always has delay_days = 0 -- enforced by the
// backend on every add/edit/reorder/delete (see MailCampaignService's
// add_step()/update_step()/_renumber()/mark_ready() docstrings), never
// only a display rule here. This is the value a real follow-up step
// (Step 2+) defaults to in the Add form, and what a step demoted FROM
// position 1 is reset to on reorder -- named once so this file and the
// backend's DEFAULT_MAIL_SEQUENCE_FOLLOWUP_DELAY_DAYS don't each carry
// their own unexplained literal 2.
export const DEFAULT_FOLLOWUP_DELAY_DAYS = 2;

/** "1 day" / "5 days" -- the one place delay_days pluralization happens,
 * shared by stepTimingLabel() below and the Steps timeline's compact Wait
 * node label (see mail-campaign-steps-timeline.tsx). */
export function formatDayCount(days: number): string {
  return `${days} day${days === 1 ? "" : "s"}`;
}

// Step 1 always reads as "Initial email" here, regardless of its stored
// delay_days -- this never inspects that field for position 1. That's a
// display choice, not a claim about the data: a legacy campaign whose
// Step 1 still carries a stale nonzero delay_days (from before this
// invariant existed) already displays correctly here with no read-time
// write of its own -- it self-heals in storage the next time that step is
// legitimately edited, reordered around, or the campaign is marked Ready
// (see mark_ready()'s docstring for that lazy-normalization decision).
export function stepTimingLabel(step: { step_number: number; delay_days: number }): string {
  if (step.step_number === 1) return "Initial email";
  if (step.delay_days === 0) return "Sent immediately";
  return `${formatDayCount(step.delay_days)} after previous step`;
}

// Step 1 only -- deliberately NOT "sent immediately": actual delivery
// still has to respect the campaign's Schedule/Channels/suppression and
// (eventually) a real sending engine, so this describes sequence-position
// eligibility, not a delivery promise. Null for every other step, which
// uses stepTimingLabel()'s own text with no secondary line.
export function stepTimingSecondaryLabel(step: { step_number: number }): string | null {
  return step.step_number === 1 ? "Eligible when the lead enters the campaign" : null;
}
