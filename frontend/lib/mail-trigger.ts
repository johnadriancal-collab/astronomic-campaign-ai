// Pure logic for the Lead-start Triggers card (Stage 5F) -- kept separate
// from components so it's unit-testable without rendering React, same
// split as lib/mail.ts itself. Deliberately contains NO duplicate-schedule
// business logic (see isTriggerFormClientValid's own docstring) -- the
// backend (Stage 5E) remains the sole authority on whether a given
// weekday/time combination collides with another enabled trigger.

import type { MailCampaignStatus, MailLeadStartTrigger } from "@/lib/api";
// Relative WITH an explicit ".ts" extension, not "@/lib/schedule" -- this
// file is imported transitively by lib/mail-trigger.test.ts, which
// node --test resolves via Node's own native ESM loader (no webpack/
// Next.js "@/..." alias, and no extension-less resolution either -- both
// were tried and both throw ERR_MODULE_NOT_FOUND under this Node
// version). Every other cross-lib-file import in this codebase is a
// type-only "./api" import, erased entirely before Node ever tries to
// resolve it -- this is the first one that needs a real runtime VALUE,
// so it's the first to actually exercise (and need) Node's own explicit-
// extension relative-TS resolution.
import { formatMinutesOfDay, minutesFromTimeString, minutesToTimelinePercent } from "./schedule.ts";

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

// --- Stage 5F.1: trigger markers on the Schedule timeline -----------------
//
// A pure VISUALIZATION of already-existing, already-fetched triggers --
// never a second scheduling mechanism, never written back anywhere. Reuses
// minutesToTimelinePercent (lib/schedule.ts) -- the SAME formula
// schedule-window-block.tsx's own send-window bar positions itself with --
// so a marker and a window drawn for the same instant land at the exact
// same pixel; positioning math is never independently re-derived here.

export interface TriggerMarker {
  triggerId: string;
  leftPct: number; // matches ScheduleWindowBlock's own `left: X%` convention
  detail: string; // e.g. "8:00 AM · Start 20 leads" -- the tooltip's second line
}

/** Every ENABLED trigger applicable to campaign-local weekday `day`
 * (0=Monday..6=Sunday, same convention as MailLeadStartTrigger.weekdays
 * and ScheduleDayRow's own `day` prop) -- a disabled trigger produces no
 * marker at all (still fully visible in the management table below,
 * this is a timeline-only omission). Multiple enabled triggers on the
 * same day/near-identical time each produce their own independent
 * marker -- never deduplicated or merged, matching MailLeadStartTrigger's
 * own "nothing here deduplicates overlapping triggers" backend design. */
export function triggerMarkersForDay(triggers: MailLeadStartTrigger[], day: number): TriggerMarker[] {
  return triggers
    .filter((t) => t.enabled && t.weekdays.includes(day))
    .map((t) => {
      const minutes = minutesFromTimeString(t.local_time);
      return {
        triggerId: t.trigger_id,
        leftPct: minutesToTimelinePercent(minutes),
        detail: `${formatMinutesOfDay(minutes)} · ${formatLeadsToStart(t.leads_to_start)}`,
      };
    });
}
