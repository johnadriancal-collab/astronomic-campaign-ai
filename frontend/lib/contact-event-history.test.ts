import assert from "node:assert/strict";
import { test } from "node:test";
import { buildEventHistory, buildEventHistoryEntry, buildParticipantEventHistory, buildParticipantEventHistoryEntry } from "./contact-event-history.ts";
import type { ContactEventHistoryEntry, CrmContactLumaRegistration } from "./api.ts";

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

// --- Contacts CRM Stage 3A -- canonical, EngagementParticipant-derived ---

function makeParticipantEntry(overrides: Partial<ContactEventHistoryEntry> = {}): ContactEventHistoryEntry {
  return {
    engagement_id: "e1",
    participant_id: "p1",
    event_name: "Austin Donor Dinner",
    client_name: "Miracle Foundation",
    engagement_date: "2026-10-08",
    engagement_type: "dinner",
    dinner_type: "donor_dinner",
    role: "guest",
    rsvp_status: "confirmed",
    attendance_status: null,
    source: "manual",
    ...overrides,
  };
}

test("a manual Guest/Confirmed entry renders with friendly labels and 'Manual' source", () => {
  const entry = buildParticipantEventHistoryEntry(makeParticipantEntry());
  assert.equal(entry.eventName, "Austin Donor Dinner");
  assert.equal(entry.clientName, "Miracle Foundation");
  assert.equal(entry.roleLabel, "Guest");
  assert.equal(entry.rsvpLabel, "Confirmed");
  assert.equal(entry.sourceLabel, "Manual");
});

test("a Luma-sourced entry displays 'Luma' as the source label", () => {
  const entry = buildParticipantEventHistoryEntry(makeParticipantEntry({ source: "luma" }));
  assert.equal(entry.sourceLabel, "Luma");
});

test("null rsvp_status displays as —", () => {
  const entry = buildParticipantEventHistoryEntry(makeParticipantEntry({ rsvp_status: null }));
  assert.equal(entry.rsvpLabel, "—");
});

test("null attendance_status displays as —", () => {
  const entry = buildParticipantEventHistoryEntry(makeParticipantEntry({ attendance_status: null }));
  assert.equal(entry.attendanceLabel, "—");
});

test("a real attendance_status displays its friendly label", () => {
  const entry = buildParticipantEventHistoryEntry(makeParticipantEntry({ attendance_status: "attended" }));
  assert.equal(entry.attendanceLabel, "Attended");
});

test("null engagement_date displays as — (reuses the same formatter as everywhere else)", () => {
  const entry = buildParticipantEventHistoryEntry(makeParticipantEntry({ engagement_date: null }));
  assert.equal(entry.dateLabel, "—");
});

test("a real engagement_date is formatted like every other Client CRM date", () => {
  const entry = buildParticipantEventHistoryEntry(makeParticipantEntry({ engagement_date: "2026-10-08" }));
  assert.equal(entry.dateLabel, "Oct 8, 2026");
});

test("dinner_type present combines Engagement Type and Dinner Type into one compact label", () => {
  const entry = buildParticipantEventHistoryEntry(makeParticipantEntry({ engagement_type: "dinner", dinner_type: "donor_dinner" }));
  assert.equal(entry.typeLabel, "Dinner · Donor Dinner");
});

test("no dinner_type falls back to just the Engagement Type label", () => {
  const entry = buildParticipantEventHistoryEntry(makeParticipantEntry({ engagement_type: "sponsorship", dinner_type: null }));
  assert.equal(entry.typeLabel, "Sponsorship");
});

test("every role value produces a real, friendly label, not the raw enum string", () => {
  for (const role of ["guest", "client", "host", "speaker_panelist", "astronomic_team", "other"] as const) {
    const entry = buildParticipantEventHistoryEntry(makeParticipantEntry({ role }));
    assert.notEqual(entry.roleLabel, role);
  }
});

test("buildParticipantEventHistory maps in the given order without re-sorting -- trusts the backend's own ordering", () => {
  const entries = [
    makeParticipantEntry({ participant_id: "p-newest", event_name: "Newer Dinner", engagement_date: "2026-11-01" }),
    makeParticipantEntry({ participant_id: "p-oldest", event_name: "Older Dinner", engagement_date: "2026-01-01" }),
  ];
  const built = buildParticipantEventHistory(entries);
  assert.deepEqual(
    built.map((e) => e.eventName),
    ["Newer Dinner", "Older Dinner"]
  );
});

test("an empty list produces an empty history", () => {
  assert.deepEqual(buildParticipantEventHistory([]), []);
});
