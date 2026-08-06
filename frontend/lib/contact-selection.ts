// Pure Set-based helpers for the CRM contacts bulk-selection UI -- kept
// separate from the page component so the selection rules (individual
// toggle, select-all-on-page with indeterminate detection, select-all-
// matching, clear) are unit-testable without rendering React. Selection is
// always just a Set<string> of crm_contact_id -- a plain client-side value
// that survives paging through the already-fully-loaded result set fine on
// its own; the page component is responsible for clearing it when the
// underlying filtered result set changes (a new search/filter fetch), not
// this module.

export function toggleOne(selected: Set<string>, id: string): Set<string> {
  const next = new Set(selected);
  if (next.has(id)) {
    next.delete(id);
  } else {
    next.add(id);
  }
  return next;
}

export function isPageFullySelected(selected: Set<string>, pageIds: string[]): boolean {
  return pageIds.length > 0 && pageIds.every((id) => selected.has(id));
}

export function isPagePartiallySelected(selected: Set<string>, pageIds: string[]): boolean {
  const selectedOnPage = pageIds.filter((id) => selected.has(id)).length;
  return selectedOnPage > 0 && selectedOnPage < pageIds.length;
}

// Toggles the CURRENT page's ids only: selects every id on the page unless the page is
// already fully selected, in which case it deselects just those ids -- selections
// belonging to other pages (from a prior "select all on this page") are untouched.
export function toggleSelectAllOnPage(selected: Set<string>, pageIds: string[]): Set<string> {
  const next = new Set(selected);
  if (isPageFullySelected(selected, pageIds)) {
    for (const id of pageIds) next.delete(id);
  } else {
    for (const id of pageIds) next.add(id);
  }
  return next;
}

// Replaces the selection wholesale with every id currently matching the active
// search/filter (across every page) -- deliberately NOT a union with the existing
// selection, so a stale selection from before "select all matching" never lingers.
export function selectAllMatching(matchingIds: string[]): Set<string> {
  return new Set(matchingIds);
}

export function clearSelection(): Set<string> {
  return new Set();
}
