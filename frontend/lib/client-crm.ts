// Pure logic for Client CRM (Stage 1C) -- kept separate from page/
// component code so it's unit-testable without rendering React, same
// split as lib/mail.ts and lib/mail-trigger.ts. Client is Astronomic's
// relationship/sales record for organizations -- distinct from CrmContact
// (people/prospects) and distinct from a "dinner" (Engagement, not built
// yet) -- see app/models/client_crm.py's own docstring on the backend.

import type {
  Client,
  ClientContact,
  ClientContactCreateInput,
  ClientContactUpdateInput,
  ClientCreateInput,
  ClientRelationshipClassification,
  ClientStatus,
  ClientUpdateInput,
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
