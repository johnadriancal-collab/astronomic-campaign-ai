// Pure formatting/labeling logic for the Email Intake review queue, kept
// separate from the page components so it's unit-testable without
// rendering React or touching the DOM -- same split as lib/activity.ts.

import type { EmailCrmFieldChange, EmailIntakeItem, EmailIntakeStatus } from "@/lib/api";

export const STATUS_OPTIONS: { value: EmailIntakeStatus | "all"; label: string }[] = [
  { value: "all", label: "All" },
  { value: "pending_review", label: "Pending" },
  { value: "needs_match", label: "Needs Match" },
  { value: "approved", label: "Approved" },
  { value: "rejected", label: "Rejected" },
  { value: "error", label: "Error" },
];

export function statusLabel(status: EmailIntakeStatus): string {
  return STATUS_OPTIONS.find((s) => s.value === status)?.label ?? status;
}

// Same "bold color for the state that matters" convention as
// campaign-status-badge.tsx -- Needs Match and Error are the two states
// that need a reviewer's attention, so they get the most visually
// distinct treatment.
export function statusBadgeClass(status: EmailIntakeStatus): string {
  switch (status) {
    case "pending_review":
      return "bg-amber-100 text-amber-800";
    case "needs_match":
      return "bg-orange-100 text-orange-800";
    case "approved":
      return "bg-emerald-100 text-emerald-800";
    case "rejected":
      return "bg-secondary text-muted-foreground";
    case "error":
      return "bg-destructive/10 text-destructive";
    default:
      return "bg-secondary text-muted-foreground";
  }
}

// Renders a field value for the before/after diff -- never throws on
// null/empty-list/plain-string, the three shapes EmailCrmFieldChange
// values actually take.
export function formatFieldValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (Array.isArray(value)) return value.length > 0 ? value.join(", ") : "—";
  const text = String(value).trim();
  return text || "—";
}

// True when a proposal has nothing to show -- the audit's explicit
// adjustment: this is a normal, valid outcome, never an error state.
export function hasNoProposedChanges(item: EmailIntakeItem): boolean {
  return item.proposal.length === 0;
}

export function senderDisplayName(sender: string): string {
  const match = sender.match(/^\s*"?([^"<]*)"?\s*<([^>]+)>\s*$/);
  return match ? (match[1].trim() || match[2].trim()) : sender.trim();
}

export function senderEmail(sender: string): string {
  const match = sender.match(/^\s*"?([^"<]*)"?\s*<([^>]+)>\s*$/);
  return match ? match[2].trim() : sender.trim();
}

// Sensible default selection when a review page first loads a proposal --
// every proposed change starts checked, matching "uncheck what you don't
// want" rather than "check what you do want".
export function defaultSelectedFieldKeys(proposal: EmailCrmFieldChange[]): Set<string> {
  return new Set(proposal.map((c) => c.field_key));
}
