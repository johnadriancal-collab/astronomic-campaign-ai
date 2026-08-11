// Shared "fetch every contact matching a FilterQuery" + "resolve selected ids
// to full contact objects, fetching the complete matching set only when the
// selection reaches outside whatever's already in memory" -- used by both
// More Filters (/crm/filters) and Astro Search (/crm/astro) so "Select all N
// matching"/"Export all matching"/"Export selected across pages" share one
// implementation instead of each page inventing its own.
//
// The Contacts page (/crm) doesn't need this: it already holds every
// matching contact in memory at all times (see app/crm/page.tsx's load()),
// so there's nothing to fetch on demand there.
//
// Normal browsing stays server-paginated -- these only run at the moment
// the user actually asks to select/export beyond the current page, never on
// every keystroke or page load.

import type { CrmContact, CrmContactPage, FilterQuery } from "./api";

export type QueryContactsFn = (query: FilterQuery) => Promise<CrmContactPage>;

// Two-step probe, the same pattern the Contacts page's load() already uses:
// ask for just the count first (page_size: 1, cheap), then re-fetch with
// page_size set to the exact total so every matching contact comes back in
// one page, however large the result set is. The query's own page/page_size
// are ignored -- this always fetches the COMPLETE matching set for the
// query's filters/logic, not whatever page the caller happened to be on.
export async function fetchAllMatchingContacts(query: FilterQuery, queryContacts: QueryContactsFn): Promise<CrmContact[]> {
  const probe = await queryContacts({ ...query, page: 1, page_size: 1 });
  if (probe.total <= probe.items.length) return probe.items;
  const full = await queryContacts({ ...query, page: 1, page_size: probe.total });
  return full.items;
}

// Resolves every selected id to its full CrmContact using whatever's already
// known (the current page's rendered contacts, or a previously-fetched full
// matching set) -- only reaching for fetchAllMatchingContacts when a selected
// id isn't among `knownContacts` (i.e. the selection includes ids beyond
// what's currently loaded, such as after "Select all N matching" selected
// ids from pages never fetched, or a hand-picked selection spanning pages).
// Preserves `knownContacts`'/the fetched set's own order (not Set insertion
// order), matching the Contacts page's existing `allContacts.filter(...)`
// export convention.
export async function resolveContactsForExport(
  selected: Set<string>,
  knownContacts: CrmContact[],
  query: FilterQuery,
  queryContacts: QueryContactsFn
): Promise<CrmContact[]> {
  const known = new Set(knownContacts.map((c) => c.crm_contact_id));
  const needsFullFetch = [...selected].some((id) => !known.has(id));
  const pool = needsFullFetch ? await fetchAllMatchingContacts(query, queryContacts) : knownContacts;
  return pool.filter((c) => selected.has(c.crm_contact_id));
}
