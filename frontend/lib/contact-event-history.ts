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

// Event History generalization stage: the two-axis rsvp_status/attendance_status
// model (see EngagementParticipant's own backend docstring for why they're kept
// separate) collapses to ONE user-facing status word for the card's primary
// line -- attendance_status wins whenever it's set (it answers the more
// specific "did they actually show up" question), falling back to
// rsvp_status, and finally "—" when neither is known. Nothing is lost from
// the API by doing this: rsvpLabel/attendanceLabel below still carry both
// individually for any other consumer.
function primaryStatusLabel(
  attendanceStatus: ApiContactEventHistoryEntry["attendance_status"],
  rsvpStatus: ApiContactEventHistoryEntry["rsvp_status"]
): string {
  if (attendanceStatus !== null) return participantAttendanceStatusLabel(attendanceStatus);
  if (rsvpStatus !== null) return participantRsvpStatusLabel(rsvpStatus);
  return "—";
}

// Guest is the default role Luma sync (and most manual adds) always uses --
// showing it on every single card would be noise, not signal. Any OTHER role
// (Host, Sponsor, Speaker/Panelist, Astronomic Team, Client, Other) is
// meaningfully different from "just attended as a guest" and worth a line.
function isNoteworthyRole(role: ApiContactEventHistoryEntry["role"]): boolean {
  return role !== "guest";
}

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
  // Event History generalization stage -- the card's own simplified visual
  // hierarchy: event name (primary), then this line combining date/location/
  // status (primary metadata), then secondaryLabel (role and/or client, only
  // when there's something noteworthy to show). None of the existing fields
  // below are removed -- this only ADDS a simpler way to render the same data.
  metaLabel: string; // "Sep 10, 2026 · Austin, TX · Attended" (omits any missing piece)
  secondaryLabel: string | null; // role (if not the default Guest) and/or client name; null if neither applies
  location: string | null;
  clientName: string | null; // null for Astronomic's own directly-hosted events (see backend docstring)
  dateLabel: string; // formatClientDate's own "—" for a null date
  typeLabel: string; // e.g. "Dinner · Donor Dinner", or just "Sponsorship" for a non-dinner
  roleLabel: string;
  rsvpLabel: string; // "—" for null (participantRsvpStatusLabel's own null handling)
  attendanceLabel: string; // "—" for null
  sourceLabel: string; // "Manual" or "Luma" -- kept in the data, just not emphasized in the default card
}

export function buildParticipantEventHistoryEntry(entry: ApiContactEventHistoryEntry): ParticipantEventHistoryEntry {
  const typeLabel = entry.dinner_type
    ? `${engagementTypeLabel(entry.engagement_type)} · ${dinnerTypeLabel(entry.dinner_type)}`
    : engagementTypeLabel(entry.engagement_type);

  const dateLabel = formatClientDate(entry.engagement_date);
  const statusLabel = primaryStatusLabel(entry.attendance_status, entry.rsvp_status);
  const metaLabel = [dateLabel !== "—" ? dateLabel : null, entry.location, statusLabel !== "—" ? statusLabel : null]
    .filter((part): part is string => Boolean(part))
    .join(" · ");

  const secondaryParts: string[] = [];
  if (isNoteworthyRole(entry.role)) secondaryParts.push(participantRoleLabel(entry.role));
  if (entry.client_name) secondaryParts.push(entry.client_name);
  const secondaryLabel = secondaryParts.length > 0 ? secondaryParts.join(" · ") : null;

  return {
    engagementId: entry.engagement_id,
    participantId: entry.participant_id,
    eventName: entry.event_name,
    metaLabel,
    secondaryLabel,
    location: entry.location,
    clientName: entry.client_name,
    dateLabel,
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
