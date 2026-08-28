import assert from "node:assert/strict";
import { test } from "node:test";
import { buildEventHistory, buildEventHistoryEntry } from "./contact-event-history.ts";
import type { CrmContactLumaRegistration } from "./api.ts";

function makeRegistration(overrides: Partial<CrmContactLumaRegistration> = {}): CrmContactLumaRegistration {
  return {
    luma_event_id: "evt-1",
    event_name: "Hot Shot Investor Dinner ATX",
    approval_status: "approved",
    registered_at: "2026-08-27T00:00:00Z",
    checked_in_at: null,
    ...overrides,
  };
}

test("a registered-and-approved registration shows 'Approved' with a registered date", () => {
  const entry = buildEventHistoryEntry(makeRegistration({ registered_at: "2026-08-27T12:00:00Z" }));
  assert.equal(entry.statusLabel, "Approved");
  assert.equal(entry.dateLabel, "Registered Aug 27, 2026");
});

test("approved is never labeled Attended without a real check-in", () => {
  const entry = buildEventHistoryEntry(makeRegistration({ approval_status: "approved", checked_in_at: null }));
  assert.notEqual(entry.statusLabel, "Attended");
  assert.equal(entry.statusLabel, "Approved");
});

test("a non-null checked_in_at produces Attended, overriding the approval-based label", () => {
  const entry = buildEventHistoryEntry(
    makeRegistration({ approval_status: "approved", checked_in_at: "2026-09-17T16:00:00Z" })
  );
  assert.equal(entry.statusLabel, "Attended");
  assert.ok(entry.dateLabel.startsWith("Checked in "), `expected a "Checked in" date label, got "${entry.dateLabel}"`);
});

test("declined registrations are labeled Declined, not Approved or Attended", () => {
  const entry = buildEventHistoryEntry(makeRegistration({ approval_status: "declined", checked_in_at: null }));
  assert.equal(entry.statusLabel, "Declined");
});

test("waitlisted registrations are labeled Waitlisted", () => {
  const entry = buildEventHistoryEntry(makeRegistration({ approval_status: "waitlist", checked_in_at: null }));
  assert.equal(entry.statusLabel, "Waitlisted");
});

test("multiple events are rendered in the order given (backend already sorts newest first)", () => {
  const registrations = [
    makeRegistration({ luma_event_id: "evt-new", event_name: "Newer Dinner", registered_at: "2026-08-01T00:00:00Z" }),
    makeRegistration({ luma_event_id: "evt-old", event_name: "Older Dinner", registered_at: "2026-01-01T00:00:00Z" }),
  ];
  const entries = buildEventHistory(registrations);
  assert.deepEqual(
    entries.map((e) => e.eventName),
    ["Newer Dinner", "Older Dinner"]
  );
});

test("no registrations produces an empty history (the page renders the empty state for this)", () => {
  assert.deepEqual(buildEventHistory([]), []);
});

test("a registration with no registered_at and no check-in has no date label, never a fabricated date", () => {
  const entry = buildEventHistoryEntry(makeRegistration({ registered_at: null, checked_in_at: null }));
  assert.equal(entry.dateLabel, "");
});
