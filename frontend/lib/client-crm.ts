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
  ClientUpdateInput,
  DinnerType,
  Engagement,
  EngagementContractStatus,
  EngagementCreateInput,
  EngagementPaymentStatus,
  EngagementStatus,
  EngagementType,
  EngagementUpdateInput,
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
  sortBy: "name" | "created_at" | "updated_at" | "next_action_due";
  sortDir: "asc" | "desc";
  page: number;
  pageSize: number;
}

export function defaultClientListFilters(): ClientListFilters {
  return {
    q: "",
    status: "",
    relationshipClassification: "",
    owner: "",
    includeArchived: false,
    sortBy: "name",
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
