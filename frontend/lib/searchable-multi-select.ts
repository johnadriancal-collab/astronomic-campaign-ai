// Pure helpers for SearchableMultiSelect (see
// components/searchable-multi-select.tsx) -- filtering, selection, and
// keyboard-navigation math kept out of the component so they're
// unit-testable without a DOM/render harness, matching this repo's own
// convention (see lib/tag-multi-select.ts for the same split applied to
// the open-ended tag editor).

// Case-insensitive substring match against every APPROVED option that
// isn't already selected -- an already-selected option is never offered
// again, which is what makes a duplicate selection impossible through this
// list (see selectOption's own belt-and-suspenders guard below). An empty
// query returns every still-available option, unfiltered -- "clicking
// opens the list" needs a real list to show before anyone types anything.
export function filterOptions(options: readonly string[], query: string, selected: readonly string[]): string[] {
  const available = options.filter((option) => !selected.includes(option));
  const q = query.trim().toLowerCase();
  if (!q) return available;
  return available.filter((option) => option.toLowerCase().includes(q));
}

// Adds `option` only if it is BOTH an exact member of the approved list and
// not already selected -- this is what makes it structurally impossible for
// arbitrary typed text (which will never exactly match an option that
// wasn't already offered) to become a value. Returns the SAME array
// reference when nothing changes, matching addTagValue's own no-op
// contract in lib/tag-multi-select.ts.
export function selectOption(current: readonly string[], option: string, options: readonly string[]): string[] {
  if (!options.includes(option)) return current as string[];
  if (current.includes(option)) return current as string[];
  return [...current, option];
}

// Removing a value is unrestricted -- this is the one operation that must
// work identically for a canonical value AND a legacy/noncanonical value
// already on the contact (see isLegacyValue below). Once removed, a legacy
// value can only come back by exact re-typing, which selectOption already
// refuses for anything not in `options`.
export function removeValue(current: readonly string[], value: string): string[] {
  return (current as string[]).filter((v) => v !== value);
}

// True for any stored value that isn't one of the approved options --
// covers every pre-existing legacy/free-text value (e.g. "Real Estate"
// before this field had a taxonomy). Used only to decide whether a chip's
// underlying value could ever be re-added via search (it can't); rendering
// itself never hides or badges these differently in V1.
export function isLegacyValue(value: string, options: readonly string[]): boolean {
  return !options.includes(value);
}

// Keyboard nav: clamps (never wraps) so ArrowDown/ArrowUp at either end of
// the filtered list simply stops, rather than cycling -- the simplest,
// least surprising behavior for a short, static option list. -1 means
// "nothing highlighted", the only valid state when the list is empty.
export function clampHighlightIndex(index: number, length: number): number {
  if (length <= 0) return -1;
  if (index < 0) return 0;
  if (index > length - 1) return length - 1;
  return index;
}

export function nextHighlightIndex(current: number, length: number): number {
  return clampHighlightIndex(current + 1, length);
}

export function previousHighlightIndex(current: number, length: number): number {
  return clampHighlightIndex(current - 1, length);
}
