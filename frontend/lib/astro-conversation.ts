// Pure reducer for the /astro Astro Search page -- kept separate from the page
// component so the backend contract rules (never store an "unresolved" intent
// as context, never clear visible results just because one turn wasn't
// understood or was count-only) are unit-testable without rendering React.
// See app/models/astro.py's AstroCommandContext for the backend side of this
// contract.

import type { AstroCommandContext, AstroCommandResponse, CrmContact } from "./api";

export interface AstroConversationState {
  context: AstroCommandContext | null;
  contacts: CrmContact[] | null;
  total: number | null;
}

export const INITIAL_ASTRO_CONVERSATION_STATE: AstroConversationState = {
  context: null,
  contacts: null,
  total: null,
};

function pluralize(count: number, word: string): string {
  return `${count} ${word}${count === 1 ? "" : "s"}`;
}

// The backend's `message` is only populated for a Phase 1.1 refinement turn
// or an unresolved one -- a plain standalone command (the usual first turn of
// a conversation) leaves it null. This composes the same deterministic,
// non-Claude wording for that case rather than leaving the chat bubble blank.
export function composeAstroMessage(response: AstroCommandResponse): string {
  if (response.message) return response.message;
  if (response.intent === "search_contacts") return `Found ${pluralize(response.total ?? 0, "contact")}.`;
  if (response.intent === "count_contacts") return `${pluralize(response.total ?? 0, "contact")} match.`;
  return response.understood_as;
}

// The backend contract (see app/api/astro.py): an "unresolved" response's own
// intent is NOT a valid context.intent, and the user must never lose their
// working list over one misunderstood sentence -- context and the visibly
// rendered contacts both carry over untouched. A "count_contacts" turn
// advances context/total (so the NEXT turn refines correctly) but deliberately
// leaves the rendered contact list alone until a search turn (e.g. "Show them
// again") explicitly refreshes it.
export function applyAstroResponse(
  state: AstroConversationState,
  response: AstroCommandResponse
): AstroConversationState {
  if (response.intent === "unresolved") return state;

  if (response.intent === "count_contacts") {
    return {
      context: { query: response.query, intent: response.intent },
      contacts: state.contacts,
      total: response.total,
    };
  }

  return {
    context: { query: response.query, intent: response.intent },
    contacts: response.contacts ?? [],
    total: response.total,
  };
}

// What to resend as `context` on the NEXT call. Undefined (rather than null)
// before any turn has resolved, so the first request matches Phase 1's exact
// standalone shape (no `context` field at all).
export function contextForRequest(state: AstroConversationState): AstroCommandContext | undefined {
  return state.context?.query ? state.context : undefined;
}

// Whether the page should clear its bulk-selection state (and any cached
// full-matching-set) after this response. A resolved search_contacts turn
// changes the visible result set -- a selection made against the PRIOR set
// no longer means anything, so it resets. count_contacts and unresolved
// turns leave the query/results exactly as they were (see applyAstroResponse
// above), so the selection stays exactly as it was too -- selecting is never
// silently dropped just because the user asked "how many are left?" or typed
// something Astro didn't understand.
export function shouldResetSelectionOnAstroResponse(response: AstroCommandResponse): boolean {
  return response.intent === "search_contacts";
}
