// Pure logic for Client CRM (Stage 1C, extended 1D/1E) -- kept separate
// from page/component code so it's unit-testable without rendering React,
// same split as lib/mail.ts and lib/mail-trigger.ts. Client is
// Astronomic's relationship/sales record for organizations -- distinct
// from CrmContact (people/prospects) and distinct from an Engagement (one
// commercial service/project delivered for a Client, e.g. a dinner) --
// see app/models/client_crm.py's own docstring on the backend.

import type {
  Client,
  ClientContact,
  ClientContactCreateInput,
  ClientContactUpdateInput,
  ClientCreateInput,
  ClientRelationshipClassification,
  ClientStatus,
  ClientTouchpoint,
  ClientTouchpointCreateInput,
  ClientTouchpointUpdateInput,
  ClientUpdateInput,
  ContactType,
  DinnerType,
  Engagement,
  EngagementCloseout,
  EngagementCloseoutCreateInput,
  EngagementCloseoutUpdateInput,
  EngagementContractStatus,
  EngagementCreateInput,
  EngagementParticipant,
  EngagementParticipantCreateInput,
  EngagementParticipantUpdateInput,
  EngagementPaymentStatus,
  EngagementStatus,
  EngagementType,
  EngagementUpdateInput,
  ParticipantAttendanceStatus,
  ParticipantRole,
  ParticipantRsvpStatus,
} from "@/lib/api";

export const CLIENT_STATUS_OPTIONS: { value: ClientStatus; label: string }[] = [
  { value: "active", label: "Active" },
  { value: "inactive", label: "Inactive" },
];

export function clientStatusLabel(status: ClientStatus): string {
  return CLIENT_STATUS_OPTIONS.find((s) => s.value === status)?.label ?? status;
}

// "active" is the plain/default case (most Clients are active) and gets a
// calm, muted treatment; "inactive" is the one worth a slightly more
// noticeable (but not alarming) color -- same "bold color for the state
// that needs noticing" philosophy as mailCampaignStatusBadgeClass, just
// pointed at the less-common value here instead of the most-common one.
export function clientStatusBadgeClass(status: ClientStatus): string {
  switch (status) {
    case "inactive":
      return "bg-amber-100 text-amber-800";
    case "active":
    default:
      return "bg-secondary text-muted-foreground";
  }
}

export const CLIENT_RELATIONSHIP_CLASSIFICATION_OPTIONS: { value: ClientRelationshipClassification; label: string }[] = [
  { value: "nurture", label: "Nurture" },
  { value: "opportunity", label: "Opportunity" },
  { value: "referral", label: "Referral" },
  { value: "needs_attention", label: "Needs Attention" },
  { value: "closed_inactive", label: "Closed / Inactive" },
];

// `null` (not yet assessed -- see Client.relationship_classification's own
// backend docstring: a fresh Client defaults to unclassified, never
// auto-assigned "Nurture") renders as a plain, neutral "—", never a
// colored badge and never silently treated as any real classification.
export function clientRelationshipClassificationLabel(value: ClientRelationshipClassification | null): string {
  if (value === null) return "—";
  return CLIENT_RELATIONSHIP_CLASSIFICATION_OPTIONS.find((c) => c.value === value)?.label ?? value;
}

export function clientRelationshipClassificationBadgeClass(value: ClientRelationshipClassification | null): string {
  switch (value) {
    case "opportunity":
      return "bg-emerald-100 text-emerald-800";
    case "referral":
      return "bg-violet-100 text-violet-800";
    case "needs_attention":
      return "bg-amber-100 text-amber-800";
    case "nurture":
      return "bg-blue-100 text-blue-800";
    case "closed_inactive":
    default:
      return "bg-secondary text-muted-foreground";
  }
}

export function formatClientDate(iso: string | null): string {
  if (!iso) return "—";
  // "YYYY-MM-DD" (next_action_due) and a full ISO timestamp (created_at/
  // updated_at) both parse fine here; deliberately UTC/date-only display
  // (no time-of-day, no browser-timezone conversion) since a Client-level
  // "next action due" is a plain calendar date, not an instant.
  const date = new Date(iso.length <= 10 ? `${iso}T00:00:00Z` : iso);
  return date.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" });
}

// --- List query construction -----------------------------------------------

export interface ClientListFilters {
  q: string;
  status: ClientStatus | "";
  relationshipClassification: ClientRelationshipClassification | "";
  owner: string;
  includeArchived: boolean;
  sortBy: "name" | "created_at" | "updated_at" | "next_action_due" | "next_dinner";
  sortDir: "asc" | "desc";
  page: number;
  pageSize: number;
}

// Client CRM Stage 2C.1: the Master Client CRM's own default ordering is
// nearest-upcoming-dinner-first, not alphabetical -- there is still no
// visible sort control on that page (see app/clients/page.tsx), so this
// default is the ONLY way a human viewing it ever sees Clients ordered;
// "next_dinner" is a fully legal, explicit sort_by value the backend
// already supports (see ClientCrmService.list_clients()'s own Stage 2C.1
// docstring for the exact locked ordering: qualifying dinners ascending,
// no-upcoming-dinner Clients always last). This is the ONE call site
// that changed -- the backend's own bare/omitted sort_by default is
// deliberately left as "name", unchanged, in case anything else ever
// calls this API directly without an explicit sort_by.
export function defaultClientListFilters(): ClientListFilters {
  return {
    q: "",
    status: "",
    relationshipClassification: "",
    owner: "",
    includeArchived: false,
    sortBy: "next_dinner",
    sortDir: "asc",
    page: 1,
    pageSize: 25,
  };
}

/** Maps the UI's own filter state onto exactly the backend's real query
 * param names -- an empty-string filter is omitted entirely (matches
 * "any"), never sent as a literal empty string the backend would have to
 * special-case. */
export function buildClientListQueryParams(filters: ClientListFilters) {
  return {
    q: filters.q.trim() || undefined,
    status: filters.status || undefined,
    relationship_classification: filters.relationshipClassification || undefined,
    owner: filters.owner.trim() || undefined,
    include_archived: filters.includeArchived,
    sort_by: filters.sortBy,
    sort_dir: filters.sortDir,
    page: filters.page,
    page_size: filters.pageSize,
  };
}

// --- Create / edit form ------------------------------------------------

export interface ClientFormState {
  name: string;
  website: string;
  industry: string;
  status: ClientStatus;
  relationshipClassification: ClientRelationshipClassification | "";
  owner: string;
  nextAction: string;
  nextActionDue: string; // "" or "YYYY-MM-DD"
}

export function emptyClientFormState(): ClientFormState {
  return {
    name: "",
    website: "",
    industry: "",
    status: "active",
    relationshipClassification: "",
    owner: "",
    nextAction: "",
    nextActionDue: "",
  };
}

export function clientFormStateFromClient(client: Client): ClientFormState {
  return {
    name: client.name,
    website: client.website ?? "",
    industry: client.industry ?? "",
    status: client.status,
    relationshipClassification: client.relationship_classification ?? "",
    owner: client.owner ?? "",
    nextAction: client.next_action ?? "",
    nextActionDue: client.next_action_due ?? "",
  };
}

export function isClientFormValid(form: ClientFormState): boolean {
  return form.name.trim().length > 0;
}

/** Every optional field an explicit `null` when blank (never omitted) --
 * this is a CREATE, so there is no "existing value" to preserve; a blank
 * field means "no value", sent as such. Duplicate Client names are
 * deliberately never checked or blocked here -- the backend allows them
 * on purpose (see ClientCrmService's own docstring). */
export function clientCreatePayload(form: ClientFormState): ClientCreateInput {
  return {
    name: form.name.trim(),
    website: form.website.trim() || null,
    industry: form.industry.trim() || null,
    status: form.status,
    relationship_classification: form.relationshipClassification || null,
    owner: form.owner.trim() || null,
    next_action: form.nextAction.trim() || null,
    next_action_due: form.nextActionDue || null,
  };
}

/** A genuinely partial PATCH -- only fields whose value actually differs
 * from `original` are included, matching the backend's own partial-PATCH
 * contract (see app/api/client_crm.py's ClientUpdateRequest) as closely
 * as the frontend can: if nothing changed, this returns an empty object
 * rather than resending the whole form. `client_id`/`created_at`/
 * `updated_at` are never included -- this function has no way to even
 * construct them, since ClientFormState carries none of them. */
export function clientUpdatePatch(form: ClientFormState, original: Client): ClientUpdateInput {
  const patch: ClientUpdateInput = {};
  const name = form.name.trim();
  if (name !== original.name) patch.name = name;

  const website = form.website.trim() || null;
  if (website !== original.website) patch.website = website;

  const industry = form.industry.trim() || null;
  if (industry !== original.industry) patch.industry = industry;

  if (form.status !== original.status) patch.status = form.status;

  const relationshipClassification = form.relationshipClassification || null;
  if (relationshipClassification !== original.relationship_classification) {
    patch.relationship_classification = relationshipClassification;
  }

  const owner = form.owner.trim() || null;
  if (owner !== original.owner) patch.owner = owner;

  const nextAction = form.nextAction.trim() || null;
  if (nextAction !== original.next_action) patch.next_action = nextAction;

  const nextActionDue = form.nextActionDue || null;
  if (nextActionDue !== original.next_action_due) patch.next_action_due = nextActionDue;

  return patch;
}

// --- ClientContact (Stage 1D) -------------------------------------------

export function clientContactDisplayName(contact: Pick<ClientContact, "first_name" | "last_name">): string {
  return [contact.first_name, contact.last_name].filter(Boolean).join(" ") || "Unnamed contact";
}

export interface ClientContactFormState {
  title: string;
  isPrimaryContact: boolean;
  isDecisionMaker: boolean;
  roleNotes: string;
}

export function emptyClientContactFormState(): ClientContactFormState {
  return { title: "", isPrimaryContact: false, isDecisionMaker: false, roleNotes: "" };
}

export function clientContactFormStateFromContact(contact: ClientContact): ClientContactFormState {
  return {
    title: contact.title ?? "",
    isPrimaryContact: contact.is_primary_contact,
    isDecisionMaker: contact.is_decision_maker,
    roleNotes: contact.role_notes ?? "",
  };
}

/** CREATE only -- `crmContactId` comes from the picker, not the form
 * state itself (there is no free-text way to set it -- see this stage's
 * own STOP report on why no free-text Primary Contact field exists). */
export function clientContactCreatePayload(form: ClientContactFormState, crmContactId: string): ClientContactCreateInput {
  return {
    crm_contact_id: crmContactId,
    title: form.title.trim() || null,
    is_primary_contact: form.isPrimaryContact,
    is_decision_maker: form.isDecisionMaker,
    role_notes: form.roleNotes.trim() || null,
  };
}

/** A genuine partial PATCH, same diff-only convention as
 * clientUpdatePatch -- crm_contact_id/snapshot fields are never part of
 * this form at all (re-linking to a different person isn't supported in
 * V1), so there is nothing here that could ever leak them. */
export function clientContactUpdatePatch(form: ClientContactFormState, original: ClientContact): ClientContactUpdateInput {
  const patch: ClientContactUpdateInput = {};
  const title = form.title.trim() || null;
  if (title !== original.title) patch.title = title;
  if (form.isPrimaryContact !== original.is_primary_contact) patch.is_primary_contact = form.isPrimaryContact;
  if (form.isDecisionMaker !== original.is_decision_maker) patch.is_decision_maker = form.isDecisionMaker;
  const roleNotes = form.roleNotes.trim() || null;
  if (roleNotes !== original.role_notes) patch.role_notes = roleNotes;
  return patch;
}

// --- Engagement (Stage 1E) -----------------------------------------------

export const ENGAGEMENT_TYPE_OPTIONS: { value: EngagementType; label: string }[] = [
  { value: "dinner", label: "Dinner" },
  { value: "sponsorship", label: "Sponsorship" },
  { value: "other", label: "Other" },
];

export function engagementTypeLabel(value: EngagementType): string {
  return ENGAGEMENT_TYPE_OPTIONS.find((o) => o.value === value)?.label ?? value;
}

// Only "dinner" is dinner-shaped -- matches the backend's own
// authoritative normalization in ClientCrmService (_normalize_dinner_type).
// The frontend uses this ONLY to decide whether to show/clear the Dinner
// Type field -- the backend remains the source of truth regardless of
// what the frontend sends.
export function isDinnerShapedEngagementType(value: EngagementType): boolean {
  return value === "dinner";
}

export const DINNER_TYPE_OPTIONS: { value: DinnerType; label: string }[] = [
  { value: "investor_dinner", label: "Investor Dinner" },
  { value: "fireside_dinner", label: "Fireside Dinner" },
  { value: "bizdev_dinner", label: "BizDev Dinner" },
  { value: "donor_dinner", label: "Donor Dinner" },
  { value: "custom_dinner", label: "Custom Dinner" },
];

export function dinnerTypeLabel(value: DinnerType | null): string {
  if (value === null) return "—";
  return DINNER_TYPE_OPTIONS.find((o) => o.value === value)?.label ?? value;
}

export const ENGAGEMENT_STATUS_OPTIONS: { value: EngagementStatus; label: string }[] = [
  { value: "planned", label: "Planned" },
  { value: "confirmed", label: "Confirmed" },
  { value: "completed", label: "Completed" },
  { value: "cancelled", label: "Cancelled" },
];

export function engagementStatusLabel(value: EngagementStatus): string {
  return ENGAGEMENT_STATUS_OPTIONS.find((o) => o.value === value)?.label ?? value;
}

export function engagementStatusBadgeClass(status: EngagementStatus): string {
  switch (status) {
    case "confirmed":
      return "bg-blue-100 text-blue-800";
    case "completed":
      return "bg-emerald-100 text-emerald-800";
    case "cancelled":
      return "bg-secondary text-muted-foreground";
    case "planned":
    default:
      return "bg-amber-100 text-amber-800";
  }
}

export const ENGAGEMENT_CONTRACT_STATUS_OPTIONS: { value: EngagementContractStatus; label: string }[] = [
  { value: "not_sent", label: "Not Sent" },
  { value: "sent", label: "Sent" },
  { value: "signed", label: "Signed" },
];

export function engagementContractStatusLabel(value: EngagementContractStatus): string {
  return ENGAGEMENT_CONTRACT_STATUS_OPTIONS.find((o) => o.value === value)?.label ?? value;
}

export const ENGAGEMENT_PAYMENT_STATUS_OPTIONS: { value: EngagementPaymentStatus; label: string }[] = [
  { value: "unpaid", label: "Unpaid" },
  { value: "partial", label: "Partial" },
  { value: "paid", label: "Paid" },
];

export function engagementPaymentStatusLabel(value: EngagementPaymentStatus): string {
  return ENGAGEMENT_PAYMENT_STATUS_OPTIONS.find((o) => o.value === value)?.label ?? value;
}

export function formatEngagementDate(iso: string | null): string {
  if (!iso) return "—";
  const date = new Date(iso.length <= 10 ? `${iso}T00:00:00Z` : iso);
  return date.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" });
}

export interface EngagementFormState {
  title: string;
  engagementType: EngagementType;
  dinnerType: DinnerType | "";
  engagementDate: string; // "" or "YYYY-MM-DD"
  location: string;
  status: EngagementStatus;
  owner: string;
  fee: string; // "" or a numeric string -- kept as text in the form, parsed on submit
  contractStatus: EngagementContractStatus;
  contractUrl: string;
  signedDate: string; // "" or "YYYY-MM-DD"
  paymentStatus: EngagementPaymentStatus;
}

export function emptyEngagementFormState(): EngagementFormState {
  return {
    title: "",
    engagementType: "dinner",
    dinnerType: "investor_dinner",
    engagementDate: "",
    location: "",
    status: "planned",
    owner: "",
    fee: "",
    contractStatus: "not_sent",
    contractUrl: "",
    signedDate: "",
    paymentStatus: "unpaid",
  };
}

export function engagementFormStateFromEngagement(engagement: Engagement): EngagementFormState {
  return {
    title: engagement.title,
    engagementType: engagement.engagement_type,
    dinnerType: engagement.dinner_type ?? "",
    engagementDate: engagement.engagement_date ?? "",
    location: engagement.location ?? "",
    status: engagement.status,
    owner: engagement.owner ?? "",
    fee: engagement.fee === null ? "" : String(engagement.fee),
    contractStatus: engagement.contract_status,
    contractUrl: engagement.contract_url ?? "",
    signedDate: engagement.signed_date ?? "",
    paymentStatus: engagement.payment_status,
  };
}

export function isEngagementFormValid(form: EngagementFormState): boolean {
  return form.title.trim().length > 0;
}

function parseEngagementFee(fee: string): number | null {
  const trimmed = fee.trim();
  if (!trimmed) return null;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? parsed : null;
}

/** CREATE only. `dinner_type` is sent exactly as chosen in the form --
 * the backend remains authoritative and will clear it server-side if
 * `engagement_type` isn't dinner-shaped, but the form itself also hides/
 * clears the field for a non-dinner type (see isDinnerShapedEngagementType)
 * so this rarely needs to rely on that backend correction in practice. */
export function engagementCreatePayload(form: EngagementFormState): EngagementCreateInput {
  return {
    title: form.title.trim(),
    engagement_type: form.engagementType,
    dinner_type: isDinnerShapedEngagementType(form.engagementType) ? form.dinnerType || null : null,
    engagement_date: form.engagementDate || null,
    location: form.location.trim() || null,
    status: form.status,
    owner: form.owner.trim() || null,
    fee: parseEngagementFee(form.fee),
    contract_status: form.contractStatus,
    contract_url: form.contractUrl.trim() || null,
    signed_date: form.signedDate || null,
    payment_status: form.paymentStatus,
  };
}

/** A genuine partial PATCH, same diff-only convention as
 * clientUpdatePatch/clientContactUpdatePatch. */
export function engagementUpdatePatch(form: EngagementFormState, original: Engagement): EngagementUpdateInput {
  const patch: EngagementUpdateInput = {};
  const title = form.title.trim();
  if (title !== original.title) patch.title = title;
  if (form.engagementType !== original.engagement_type) patch.engagement_type = form.engagementType;

  const dinnerType = isDinnerShapedEngagementType(form.engagementType) ? form.dinnerType || null : null;
  if (dinnerType !== original.dinner_type) patch.dinner_type = dinnerType;

  const engagementDate = form.engagementDate || null;
  if (engagementDate !== original.engagement_date) patch.engagement_date = engagementDate;

  const location = form.location.trim() || null;
  if (location !== original.location) patch.location = location;

  if (form.status !== original.status) patch.status = form.status;

  const owner = form.owner.trim() || null;
  if (owner !== original.owner) patch.owner = owner;

  const fee = parseEngagementFee(form.fee);
  if (fee !== original.fee) patch.fee = fee;

  if (form.contractStatus !== original.contract_status) patch.contract_status = form.contractStatus;

  const contractUrl = form.contractUrl.trim() || null;
  if (contractUrl !== original.contract_url) patch.contract_url = contractUrl;

  const signedDate = form.signedDate || null;
  if (signedDate !== original.signed_date) patch.signed_date = signedDate;

  if (form.paymentStatus !== original.payment_status) patch.payment_status = form.paymentStatus;

  return patch;
}

// --- EngagementCloseout (Stage 1F) -----------------------------------------

/** Purely derived from fields already on EngagementCloseout -- never
 * stored or returned by the backend (see EngagementCloseout's own model
 * docstring on the backend for why). V1 definition: attended /
 * confirmed_guest_count -- `confirmed_guest_count` IS the expected/
 * confirmed guest count for the dinner; this deliberately does NOT
 * subtract `cancelled_count` from it, since that would assume
 * cancellations were never already reflected in how a human entered
 * Confirmed Guests, an assumption this stage has no basis to make.
 * No-shows/cancellations/walk-ins stay separately recorded fields, not
 * folded into this formula. Returns null (not computable) unless both
 * confirmed_guest_count and attended_count are present and
 * confirmed_guest_count > 0 -- never a guessed or defaulted rate. Not
 * clamped to 100% -- an aggregate entry that implies over-attendance is
 * preserved as entered, not silently altered; Stage 1G's future
 * person-level attendance is what will make a truly reliable derived
 * metric possible. */
export function engagementCloseoutAttendanceRate(
  closeout: Pick<EngagementCloseout, "confirmed_guest_count" | "attended_count">
): number | null {
  const { confirmed_guest_count, attended_count } = closeout;
  if (confirmed_guest_count === null || attended_count === null) return null;
  if (confirmed_guest_count <= 0) return null;
  return attended_count / confirmed_guest_count;
}

export interface EngagementCloseoutFormState {
  confirmedGuestCount: string;
  attendedCount: string;
  noShowCount: string;
  cancelledCount: string;
  unexpectedAttendeeCount: string;
  guestQuality: string;
  dinnerDynamics: string;
  initialClientExperience: string;
  immediateOutcomes: string;
  notableSignals: string;
  issues: string;
  referrals: string;
  futureOpportunities: string;
  internalNotes: string;
  completedAt: string; // "" or "YYYY-MM-DD"
  completedBy: string;
}

export function emptyEngagementCloseoutFormState(): EngagementCloseoutFormState {
  return {
    confirmedGuestCount: "",
    attendedCount: "",
    noShowCount: "",
    cancelledCount: "",
    unexpectedAttendeeCount: "",
    guestQuality: "",
    dinnerDynamics: "",
    initialClientExperience: "",
    immediateOutcomes: "",
    notableSignals: "",
    issues: "",
    referrals: "",
    futureOpportunities: "",
    internalNotes: "",
    completedAt: "",
    completedBy: "",
  };
}

export function engagementCloseoutFormStateFromCloseout(closeout: EngagementCloseout): EngagementCloseoutFormState {
  return {
    confirmedGuestCount: closeout.confirmed_guest_count === null ? "" : String(closeout.confirmed_guest_count),
    attendedCount: closeout.attended_count === null ? "" : String(closeout.attended_count),
    noShowCount: closeout.no_show_count === null ? "" : String(closeout.no_show_count),
    cancelledCount: closeout.cancelled_count === null ? "" : String(closeout.cancelled_count),
    unexpectedAttendeeCount: closeout.unexpected_attendee_count === null ? "" : String(closeout.unexpected_attendee_count),
    guestQuality: closeout.guest_quality ?? "",
    dinnerDynamics: closeout.dinner_dynamics ?? "",
    initialClientExperience: closeout.initial_client_experience ?? "",
    immediateOutcomes: closeout.immediate_outcomes ?? "",
    notableSignals: closeout.notable_signals ?? "",
    issues: closeout.issues ?? "",
    referrals: closeout.referrals ?? "",
    futureOpportunities: closeout.future_opportunities ?? "",
    internalNotes: closeout.internal_notes ?? "",
    completedAt: closeout.completed_at ? closeout.completed_at.slice(0, 10) : "",
    completedBy: closeout.completed_by ?? "",
  };
}

/** Blank, negative, or non-numeric input is treated as "not entered"
 * (null) -- the frontend never sends a negative count; the backend's own
 * Field(ge=0) rejection is the authoritative guarantee, this is just a
 * graceful client-side fallback so a stray "-1" keystroke can't even be
 * submitted as one. */
function parseNonNegativeCount(value: string): number | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  const parsed = Number(trimmed);
  if (!Number.isFinite(parsed) || parsed < 0) return null;
  return Math.trunc(parsed);
}

function completedAtIsoFromFormValue(value: string): string | null {
  return value ? new Date(`${value}T00:00:00Z`).toISOString() : null;
}

/** CREATE only. Every field is optional -- there is no required field on
 * a Closeout (unlike Engagement's title), so this always produces a
 * valid payload even from a completely blank form. */
export function engagementCloseoutCreatePayload(form: EngagementCloseoutFormState): EngagementCloseoutCreateInput {
  return {
    confirmed_guest_count: parseNonNegativeCount(form.confirmedGuestCount),
    attended_count: parseNonNegativeCount(form.attendedCount),
    no_show_count: parseNonNegativeCount(form.noShowCount),
    cancelled_count: parseNonNegativeCount(form.cancelledCount),
    unexpected_attendee_count: parseNonNegativeCount(form.unexpectedAttendeeCount),
    guest_quality: form.guestQuality.trim() || null,
    dinner_dynamics: form.dinnerDynamics.trim() || null,
    initial_client_experience: form.initialClientExperience.trim() || null,
    immediate_outcomes: form.immediateOutcomes.trim() || null,
    notable_signals: form.notableSignals.trim() || null,
    issues: form.issues.trim() || null,
    referrals: form.referrals.trim() || null,
    future_opportunities: form.futureOpportunities.trim() || null,
    internal_notes: form.internalNotes.trim() || null,
    completed_at: completedAtIsoFromFormValue(form.completedAt),
    completed_by: form.completedBy.trim() || null,
  };
}

/** A genuine partial PATCH, same diff-only convention as
 * clientUpdatePatch/clientContactUpdatePatch/engagementUpdatePatch. */
export function engagementCloseoutUpdatePatch(
  form: EngagementCloseoutFormState,
  original: EngagementCloseout
): EngagementCloseoutUpdateInput {
  const patch: EngagementCloseoutUpdateInput = {};

  const confirmedGuestCount = parseNonNegativeCount(form.confirmedGuestCount);
  if (confirmedGuestCount !== original.confirmed_guest_count) patch.confirmed_guest_count = confirmedGuestCount;

  const attendedCount = parseNonNegativeCount(form.attendedCount);
  if (attendedCount !== original.attended_count) patch.attended_count = attendedCount;

  const noShowCount = parseNonNegativeCount(form.noShowCount);
  if (noShowCount !== original.no_show_count) patch.no_show_count = noShowCount;

  const cancelledCount = parseNonNegativeCount(form.cancelledCount);
  if (cancelledCount !== original.cancelled_count) patch.cancelled_count = cancelledCount;

  const unexpectedAttendeeCount = parseNonNegativeCount(form.unexpectedAttendeeCount);
  if (unexpectedAttendeeCount !== original.unexpected_attendee_count) {
    patch.unexpected_attendee_count = unexpectedAttendeeCount;
  }

  const guestQuality = form.guestQuality.trim() || null;
  if (guestQuality !== original.guest_quality) patch.guest_quality = guestQuality;

  const dinnerDynamics = form.dinnerDynamics.trim() || null;
  if (dinnerDynamics !== original.dinner_dynamics) patch.dinner_dynamics = dinnerDynamics;

  const initialClientExperience = form.initialClientExperience.trim() || null;
  if (initialClientExperience !== original.initial_client_experience) {
    patch.initial_client_experience = initialClientExperience;
  }

  const immediateOutcomes = form.immediateOutcomes.trim() || null;
  if (immediateOutcomes !== original.immediate_outcomes) patch.immediate_outcomes = immediateOutcomes;

  const notableSignals = form.notableSignals.trim() || null;
  if (notableSignals !== original.notable_signals) patch.notable_signals = notableSignals;

  const issues = form.issues.trim() || null;
  if (issues !== original.issues) patch.issues = issues;

  const referrals = form.referrals.trim() || null;
  if (referrals !== original.referrals) patch.referrals = referrals;

  const futureOpportunities = form.futureOpportunities.trim() || null;
  if (futureOpportunities !== original.future_opportunities) patch.future_opportunities = futureOpportunities;

  const internalNotes = form.internalNotes.trim() || null;
  if (internalNotes !== original.internal_notes) patch.internal_notes = internalNotes;

  const originalCompletedAtDate = original.completed_at ? original.completed_at.slice(0, 10) : "";
  if (form.completedAt !== originalCompletedAtDate) patch.completed_at = completedAtIsoFromFormValue(form.completedAt);

  const completedBy = form.completedBy.trim() || null;
  if (completedBy !== original.completed_by) patch.completed_by = completedBy;

  return patch;
}

// --- EngagementParticipant (Stage 1G) ---------------------------------------
// CrmContact <-> Engagement, when the canonical Contact is known -- see
// EngagementParticipant's own model docstring on the backend for the full
// architecture (unresolved participants, snapshot-not-live-reference,
// two-axis RSVP/attendance status, walk-in as provenance).

export const PARTICIPANT_ROLE_OPTIONS: { value: ParticipantRole; label: string }[] = [
  { value: "guest", label: "Guest" },
  { value: "client", label: "Client" },
  { value: "host", label: "Host" },
  { value: "speaker_panelist", label: "Speaker / Panelist" },
  { value: "astronomic_team", label: "Astronomic Team" },
  { value: "other", label: "Other" },
];

export function participantRoleLabel(value: ParticipantRole): string {
  return PARTICIPANT_ROLE_OPTIONS.find((o) => o.value === value)?.label ?? value;
}

export const PARTICIPANT_RSVP_STATUS_OPTIONS: { value: ParticipantRsvpStatus; label: string }[] = [
  { value: "invited", label: "Invited" },
  { value: "confirmed", label: "Confirmed" },
  { value: "declined", label: "Declined" },
];

export function participantRsvpStatusLabel(value: ParticipantRsvpStatus | null): string {
  if (value === null) return "—";
  return PARTICIPANT_RSVP_STATUS_OPTIONS.find((o) => o.value === value)?.label ?? value;
}

export const PARTICIPANT_ATTENDANCE_STATUS_OPTIONS: { value: ParticipantAttendanceStatus; label: string }[] = [
  { value: "attended", label: "Attended" },
  { value: "no_show", label: "No-Show" },
  { value: "cancelled", label: "Cancelled" },
];

export function participantAttendanceStatusLabel(value: ParticipantAttendanceStatus | null): string {
  if (value === null) return "—";
  return PARTICIPANT_ATTENDANCE_STATUS_OPTIONS.find((o) => o.value === value)?.label ?? value;
}

export function participantDisplayName(participant: Pick<EngagementParticipant, "first_name" | "last_name">): string {
  return [participant.first_name, participant.last_name].filter(Boolean).join(" ") || "Unnamed participant";
}

/** Mirrors the backend's own _participant_identity_is_meaningful rule
 * (service layer, not a Pydantic constraint) -- at least one of
 * first/last/email must be non-blank for an unresolved participant. This
 * copy exists purely for instant client-side feedback (disabling Save
 * before a round trip); the backend remains the authoritative check. */
export function participantIdentityIsMeaningful(firstName: string, lastName: string, email: string): boolean {
  return Boolean(firstName.trim() || lastName.trim() || email.trim());
}

export interface EngagementParticipantFormState {
  crmContactId: string | null;
  firstName: string;
  lastName: string;
  email: string;
  title: string;
  company: string;
  role: ParticipantRole;
  rsvpStatus: ParticipantRsvpStatus | "";
  attendanceStatus: ParticipantAttendanceStatus | "";
  isWalkIn: boolean;
}

export function emptyEngagementParticipantFormState(): EngagementParticipantFormState {
  return {
    crmContactId: null,
    firstName: "",
    lastName: "",
    email: "",
    title: "",
    company: "",
    role: "guest",
    rsvpStatus: "",
    attendanceStatus: "",
    isWalkIn: false,
  };
}

export function engagementParticipantFormStateFromParticipant(participant: EngagementParticipant): EngagementParticipantFormState {
  return {
    crmContactId: participant.crm_contact_id,
    firstName: participant.first_name ?? "",
    lastName: participant.last_name ?? "",
    email: participant.email ?? "",
    title: participant.title ?? "",
    company: participant.company ?? "",
    role: participant.role,
    rsvpStatus: participant.rsvp_status ?? "",
    attendanceStatus: participant.attendance_status ?? "",
    isWalkIn: participant.is_walk_in,
  };
}

/** CREATE only. When `crmContactId` is set (the resolved/primary path),
 * identity fields are omitted entirely -- the backend always snapshots
 * from the canonical Contact and ignores anything sent here in that case,
 * so there is no reason to send stale copies. When unresolved, the raw
 * form fields are sent as-is; the backend enforces the at-least-one-of
 * first/last/email rule (see participantIdentityIsMeaningful). */
export function engagementParticipantCreatePayload(form: EngagementParticipantFormState): EngagementParticipantCreateInput {
  const base: EngagementParticipantCreateInput = {
    role: form.role,
    rsvp_status: form.rsvpStatus || null,
    attendance_status: form.attendanceStatus || null,
    is_walk_in: form.isWalkIn,
  };
  if (form.crmContactId) {
    return { ...base, crm_contact_id: form.crmContactId };
  }
  return {
    ...base,
    crm_contact_id: null,
    first_name: form.firstName.trim() || null,
    last_name: form.lastName.trim() || null,
    email: form.email.trim() || null,
    title: form.title.trim() || null,
    company: form.company.trim() || null,
  };
}

/** A genuine partial PATCH, same diff-only convention as the other
 * Client CRM update-patch helpers. Unlike ClientContact, `crm_contact_id`
 * IS diffed and sent here -- linking an unresolved participant to a
 * canonical Contact is the explicit unresolved-to-resolved transition
 * this stage's approved design requires; the backend then refreshes the
 * identity snapshot from that Contact itself, so identity fields are only
 * ever diffed/sent here while the participant remains unresolved. */
export function engagementParticipantUpdatePatch(
  form: EngagementParticipantFormState,
  original: EngagementParticipant
): EngagementParticipantUpdateInput {
  const patch: EngagementParticipantUpdateInput = {};

  if (form.crmContactId !== original.crm_contact_id) patch.crm_contact_id = form.crmContactId;

  const stillUnresolved = form.crmContactId === null;
  if (stillUnresolved) {
    const firstName = form.firstName.trim() || null;
    if (firstName !== original.first_name) patch.first_name = firstName;
    const lastName = form.lastName.trim() || null;
    if (lastName !== original.last_name) patch.last_name = lastName;
    const email = form.email.trim() || null;
    if (email !== original.email) patch.email = email;
    const title = form.title.trim() || null;
    if (title !== original.title) patch.title = title;
    const company = form.company.trim() || null;
    if (company !== original.company) patch.company = company;
  }

  if (form.role !== original.role) patch.role = form.role;
  const rsvpStatus = form.rsvpStatus || null;
  if (rsvpStatus !== original.rsvp_status) patch.rsvp_status = rsvpStatus;
  const attendanceStatus = form.attendanceStatus || null;
  if (attendanceStatus !== original.attendance_status) patch.attendance_status = attendanceStatus;
  if (form.isWalkIn !== original.is_walk_in) patch.is_walk_in = form.isWalkIn;

  return patch;
}

// --- ClientTouchpoint (Stage 2A backend, Stage 2B frontend) -----------------
// A persistent, structured record of one communication/interaction with a
// Client -- see ClientTouchpoint's own model docstring on the backend.
// `occurred_at` is a full datetime but the V1 UI only ever picks a
// calendar day, so it uses the exact same UTC-midnight round-trip
// convention as EngagementCloseout.completed_at above
// (completedAtIsoFromFormValue) -- the displayed day never shifts because
// of the viewer's own browser timezone.

export const CONTACT_TYPE_OPTIONS: { value: ContactType; label: string }[] = [
  { value: "email", label: "Email" },
  { value: "call", label: "Call" },
  { value: "slack", label: "Slack" },
  { value: "linkedin", label: "LinkedIn" },
  { value: "in_person", label: "In-person" },
];

export function contactTypeLabel(value: ContactType): string {
  return CONTACT_TYPE_OPTIONS.find((o) => o.value === value)?.label ?? value;
}

/** Curated V1 "Contacted By" roster -- confirmed directly with Astronomic,
 * not derived from any existing data (Client.owner/Engagement.owner/
 * EngagementCloseout.completed_by have no real values populated anywhere
 * in this app yet, and no Staff/User model exists). Kept as one small
 * constant, separate from the backend's free-text field, so the roster
 * can change later without touching the model. */
export const CONTACTED_BY_OPTIONS: string[] = ["Chris", "John", "Karla", "Ria"];

/** Whether `value` is one of the curated names above (exact match, no
 * case-folding -- the stored value is never normalized). Used by the form
 * to decide whether to show the curated dropdown's own selection or fall
 * back to the free-text "Other" field -- an existing stored value outside
 * the curated list (e.g. someone removed from the roster later) must
 * still render/preserve correctly via that fallback, never silently
 * dropped. */
export function isCuratedContactedBy(value: string): boolean {
  return CONTACTED_BY_OPTIONS.includes(value);
}

export interface TouchpointFormState {
  occurredAt: string; // "YYYY-MM-DD"
  contactType: ContactType;
  crmContactId: string | null;
  contactedBy: string;
  note: string;
}

/** Today's calendar date in the viewer's OWN local timezone, as a plain
 * "YYYY-MM-DD" -- deliberately not `new Date().toISOString().slice(0,10)`,
 * which would read back yesterday's or tomorrow's date depending on the
 * viewer's timezone/time-of-day (the same off-by-one this whole feature
 * exists to avoid). */
function todayLocalDateInputValue(): string {
  const now = new Date();
  const year = now.getFullYear();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

export function emptyTouchpointFormState(): TouchpointFormState {
  return {
    occurredAt: todayLocalDateInputValue(),
    contactType: "email",
    crmContactId: null,
    contactedBy: "",
    note: "",
  };
}

export function touchpointFormStateFromTouchpoint(touchpoint: ClientTouchpoint): TouchpointFormState {
  return {
    occurredAt: touchpoint.occurred_at.slice(0, 10),
    contactType: touchpoint.contact_type,
    crmContactId: touchpoint.crm_contact_id,
    contactedBy: touchpoint.contacted_by ?? "",
    note: touchpoint.note ?? "",
  };
}

/** Parses a plain "YYYY-MM-DD" as UTC midnight -- same fixed-point
 * convention as completedAtIsoFromFormValue, so the calendar day the
 * operator picked round-trips exactly (create -> reload -> edit) no
 * matter what timezone the viewing browser is in. */
function touchpointOccurredAtIsoFromFormValue(value: string): string {
  return new Date(`${value}T00:00:00Z`).toISOString();
}

/** CREATE only. Contact Type and Contacted By are both required by the
 * form (canSave gates on them); occurred_at is always sent explicitly
 * (never left to the backend's own now()-default) since the operator
 * picked a specific calendar day. */
export function touchpointCreatePayload(form: TouchpointFormState): ClientTouchpointCreateInput {
  return {
    crm_contact_id: form.crmContactId,
    occurred_at: touchpointOccurredAtIsoFromFormValue(form.occurredAt),
    contact_type: form.contactType,
    contacted_by: form.contactedBy.trim(),
    note: form.note.trim() || null,
  };
}

/** A genuine partial PATCH, same diff-only convention as the other Client
 * CRM update-patch helpers. Clearing the Contact sends an explicit
 * `crm_contact_id: null` (the backend then clears contact_name too) --
 * never just omitted. */
export function touchpointUpdatePatch(form: TouchpointFormState, original: ClientTouchpoint): ClientTouchpointUpdateInput {
  const patch: ClientTouchpointUpdateInput = {};

  const originalDate = original.occurred_at.slice(0, 10);
  if (form.occurredAt !== originalDate) patch.occurred_at = touchpointOccurredAtIsoFromFormValue(form.occurredAt);

  if (form.contactType !== original.contact_type) patch.contact_type = form.contactType;

  if (form.crmContactId !== original.crm_contact_id) patch.crm_contact_id = form.crmContactId;

  const contactedBy = form.contactedBy.trim();
  if (contactedBy !== (original.contacted_by ?? "")) patch.contacted_by = contactedBy;

  const note = form.note.trim() || null;
  if (note !== original.note) patch.note = note;

  return patch;
}

/** The newest active Touchpoint IS Last Contact for V1 -- no derived
 * field is stored anywhere (see this stage's own approved design: "Do NOT
 * create duplicate Last Contact fields on Client"). The backend already
 * returns active Touchpoints newest-first by default, so this is a
 * defensive `!archived` filter, not a re-sort -- if archived rows ever
 * reach this list (e.g. a caller passed include_archived), they're
 * excluded rather than trusted to sort correctly. */
export function latestActiveTouchpoint(touchpoints: ClientTouchpoint[]): ClientTouchpoint | null {
  return touchpoints.find((t) => !t.archived) ?? null;
}
