// Pure helpers for the Steps tab's two-column sequence builder -- kept
// separate from the timeline/editor components so the "how does the flat
// steps[] array become a visual Email/Wait node list" logic is unit-tested
// directly, same split as lib/schedule.ts for the Schedule tab.
//
// There is no separate "Wait" backend entity -- app/models/mail.py stores
// delay_days directly on the email step it applies to (see
// MailSequenceStep's docstring). A Wait node here is purely a rendering
// choice: `{ kind: "wait", step }`'s `step` IS the email that OWNS the
// delay_days being visualized/edited -- selecting/saving a Wait node reads
// and PATCHes that same step, never a separate record. This mirrors
// QuickMail's visual layout without inventing new backend architecture.

import type { MailSequenceStep } from "@/lib/api";

export type StepTimelineNode =
  | { kind: "email"; step: MailSequenceStep }
  | { kind: "wait"; step: MailSequenceStep };

// What the right-hand editor panel is currently showing -- at most ONE of
// these at a time, replacing the previous inline-per-card editingStepId.
// "wait" carries the OWNING email step's id (see StepTimelineNode above --
// same "the wait node IS that step's delay_days" relationship). "new-email"
// has no id yet; it's the not-yet-persisted step being composed via
// "+ Add a new step", before the first successful POST gives it one.
export type StepSelection = { type: "email"; stepId: string } | { type: "wait"; stepId: string } | { type: "new-email" } | null;

/**
 * Steps are already ordered by step_number (see listMailSequenceSteps()).
 * Every step becomes an "email" node; every step EXCEPT THE FIRST is
 * preceded by a "wait" node representing that same step's own delay_days --
 * so Step 1 (step_number 1) never has a Wait before it, matching the
 * invariant that its delay_days is always 0 (see
 * MailCampaignService.add_step()/update_step()/_renumber()'s docstrings).
 * Output length is always `2 * steps.length - 1` for a non-empty input,
 * `0` for an empty one.
 */
export function buildStepTimeline(steps: MailSequenceStep[]): StepTimelineNode[] {
  const nodes: StepTimelineNode[] = [];
  steps.forEach((step, i) => {
    if (i > 0) nodes.push({ kind: "wait", step });
    nodes.push({ kind: "email", step });
  });
  return nodes;
}

/** A short, single-line preview of a step's body for the timeline's compact
 * node cards -- strips newlines (the full body already renders as
 * `whitespace-pre-wrap` in the editor once selected) and truncates at a
 * fixed character count with an ellipsis, never mid-surrogate-pair. */
export function stepBodyPreview(body: string, maxChars: number = 60): string {
  const collapsed = body.replace(/\s+/g, " ").trim();
  if (collapsed.length <= maxChars) return collapsed;
  return `${collapsed.slice(0, maxChars).trimEnd()}…`;
}
