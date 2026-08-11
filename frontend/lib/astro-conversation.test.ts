import { test } from "node:test";
import assert from "node:assert/strict";
import {
  applyAstroResponse,
  composeAstroMessage,
  contextForRequest,
  INITIAL_ASTRO_CONVERSATION_STATE,
  type AstroConversationState,
} from "./astro-conversation.ts";
import type { AstroCommandResponse, CrmContact } from "./api.ts";

function contact(id: string): CrmContact {
  return { crm_contact_id: id } as CrmContact;
}

function response(overrides: Partial<AstroCommandResponse>): AstroCommandResponse {
  return {
    intent: "search_contacts",
    understood_as: "",
    query: { filters: [], logic: "AND" },
    total: 0,
    contacts: null,
    operation: null,
    changed_field: null,
    message: null,
    understood: null,
    unresolved_phrase: null,
    ...overrides,
  };
}

// --- composeAstroMessage ---

test("composeAstroMessage prefers the backend's deterministic message when present", () => {
  const r = response({ message: "Showing 89 contacts in Austin. Your other filters are unchanged." });
  assert.equal(composeAstroMessage(r), "Showing 89 contacts in Austin. Your other filters are unchanged.");
});

test("composeAstroMessage synthesizes a standalone search message when the backend leaves message null", () => {
  const r = response({ intent: "search_contacts", total: 127, message: null });
  assert.equal(composeAstroMessage(r), "Found 127 contacts.");
});

test("composeAstroMessage synthesizes a singular contact correctly", () => {
  const r = response({ intent: "search_contacts", total: 1, message: null });
  assert.equal(composeAstroMessage(r), "Found 1 contact.");
});

test("composeAstroMessage synthesizes a standalone count message when the backend leaves message null", () => {
  const r = response({ intent: "count_contacts", total: 14, message: null });
  assert.equal(composeAstroMessage(r), "14 contacts match.");
});

// --- applyAstroResponse: standalone / refinement search ---

test("a resolved search_contacts response replaces context, contacts, and total", () => {
  const next = applyAstroResponse(INITIAL_ASTRO_CONVERSATION_STATE, response({
    intent: "search_contacts",
    query: { filters: [{ field: "state", operator: "eq", value: "Texas" }], logic: "AND" },
    total: 127,
    contacts: [contact("a"), contact("b")],
  }));
  assert.equal(next.total, 127);
  assert.deepEqual(next.contacts?.map((c) => c.crm_contact_id), ["a", "b"]);
  assert.deepEqual(next.context, {
    query: { filters: [{ field: "state", operator: "eq", value: "Texas" }], logic: "AND" },
    intent: "search_contacts",
  });
});

test("a null contacts array on a search_contacts response is treated as empty, not left over from before", () => {
  const prior: AstroConversationState = { context: null, contacts: [contact("stale")], total: 5 };
  const next = applyAstroResponse(prior, response({ intent: "search_contacts", total: 0, contacts: null }));
  assert.deepEqual(next.contacts, []);
});

// --- applyAstroResponse: count_contacts keeps prior visible results ---

test("a count_contacts response advances context/total but keeps the previously visible contacts", () => {
  const prior: AstroConversationState = {
    context: { query: { filters: [], logic: "AND" }, intent: "search_contacts" },
    contacts: [contact("a"), contact("b")],
    total: 14,
  };
  const next = applyAstroResponse(prior, response({ intent: "count_contacts", total: 14 }));
  assert.deepEqual(next.contacts?.map((c) => c.crm_contact_id), ["a", "b"]); // unchanged, not cleared
  assert.equal(next.total, 14);
  assert.equal(next.context?.intent, "count_contacts"); // advances so the NEXT turn refines correctly
});

test("a search_contacts response after a count turn ('Show them again') refreshes the visible contacts", () => {
  const afterCount: AstroConversationState = {
    context: { query: { filters: [], logic: "AND" }, intent: "count_contacts" },
    contacts: [contact("a"), contact("b")],
    total: 14,
  };
  const next = applyAstroResponse(afterCount, response({
    intent: "search_contacts",
    total: 14,
    contacts: [contact("a"), contact("b")],
  }));
  assert.equal(next.context?.intent, "search_contacts");
  assert.deepEqual(next.contacts?.map((c) => c.crm_contact_id), ["a", "b"]);
});

// --- applyAstroResponse: unresolved is a true no-op ---

test("an unresolved response leaves context, contacts, and total completely untouched", () => {
  const prior: AstroConversationState = {
    context: { query: { filters: [{ field: "city", operator: "eq", value: "Austin" }], logic: "AND" }, intent: "search_contacts" },
    contacts: [contact("a")],
    total: 1,
  };
  const next = applyAstroResponse(prior, response({
    intent: "unresolved",
    query: prior.context!.query,
    total: 1,
    contacts: null,
    message: "I don't know what you mean by 'good ones'.",
    unresolved_phrase: "good ones",
  }));
  assert.deepEqual(next, prior);
});

// --- applyAstroResponse: reset ---

test("a reset response (operation: reset, empty filters) is handled as a normal search_contacts replace", () => {
  const prior: AstroConversationState = {
    context: { query: { filters: [{ field: "state", operator: "eq", value: "Texas" }], logic: "AND" }, intent: "search_contacts" },
    contacts: [contact("a")],
    total: 1,
  };
  const next = applyAstroResponse(prior, response({
    intent: "search_contacts",
    operation: "reset",
    query: { filters: [], logic: "AND" },
    total: 5,
    contacts: [contact("a"), contact("b"), contact("c"), contact("d"), contact("e")],
  }));
  assert.deepEqual(next.context?.query, { filters: [], logic: "AND" });
  assert.equal(next.total, 5);
  assert.equal(next.contacts?.length, 5);
});

// --- contextForRequest ---

test("contextForRequest is undefined before any turn has resolved (first request has no context field)", () => {
  assert.equal(contextForRequest(INITIAL_ASTRO_CONVERSATION_STATE), undefined);
});

test("contextForRequest returns the stored context once a query exists, even with empty filters (post-reset)", () => {
  const state: AstroConversationState = {
    context: { query: { filters: [], logic: "AND" }, intent: "search_contacts" },
    contacts: [],
    total: 5,
  };
  assert.deepEqual(contextForRequest(state), state.context);
});

test("contextForRequest is never derived from an unresolved response (integration of the no-op rule)", () => {
  const prior: AstroConversationState = {
    context: { query: { filters: [], logic: "AND" }, intent: "search_contacts" },
    contacts: [contact("a")],
    total: 1,
  };
  const afterUnresolved = applyAstroResponse(prior, response({ intent: "unresolved", query: prior.context!.query, total: 1 }));
  assert.deepEqual(contextForRequest(afterUnresolved), prior.context);
});
