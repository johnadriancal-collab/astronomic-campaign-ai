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

// 0=Monday .. 6=Sunday -- matches app/models/mail.py's convention exactly
// (Python's date.weekday()), NOT JavaScript's Date.getDay() (0=Sunday) --
// callers must never pass a raw getDay() value here.
export const WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

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
