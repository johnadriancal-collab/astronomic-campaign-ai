// Pure logic for the Lead-start Triggers card (Stage 5F) -- kept separate
// from components so it's unit-testable without rendering React, same
// split as lib/mail.ts itself. Deliberately contains NO duplicate-schedule
// business logic (see isTriggerFormClientValid's own docstring) -- the
// backend (Stage 5E) remains the sole authority on whether a given
// weekday/time combination collides with another enabled trigger.

import type { MailCampaignStatus, MailLeadStartTrigger } from "@/lib/api";

// Editable in DRAFT/READY/ACTIVE/PAUSED, read-only in legacy COMPLETED and
// ARCHIVED -- mirrors MailTriggerService._TRIGGER_CONFIGURABLE_STATUSES
// exactly (app/services/mail_trigger_service.py), which is BROADER than
// the surrounding Schedule tab's own sending-hours `editable` (DRAFT
// only -- see mail-campaign-schedule-tab.tsx). Deliberately a separate
// function, never derived from or compared against that other flag, so
// the two can never accidentally get wired to each other.
export function isTriggerEditable(status: MailCampaignStatus): boolean {
  return status === "draft" || status === "ready" || status === "active" || status === "paused";
}

// A one-way, backend-owned flag (MailCampaign.lead_start_mode): while
// still "immediate", no trigger has ever been successfully created for
// this campaign (creating one always flips it, and deleting/disabling
// every trigger never reverts it) -- so this alone is enough to decide
// whether the about-to-be-created trigger is genuinely the first one,
// without also needing to inspect the current trigger list.
export function needsFirstTriggerConfirmation(leadStartMode: "immediate" | "triggered"): boolean {
  return leadStartMode === "immediate";
}

// A valid, intentional state (Stage 5E): PENDING prospects simply
// accumulate until an operator enables (or creates) a trigger. Never a
// signal to revert lead_start_mode.
export function hasZeroEnabledTriggers(
  leadStartMode: "immediate" | "triggered",
  triggers: MailLeadStartTrigger[]
): boolean {
  return leadStartMode === "triggered" && !triggers.some((t) => t.enabled);
}

// `null` means "not yet loaded" (or the workload fetch failed) -- the
// caller must not render this at all rather than falling back to a
// fabricated 0, since a real 0 (genuinely zero PENDING prospects) is a
// meaningful, different state from "we don't know yet."
export function formatWaitingToStartCopy(pending: number | null): string | null {
  if (pending === null) return null;
  return `${pending} prospect${pending === 1 ? "" : "s"} waiting to start`;
}

// No Trigger-specific timezone setting exists or should exist -- Trigger
// times always use the campaign's own Schedule timezone. When unset, this
// is informational only (the backend does not reject Trigger CRUD for a
// missing timezone -- see MailTriggerService.create_trigger(), which only
// requires the campaign to be in a configurable status); Trigger
// configuration itself must remain exactly as available as the backend
// allows, never additionally restricted here.
export function formatTriggerTimezoneCopy(timezone: string | null): string {
  if (!timezone) {
    return "Set a campaign timezone before activation so trigger times can run correctly.";
  }
  return `Trigger times use this campaign's timezone: ${timezone}`;
}

export function formatLeadsToStart(leadsToStart: number): string {
  return `Start ${leadsToStart} lead${leadsToStart === 1 ? "" : "s"}`;
}

// `lead_start_mode === "triggered"` is the only condition -- shown
// regardless of how many triggers exist or are enabled, since the field
// itself is genuinely inert (Stage 5B) the moment the campaign is no
// longer "immediate", not just while triggers happen to be active.
export function showsLegacyDailyLimitNote(leadStartMode: "immediate" | "triggered"): boolean {
  return leadStartMode === "triggered";
}

export interface TriggerFormState {
  weekdays: number[];
  localTime: string; // "HH:MM" from an <input type="time">, "" if unset
  leadsToStart: string; // kept as a string for the input, matching dailyLeadStartLimit's own convention
  enabled: boolean;
}

// Basic client-side usability validation ONLY -- at least one weekday, a
// present time, and a positive-integer lead count. Deliberately does NOT
// attempt to detect a schedule collision with another enabled trigger:
// that check requires knowing every other trigger's live enabled state,
// which the backend (Stage 5E's own CAS-guarded validation) already
// authoritatively enforces on submit -- duplicating it here would risk
// the two rules silently drifting apart. A collision is always surfaced
// as the backend's own 400 error instead (see the Add/Edit modal).
export function triggerFormValidationError(form: TriggerFormState): string | null {
  if (form.weekdays.length === 0) return "Select at least one day.";
  if (!form.localTime) return "Choose a time.";
  const parsed = Number(form.leadsToStart);
  if (form.leadsToStart.trim() === "" || !Number.isInteger(parsed) || parsed < 1) {
    return "Leads to start must be a positive integer.";
  }
  return null;
}

export function isTriggerFormClientValid(form: TriggerFormState): boolean {
  return triggerFormValidationError(form) === null;
}
