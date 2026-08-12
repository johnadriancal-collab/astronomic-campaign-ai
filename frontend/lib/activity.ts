// Pure formatting/labeling logic for the CRM Activity Log feed, kept
// separate from the page component so it's unit-testable without rendering
// React or touching the DOM -- same split as lib/csv-export.ts.

import type { ActivityCategory, ActivityEvent } from "@/lib/api";

export const CATEGORY_OPTIONS: { value: ActivityCategory | "all"; label: string }[] = [
  { value: "all", label: "All" },
  { value: "itf", label: "ITF" },
  { value: "contacts", label: "Contacts" },
  { value: "imports", label: "Imports" },
  { value: "lists", label: "Lists" },
  { value: "exports", label: "Exports" },
  { value: "campaigns", label: "Campaigns" },
  { value: "email_intake", label: "Email Intake" },
  { value: "errors", label: "Errors" },
];

// Known event_type -> short, human title for the feed's bold headline.
// Anything not listed here falls back to titleCaseEventType() below, so a
// future event_type added on the backend still renders sensibly with zero
// changes required here.
const EVENT_TITLES: Record<string, string> = {
  "itf.submission_received": "ITF submission received",
  "itf.contact_created": "Contact created",
  "itf.contact_updated": "Contact updated",
  "itf.processing_failed": "ITF processing failed",
  "contact.created": "Contact created",
  "contact.updated": "Contact updated",
  "contact.archived": "Contact archived",
  "contact.unarchived": "Contact unarchived",
  "import.completed": "CSV import completed",
  "list.created": "List created",
  "list.updated": "List updated",
  "list.deleted": "List deleted",
  "list.contacts_added": "List updated",
  "list.contacts_removed": "List updated",
  "contacts.exported": "Contacts exported",
  "campaign.created": "Campaign created",
  "campaign.build_completed": "Campaign build completed",
  "campaign.build_failed": "Campaign build failed",
  "campaign.activated": "Campaign activated",
  "campaign.paused": "Campaign paused",
  "campaign.sync_completed": "Campaign sync completed",
  "email_intake.proposal_created": "Email intake proposal created",
  "email_intake.needs_match": "Email intake needs match",
  "email_intake.approved": "Email intake approved",
  "email_intake.rejected": "Email intake rejected",
  "email_intake.processing_failed": "Email intake processing failed",
};

export function titleCaseEventType(eventType: string): string {
  const [, action] = eventType.split(".");
  const words = (action ?? eventType).split("_");
  return words.map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(" ");
}

export function eventTitle(event: ActivityEvent): string {
  return EVENT_TITLES[event.event_type] ?? titleCaseEventType(event.event_type);
}

export function categoryLabel(category: ActivityCategory): string {
  return CATEGORY_OPTIONS.find((c) => c.value === category)?.label ?? category;
}

// Sensible human-readable timestamp -- today shows a bare time ("10:10 AM"),
// anything older shows a short date + time ("Aug 10, 10:10 AM"). The precise
// stored timestamp is never discarded -- callers should still render
// event.created_at itself as a `title`/tooltip attribute alongside this.
export function formatEventTimestamp(iso: string, now: Date = new Date()): string {
  const date = new Date(iso);
  const time = date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  const isToday =
    date.getFullYear() === now.getFullYear() && date.getMonth() === now.getMonth() && date.getDate() === now.getDate();
  if (isToday) return time;
  const datePart = date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  return `${datePart}, ${time}`;
}

export interface EntityLink {
  href: string;
  label: string;
}

// Never returns a link for an event whose own event_type says the entity no
// longer exists (any "*.deleted" event) -- the summary text already states
// the deletion, and linking would either be dead or, worse, misleadingly
// imply the entity is still there. For every other event, links optimistically
// to the entity's current page -- if it was deleted LATER (e.g. a list
// created then deleted afterward), the destination page's own not-found
// state handles that gracefully, same as any stale deep link.
export function entityLink(event: ActivityEvent): EntityLink | null {
  if (event.event_type.endsWith(".deleted")) return null;
  if (!event.entity_id || !event.entity_type) return null;
  switch (event.entity_type) {
    case "contact":
      return { href: `/crm/${event.entity_id}`, label: "View contact" };
    case "list":
      return { href: `/crm/lists/${event.entity_id}`, label: "View list" };
    case "campaign":
      return { href: `/manager/campaigns/${event.entity_id}`, label: "View campaign" };
    case "email_intake_item":
      return { href: `/crm/settings/email-intake/${event.entity_id}`, label: "View email intake item" };
    default:
      return null;
  }
}

// Simple, readable key/value lines for an event's details drawer -- never
// dumps raw metadata JSON into the normal feed. Only includes metadata keys
// that are actually present and meaningful; a blank/empty value is omitted
// rather than shown as "N/A".
export function detailLines(event: ActivityEvent): { label: string; value: string }[] {
  const lines: { label: string; value: string }[] = [];
  if (event.entity_type) lines.push({ label: "Entity type", value: titleCaseEventType(event.entity_type) });
  if (event.entity_id) lines.push({ label: "Entity ID", value: event.entity_id });
  for (const [key, value] of Object.entries(event.metadata)) {
    if (value === null || value === undefined || value === "") continue;
    const label = key
      .split("_")
      .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
      .join(" ");
    lines.push({ label, value: typeof value === "object" ? JSON.stringify(value) : String(value) });
  }
  return lines;
}
