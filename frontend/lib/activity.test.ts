import { test } from "node:test";
import assert from "node:assert/strict";
import {
  categoryLabel,
  detailLines,
  entityLink,
  eventTitle,
  formatEventTimestamp,
  titleCaseEventType,
} from "./activity.ts";
import type { ActivityEvent } from "./api.ts";

function makeEvent(overrides: Partial<ActivityEvent> = {}): ActivityEvent {
  return {
    event_id: "e1",
    event_type: "contact.created",
    category: "contacts",
    created_at: "2026-08-12T10:10:25Z",
    source: "manual_crm",
    actor: null,
    entity_type: "contact",
    entity_id: "c1",
    entity_name: "Ada Lovelace",
    summary: "Ada Lovelace was manually created in the CRM.",
    metadata: {},
    ...overrides,
  };
}

test("titleCaseEventType derives a readable title from the action half of event_type", () => {
  assert.equal(titleCaseEventType("list.contacts_added"), "Contacts Added");
  assert.equal(titleCaseEventType("campaign.build_failed"), "Build Failed");
});

test("eventTitle uses the known-title table for recognized event types", () => {
  assert.equal(eventTitle(makeEvent({ event_type: "itf.submission_received" })), "ITF submission received");
  assert.equal(eventTitle(makeEvent({ event_type: "list.contacts_added" })), "List updated");
});

test("eventTitle falls back to a derived title for an unrecognized event type", () => {
  assert.equal(eventTitle(makeEvent({ event_type: "widget.frobnicated" })), "Frobnicated");
});

test("categoryLabel maps every category to its filter label", () => {
  assert.equal(categoryLabel("itf"), "ITF");
  assert.equal(categoryLabel("errors"), "Errors");
});

test("formatEventTimestamp shows a bare time for today", () => {
  // "now" anchored to the exact same instant as the event -- guaranteed to
  // be the same local calendar day in any timezone, no hardcoded offset assumptions.
  const iso = "2026-08-12T10:10:25Z";
  const now = new Date(iso);
  const result = formatEventTimestamp(iso, now);
  assert.ok(!result.includes("2026") && !/[A-Za-z]{3} \d/.test(result), `expected a bare time, got "${result}"`);
});

test("formatEventTimestamp includes the date for a past day", () => {
  const iso = "2026-08-12T10:10:25Z";
  // 25 hours later is always the next local calendar day, regardless of timezone.
  const now = new Date(new Date(iso).getTime() + 25 * 60 * 60 * 1000);
  const result = formatEventTimestamp(iso, now);
  assert.ok(/[A-Za-z]{3} \d/.test(result), `expected a date included, got "${result}"`);
});

test("entityLink returns a contact link for a contact entity", () => {
  const link = entityLink(makeEvent({ entity_type: "contact", entity_id: "c1" }));
  assert.deepEqual(link, { href: "/crm/c1", label: "View contact" });
});

test("entityLink returns a list link for a list entity", () => {
  const link = entityLink(makeEvent({ event_type: "list.created", entity_type: "list", entity_id: "l1" }));
  assert.deepEqual(link, { href: "/crm/lists/l1", label: "View list" });
});

test("entityLink returns a campaign link for a campaign entity", () => {
  const link = entityLink(makeEvent({ event_type: "campaign.created", entity_type: "campaign", entity_id: "camp1" }));
  assert.deepEqual(link, { href: "/manager/campaigns/camp1", label: "View campaign" });
});

test("entityLink never links a *.deleted event, even though entity_id is present", () => {
  const link = entityLink(
    makeEvent({ event_type: "list.deleted", entity_type: "list", entity_id: "l1", entity_name: "Old List" })
  );
  assert.equal(link, null);
});

test("entityLink returns null when there is no entity at all (e.g. a bulk export/sync event)", () => {
  const link = entityLink(makeEvent({ event_type: "contacts.exported", entity_type: null, entity_id: null }));
  assert.equal(link, null);
});

test("detailLines includes entity type/id and every non-empty metadata key, human-labeled", () => {
  const event = makeEvent({
    entity_type: "list",
    entity_id: "l1",
    metadata: { added: 89, already_member: 4, not_found: 0 },
  });
  const lines = detailLines(event);
  assert.deepEqual(lines, [
    { label: "Entity type", value: "List" },
    { label: "Entity ID", value: "l1" },
    { label: "Added", value: "89" },
    { label: "Already Member", value: "4" },
    { label: "Not Found", value: "0" },
  ]);
});

test("detailLines omits blank/null/undefined metadata values rather than showing them as N/A", () => {
  const event = makeEvent({ metadata: { error: null, submitted_at: "", contact_count: 127 } });
  const lines = detailLines(event);
  assert.deepEqual(lines.map((l) => l.label), ["Entity type", "Entity ID", "Contact Count"]);
});

test("detailLines never dumps a nested object as raw JSON without a readable label", () => {
  const event = makeEvent({ entity_type: null, entity_id: null, metadata: { report: { created: 5, updated: 2 } } });
  const lines = detailLines(event);
  assert.equal(lines[0].label, "Report");
  assert.equal(lines[0].value, JSON.stringify({ created: 5, updated: 2 }));
});
