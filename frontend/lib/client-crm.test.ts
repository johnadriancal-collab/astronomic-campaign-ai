import assert from "node:assert/strict";
import { test } from "node:test";
import {
  buildClientListQueryParams,
  clientContactCreatePayload,
  clientContactDisplayName,
  clientContactFormStateFromContact,
  clientContactUpdatePatch,
  clientCreatePayload,
  clientFormStateFromClient,
  clientRelationshipClassificationBadgeClass,
  clientRelationshipClassificationLabel,
  clientStatusBadgeClass,
  clientStatusLabel,
  clientUpdatePatch,
  defaultClientListFilters,
  dinnerProgramLabel,
  emptyClientContactFormState,
  emptyClientFormState,
  emptyEngagementFormState,
  engagementCreatePayload,
  engagementFormStateFromEngagement,
  engagementStatusLabel,
  engagementTypeLabel,
  engagementUpdatePatch,
  formatClientDate,
  formatEngagementDate,
  isClientFormValid,
  isDinnerShapedEngagementType,
  isEngagementFormValid,
} from "./client-crm.ts";
import type { Client, ClientContact, Engagement } from "./api.ts";

function makeClient(overrides: Partial<Client> = {}): Client {
  return {
    client_id: "c1",
    name: "Hive ASMBLD",
    website: null,
    industry: null,
    status: "active",
    relationship_classification: null,
    owner: null,
    next_action: null,
    next_action_due: null,
    created_at: "2026-09-01T00:00:00Z",
    updated_at: "2026-09-01T00:00:00Z",
    archived: false,
    ...overrides,
  };
}

// --- Status / relationship rendering ---------------------------------------

test("clientStatusLabel maps every status", () => {
  assert.equal(clientStatusLabel("active"), "Active");
  assert.equal(clientStatusLabel("inactive"), "Inactive");
});

test("clientRelationshipClassificationLabel renders null as a plain em-dash, never Nurture", () => {
  assert.equal(clientRelationshipClassificationLabel(null), "—");
});

test("clientRelationshipClassificationLabel maps every real value", () => {
  assert.equal(clientRelationshipClassificationLabel("nurture"), "Nurture");
  assert.equal(clientRelationshipClassificationLabel("opportunity"), "Opportunity");
  assert.equal(clientRelationshipClassificationLabel("referral"), "Referral");
  assert.equal(clientRelationshipClassificationLabel("needs_attention"), "Needs Attention");
  assert.equal(clientRelationshipClassificationLabel("closed_inactive"), "Closed / Inactive");
});

test("status and relationship classification never share a badge-class implementation", () => {
  // Status and classification are separate concepts (per this stage's own
  // instruction) -- confirmed structurally: the two badge-class functions
  // never resolve to literally the same lookup table/logic path.
  assert.notEqual(clientStatusBadgeClass, clientRelationshipClassificationBadgeClass as unknown as typeof clientStatusBadgeClass);
});

test("clientStatusBadgeClass gives active a calm/muted treatment and inactive a noticeable one", () => {
  assert.match(clientStatusBadgeClass("active"), /secondary|muted/);
  assert.notEqual(clientStatusBadgeClass("active"), clientStatusBadgeClass("inactive"));
});

// --- Date formatting ---------------------------------------------------

test("formatClientDate renders a plain em-dash for null", () => {
  assert.equal(formatClientDate(null), "—");
});

test("formatClientDate renders a date-only value without inventing a time-of-day", () => {
  assert.equal(formatClientDate("2027-01-15"), "Jan 15, 2027");
});

test("formatClientDate renders a full ISO timestamp using its own UTC date, not the local browser timezone", () => {
  assert.equal(formatClientDate("2026-09-01T00:00:00Z"), "Sep 1, 2026");
});

// --- List query construction -----------------------------------------------

test("buildClientListQueryParams omits every empty filter", () => {
  const params = buildClientListQueryParams(defaultClientListFilters());
  assert.equal(params.q, undefined);
  assert.equal(params.status, undefined);
  assert.equal(params.relationship_classification, undefined);
  assert.equal(params.owner, undefined);
  assert.equal(params.include_archived, false);
});

test("buildClientListQueryParams maps every real filter to the backend's own param names", () => {
  const params = buildClientListQueryParams({
    q: "hive",
    status: "active",
    relationshipClassification: "opportunity",
    owner: "chris",
    includeArchived: true,
    sortBy: "next_action_due",
    sortDir: "desc",
    page: 2,
    pageSize: 10,
  });
  assert.deepEqual(params, {
    q: "hive",
    status: "active",
    relationship_classification: "opportunity",
    owner: "chris",
    include_archived: true,
    sort_by: "next_action_due",
    sort_dir: "desc",
    page: 2,
    page_size: 10,
  });
});

test("buildClientListQueryParams trims whitespace out of q/owner", () => {
  const params = buildClientListQueryParams({ ...defaultClientListFilters(), q: "  hive  ", owner: "  chris  " });
  assert.equal(params.q, "hive");
  assert.equal(params.owner, "chris");
});

// --- Create payload ----------------------------------------------------

test("clientCreatePayload requires only a name, everything else null", () => {
  const payload = clientCreatePayload(emptyClientFormState());
  assert.equal(payload.website, null);
  assert.equal(payload.industry, null);
  assert.equal(payload.relationship_classification, null);
  assert.equal(payload.owner, null);
  assert.equal(payload.next_action, null);
  assert.equal(payload.next_action_due, null);
  assert.equal(payload.status, "active");
});

test("clientCreatePayload trims the name", () => {
  const payload = clientCreatePayload({ ...emptyClientFormState(), name: "  Hive ASMBLD  " });
  assert.equal(payload.name, "Hive ASMBLD");
});

test("clientCreatePayload carries every field through when filled in", () => {
  const payload = clientCreatePayload({
    name: "Acme Co",
    website: "https://acme.example.com",
    industry: "Venture",
    status: "inactive",
    relationshipClassification: "opportunity",
    owner: "Chris",
    nextAction: "Schedule Q1 check-in",
    nextActionDue: "2027-01-15",
  });
  assert.deepEqual(payload, {
    name: "Acme Co",
    website: "https://acme.example.com",
    industry: "Venture",
    status: "inactive",
    relationship_classification: "opportunity",
    owner: "Chris",
    next_action: "Schedule Q1 check-in",
    next_action_due: "2027-01-15",
  });
});

test("isClientFormValid requires a non-blank name", () => {
  assert.equal(isClientFormValid(emptyClientFormState()), false);
  assert.equal(isClientFormValid({ ...emptyClientFormState(), name: "   " }), false);
  assert.equal(isClientFormValid({ ...emptyClientFormState(), name: "Hive ASMBLD" }), true);
});

// --- Partial edit payload ------------------------------------------------

test("clientUpdatePatch is empty when nothing changed", () => {
  const client = makeClient({ industry: "Venture" });
  const form = clientFormStateFromClient(client);
  assert.deepEqual(clientUpdatePatch(form, client), {});
});

test("clientUpdatePatch includes ONLY the field that actually changed", () => {
  const client = makeClient({ industry: null });
  const form = clientFormStateFromClient(client);
  form.industry = "Venture";
  assert.deepEqual(clientUpdatePatch(form, client), { industry: "Venture" });
});

test("clientUpdatePatch never includes client_id/created_at/updated_at -- ClientFormState carries none of them", () => {
  const client = makeClient();
  const form = clientFormStateFromClient(client);
  form.name = "Renamed Co";
  const patch = clientUpdatePatch(form, client);
  assert.ok(!("client_id" in patch));
  assert.ok(!("created_at" in patch));
  assert.ok(!("updated_at" in patch));
});

test("clientUpdatePatch can clear a previously-set optional field back to null", () => {
  const client = makeClient({ owner: "Chris" });
  const form = clientFormStateFromClient(client);
  form.owner = "";
  assert.deepEqual(clientUpdatePatch(form, client), { owner: null });
});

test("clientUpdatePatch can clear the relationship classification back to unassessed", () => {
  const client = makeClient({ relationship_classification: "opportunity" });
  const form = clientFormStateFromClient(client);
  form.relationshipClassification = "";
  assert.deepEqual(clientUpdatePatch(form, client), { relationship_classification: null });
});

test("clientUpdatePatch reflects multiple simultaneous changes", () => {
  const client = makeClient({ status: "active", owner: null });
  const form = clientFormStateFromClient(client);
  form.status = "inactive";
  form.owner = "Ria";
  assert.deepEqual(clientUpdatePatch(form, client), { status: "inactive", owner: "Ria" });
});

// --- ClientContact (Stage 1D) -------------------------------------------

function makeClientContact(overrides: Partial<ClientContact> = {}): ClientContact {
  return {
    client_contact_id: "cc1",
    client_id: "c1",
    crm_contact_id: "crm-1",
    first_name: "Ethan",
    last_name: "Wong",
    email: "ethan@hiveasmbld.example.com",
    phone: null,
    title: "Co-CEO",
    is_primary_contact: false,
    is_decision_maker: false,
    role_notes: null,
    created_at: "2026-09-01T00:00:00Z",
    updated_at: "2026-09-01T00:00:00Z",
    archived: false,
    ...overrides,
  };
}

test("clientContactDisplayName joins first/last name", () => {
  assert.equal(clientContactDisplayName(makeClientContact()), "Ethan Wong");
});

test("clientContactDisplayName falls back to Unnamed contact when both names are missing", () => {
  assert.equal(clientContactDisplayName(makeClientContact({ first_name: null, last_name: null })), "Unnamed contact");
});

test("emptyClientContactFormState defaults to no title, not primary, not decision maker", () => {
  const form = emptyClientContactFormState();
  assert.equal(form.title, "");
  assert.equal(form.isPrimaryContact, false);
  assert.equal(form.isDecisionMaker, false);
});

test("clientContactCreatePayload carries the picked crm_contact_id and form fields", () => {
  const form = { title: "VP of BD", isPrimaryContact: true, isDecisionMaker: true, roleNotes: "Warm intro" };
  const payload = clientContactCreatePayload(form, "crm-42");
  assert.deepEqual(payload, {
    crm_contact_id: "crm-42",
    title: "VP of BD",
    is_primary_contact: true,
    is_decision_maker: true,
    role_notes: "Warm intro",
  });
});

test("clientContactCreatePayload defaults blank optional fields to null", () => {
  const payload = clientContactCreatePayload(emptyClientContactFormState(), "crm-42");
  assert.equal(payload.title, null);
  assert.equal(payload.role_notes, null);
  assert.equal(payload.is_primary_contact, false);
});

test("clientContactUpdatePatch is empty when nothing changed", () => {
  const contact = makeClientContact({ title: "Co-CEO" });
  const form = clientContactFormStateFromContact(contact);
  assert.deepEqual(clientContactUpdatePatch(form, contact), {});
});

test("clientContactUpdatePatch includes only the field that actually changed", () => {
  const contact = makeClientContact({ role_notes: null });
  const form = clientContactFormStateFromContact(contact);
  form.roleNotes = "Introduced us to the CFO";
  assert.deepEqual(clientContactUpdatePatch(form, contact), { role_notes: "Introduced us to the CFO" });
});

test("clientContactUpdatePatch never includes client_contact_id/crm_contact_id/client_id -- the form state carries none of them", () => {
  const contact = makeClientContact();
  const form = clientContactFormStateFromContact(contact);
  form.title = "New Title";
  const patch = clientContactUpdatePatch(form, contact);
  assert.ok(!("client_contact_id" in patch));
  assert.ok(!("crm_contact_id" in patch));
  assert.ok(!("client_id" in patch));
});

test("clientContactUpdatePatch reflects switching is_primary_contact", () => {
  const contact = makeClientContact({ is_primary_contact: false });
  const form = clientContactFormStateFromContact(contact);
  form.isPrimaryContact = true;
  assert.deepEqual(clientContactUpdatePatch(form, contact), { is_primary_contact: true });
});

// --- Engagement (Stage 1E) -----------------------------------------------

function makeEngagement(overrides: Partial<Engagement> = {}): Engagement {
  return {
    engagement_id: "e1",
    client_id: "c1",
    title: "SF Investor Dinner",
    engagement_type: "investor_dinner",
    dinner_program: null,
    engagement_date: "2026-09-22",
    location: "San Francisco",
    status: "planned",
    owner: null,
    fee: null,
    contract_status: "not_sent",
    contract_url: null,
    signed_date: null,
    payment_status: "unpaid",
    luma_event_id: null,
    created_at: "2026-09-01T00:00:00Z",
    updated_at: "2026-09-01T00:00:00Z",
    archived: false,
    ...overrides,
  };
}

test("engagementTypeLabel maps every type", () => {
  assert.equal(engagementTypeLabel("investor_dinner"), "Investor Dinner");
  assert.equal(engagementTypeLabel("customer_dinner"), "Customer Dinner");
  assert.equal(engagementTypeLabel("sponsorship"), "Sponsorship");
  assert.equal(engagementTypeLabel("other"), "Other");
});

test("isDinnerShapedEngagementType is true only for investor/customer dinner", () => {
  assert.equal(isDinnerShapedEngagementType("investor_dinner"), true);
  assert.equal(isDinnerShapedEngagementType("customer_dinner"), true);
  assert.equal(isDinnerShapedEngagementType("sponsorship"), false);
  assert.equal(isDinnerShapedEngagementType("other"), false);
});

test("dinnerProgramLabel renders null as a plain em-dash", () => {
  assert.equal(dinnerProgramLabel(null), "—");
});

test("dinnerProgramLabel maps every real value", () => {
  assert.equal(dinnerProgramLabel("supernova"), "Supernova");
  assert.equal(dinnerProgramLabel("galaxy"), "Galaxy");
  assert.equal(dinnerProgramLabel("aurora"), "Aurora");
  assert.equal(dinnerProgramLabel("other"), "Other");
});

test("engagementStatusLabel maps every status, including the new CONFIRMED", () => {
  assert.equal(engagementStatusLabel("planned"), "Planned");
  assert.equal(engagementStatusLabel("confirmed"), "Confirmed");
  assert.equal(engagementStatusLabel("completed"), "Completed");
  assert.equal(engagementStatusLabel("cancelled"), "Cancelled");
});

test("formatEngagementDate renders a plain em-dash for null", () => {
  assert.equal(formatEngagementDate(null), "—");
});

test("formatEngagementDate renders a date-only value without inventing a time-of-day", () => {
  assert.equal(formatEngagementDate("2026-09-22"), "Sep 22, 2026");
});

test("emptyEngagementFormState defaults to planned/investor_dinner/not_sent/unpaid", () => {
  const form = emptyEngagementFormState();
  assert.equal(form.status, "planned");
  assert.equal(form.engagementType, "investor_dinner");
  assert.equal(form.contractStatus, "not_sent");
  assert.equal(form.paymentStatus, "unpaid");
  assert.equal(form.dinnerProgram, "");
});

test("isEngagementFormValid requires a non-blank title", () => {
  assert.equal(isEngagementFormValid(emptyEngagementFormState()), false);
  assert.equal(isEngagementFormValid({ ...emptyEngagementFormState(), title: "   " }), false);
  assert.equal(isEngagementFormValid({ ...emptyEngagementFormState(), title: "SF Dinner" }), true);
});

test("engagementCreatePayload carries every field through when filled in", () => {
  const payload = engagementCreatePayload({
    title: "SF Investor Dinner",
    engagementType: "investor_dinner",
    dinnerProgram: "supernova",
    engagementDate: "2026-09-22",
    location: "The Battery, San Francisco",
    status: "confirmed",
    owner: "Chris",
    fee: "5000",
    contractStatus: "signed",
    contractUrl: "https://drive.example.com/contract",
    signedDate: "2026-09-01",
    paymentStatus: "partial",
  });
  assert.deepEqual(payload, {
    title: "SF Investor Dinner",
    engagement_type: "investor_dinner",
    dinner_program: "supernova",
    engagement_date: "2026-09-22",
    location: "The Battery, San Francisco",
    status: "confirmed",
    owner: "Chris",
    fee: 5000,
    contract_status: "signed",
    contract_url: "https://drive.example.com/contract",
    signed_date: "2026-09-01",
    payment_status: "partial",
  });
});

test("engagementCreatePayload clears dinner_program for a non-dinner-shaped engagement_type even if the form still holds one", () => {
  const payload = engagementCreatePayload({
    ...emptyEngagementFormState(),
    title: "Fall Sponsorship",
    engagementType: "sponsorship",
    dinnerProgram: "supernova",
  });
  assert.equal(payload.dinner_program, null);
});

test("engagementCreatePayload defaults blank optional fields to null", () => {
  const payload = engagementCreatePayload({ ...emptyEngagementFormState(), title: "SF Dinner" });
  assert.equal(payload.dinner_program, null);
  assert.equal(payload.location, null);
  assert.equal(payload.owner, null);
  assert.equal(payload.fee, null);
  assert.equal(payload.contract_url, null);
  assert.equal(payload.signed_date, null);
});

test("engagementUpdatePatch is empty when nothing changed", () => {
  const engagement = makeEngagement({ owner: "Chris" });
  const form = engagementFormStateFromEngagement(engagement);
  assert.deepEqual(engagementUpdatePatch(form, engagement), {});
});

test("engagementUpdatePatch includes only the field that actually changed", () => {
  const engagement = makeEngagement({ owner: null });
  const form = engagementFormStateFromEngagement(engagement);
  form.owner = "Chris";
  assert.deepEqual(engagementUpdatePatch(form, engagement), { owner: "Chris" });
});

test("engagementUpdatePatch never includes engagement_id/client_id/created_at/updated_at", () => {
  const engagement = makeEngagement();
  const form = engagementFormStateFromEngagement(engagement);
  form.title = "Renamed Dinner";
  const patch = engagementUpdatePatch(form, engagement);
  assert.ok(!("engagement_id" in patch));
  assert.ok(!("client_id" in patch));
  assert.ok(!("created_at" in patch));
  assert.ok(!("updated_at" in patch));
});

test("engagementUpdatePatch clears dinner_program when engagement_type changes to a non-dinner type in the same patch", () => {
  const engagement = makeEngagement({ engagement_type: "investor_dinner", dinner_program: "supernova" });
  const form = engagementFormStateFromEngagement(engagement);
  form.engagementType = "sponsorship";
  assert.deepEqual(engagementUpdatePatch(form, engagement), { engagement_type: "sponsorship", dinner_program: null });
});

test("engagementUpdatePatch reflects the new CONFIRMED status", () => {
  const engagement = makeEngagement({ status: "planned" });
  const form = engagementFormStateFromEngagement(engagement);
  form.status = "confirmed";
  assert.deepEqual(engagementUpdatePatch(form, engagement), { status: "confirmed" });
});

test("engagementUpdatePatch reflects a fee change", () => {
  const engagement = makeEngagement({ fee: null });
  const form = engagementFormStateFromEngagement(engagement);
  form.fee = "7500";
  assert.deepEqual(engagementUpdatePatch(form, engagement), { fee: 7500 });
});
