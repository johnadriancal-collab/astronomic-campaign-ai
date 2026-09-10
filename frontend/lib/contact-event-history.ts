// Pure formatting for the CRM contact detail page's Event History section --
// kept separate from the page component so it's unit-testable without
// rendering React, same split as lib/contact-summary.ts.
//
// Truthfulness rules are strict and enforced here, not just documented:
// checked_in_at is the ONLY thing that ever produces "Attended" -- an
// approved (or any other) registration without a check-in is never
// relabeled as attended. The backend never returns an "invited" registration
// today (see CrmContactLumaRegistration's docstring), but the label map
// below still has an entry for it so a display bug is a wrong string, never
// a crash, if that ever changes.

import type { ContactEventHistoryEntry as ApiContactEventHistoryEntry, CrmContactLumaRegistration } from "@/lib/api";
import {
  dinnerTypeLabel,
  engagementTypeLabel,
  formatClientDate,
  participantAttendanceStatusLabel,
  participantRoleLabel,
  participantRsvpStatusLabel,
  participantSourceLabel,
} from "./client-crm.ts";

export interface ContactEventHistoryEntry {
  eventName: string;
  lumaEventId: string;
  statusLabel: string;
  dateLabel: string;
}

const APPROVAL_STATUS_LABEL: Record<string, string> = {
  approved: "Approved",
  declined: "Declined",
  waitlist: "Waitlisted",
  pending_approval: "Pending Approval",
  invited: "Invited",
  session: "Registered",
};

export function formatEventDate(iso: string): string {
  return new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}

// registration_answers/luma_guest_id are never part of CrmContactLumaRegistration
// in the first place (the backend excludes them) -- this function only ever
// touches the 5 fields that model actually carries.
export function buildEventHistoryEntry(registration: CrmContactLumaRegistration): ContactEventHistoryEntry {
  const attended = registration.checked_in_at != null;
  const statusLabel = attended ? "Attended" : APPROVAL_STATUS_LABEL[registration.approval_status] ?? "Registered";

  let dateLabel = "";
  if (attended && registration.checked_in_at) {
    dateLabel = `Checked in ${formatEventDate(registration.checked_in_at)}`;
  } else if (registration.registered_at) {
    dateLabel = `Registered ${formatEventDate(registration.registered_at)}`;
  }

  return {
    eventName: registration.event_name,
    lumaEventId: registration.luma_event_id,
    statusLabel,
    dateLabel,
  };
}

// The backend already returns registrations newest-first; this just maps
// each one through buildEventHistoryEntry without re-sorting, so a change
// here can never silently reorder what the backend decided.
export function buildEventHistory(registrations: CrmContactLumaRegistration[]): ContactEventHistoryEntry[] {
  return registrations.map(buildEventHistoryEntry);
}

// --- Contacts CRM Stage 3A -- canonical, EngagementParticipant-derived ---
// The contact detail page's Event History section reads from THIS below,
// not the LumaRegistration-derived functions above (those stay in place,
// still exported/tested, purely for /luma-registrations compatibility).
// Every label here reuses the SAME helpers Client CRM's own Engagement
// Participant UI already built (lib/client-crm.ts) -- never a second,
// independently-maintained copy of the same enum-label mapping. Ordering
// is entirely the backend's responsibility (see
// ClientCrmService.list_contact_event_history()) -- this only maps, never
// re-sorts, same "trust the backend's own order" convention as
// buildEventHistory above.

export interface ParticipantEventHistoryEntry {
  engagementId: string;
  participantId: string;
  eventName: string;
  clientName: string;
  dateLabel: string; // formatClientDate's own "—" for a null date
  typeLabel: string; // e.g. "Dinner · Donor Dinner", or just "Sponsorship" for a non-dinner
  roleLabel: string;
  rsvpLabel: string; // "—" for null (participantRsvpStatusLabel's own null handling)
  attendanceLabel: string; // "—" for null
  sourceLabel: string; // "Manual" or "Luma"
}

export function buildParticipantEventHistoryEntry(entry: ApiContactEventHistoryEntry): ParticipantEventHistoryEntry {
  const typeLabel = entry.dinner_type
    ? `${engagementTypeLabel(entry.engagement_type)} · ${dinnerTypeLabel(entry.dinner_type)}`
    : engagementTypeLabel(entry.engagement_type);

  return {
    engagementId: entry.engagement_id,
    participantId: entry.participant_id,
    eventName: entry.event_name,
    clientName: entry.client_name,
    dateLabel: formatClientDate(entry.engagement_date),
    typeLabel,
    roleLabel: participantRoleLabel(entry.role),
    rsvpLabel: participantRsvpStatusLabel(entry.rsvp_status),
    attendanceLabel: participantAttendanceStatusLabel(entry.attendance_status),
    sourceLabel: participantSourceLabel(entry.source),
  };
}

export function buildParticipantEventHistory(entries: ApiContactEventHistoryEntry[]): ParticipantEventHistoryEntry[] {
  return entries.map(buildParticipantEventHistoryEntry);
}
