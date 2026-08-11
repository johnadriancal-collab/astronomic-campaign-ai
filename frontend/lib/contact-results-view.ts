// Pure helpers for the shared ContactResults component (components/crm-
// contact-results.tsx) -- kept separate from the JSX so the render-mode
// logic (what chrome shows in the normal Contacts/More Filters mode vs the
// read-only "simple" mode used by Astro Search) and the per-contact display
// fields are unit-testable without a DOM renderer.

import type { CrmContact } from "./api";

export interface ContactResultsMode {
  // Bulk-selection chrome: the "Select all on this page" bar, per-card
  // checkboxes, and "Select all N matching" button. Astro Search has no
  // export/write action to attach a selection to, so this is always hidden
  // there -- see app/astro/page.tsx.
  showSelectionChrome: boolean;
  // The rows-per-page + Previous/Next footer. Astro Search only ever holds
  // the first page_size matches the backend returned and cannot fetch more,
  // so showing paging controls would imply a capability that doesn't exist.
  showPagination: boolean;
}

export function contactResultsMode(simple: boolean): ContactResultsMode {
  return {
    showSelectionChrome: !simple,
    showPagination: !simple,
  };
}

// The top-of-results summary line. Non-simple mode states the paginated
// range ("Showing 1-50 of 89 contacts"); simple mode has no real page to
// report a range within, so it states just the rendered count.
export function contactResultsSummaryText({
  simple,
  total,
  page,
  pageSize,
}: {
  simple: boolean;
  total: number;
  page: number;
  pageSize: number;
}): string | null {
  if (total <= 0) return null;
  if (simple) return `${total} contact${total === 1 ? "" : "s"}`;
  const rangeStart = (page - 1) * pageSize + 1;
  const rangeEnd = Math.min(page * pageSize, total);
  return `Showing ${rangeStart}–${rangeEnd} of ${total} contact${total === 1 ? "" : "s"}`;
}

export function formatContactName(contact: Pick<CrmContact, "first_name" | "last_name">): string {
  return [contact.first_name, contact.last_name].filter(Boolean).join(" ") || "Unnamed contact";
}

export function formatContactLocation(contact: Pick<CrmContact, "city" | "state">): string {
  return [contact.city, contact.state].filter(Boolean).join(", ");
}

export function formatContactTitleCompany(contact: Pick<CrmContact, "title" | "company">): string {
  return [contact.title, contact.company].filter(Boolean).join(" @ ") || "No title/company on file";
}
