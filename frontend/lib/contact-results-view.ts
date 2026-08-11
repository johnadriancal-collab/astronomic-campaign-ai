// Pure helpers for the shared ContactResults component (components/crm-
// contact-results.tsx) -- kept separate from the JSX so the render-mode
// logic (what chrome shows for Contacts/More Filters/Astro Search) and the
// per-contact display fields are unit-testable without a DOM renderer.

import type { CrmContact } from "./api";

export interface ContactResultsMode {
  // Bulk-selection chrome: the "Select all on this page" bar, per-card
  // checkboxes, and "Select all N matching" button.
  showSelectionChrome: boolean;
  // The rows-per-page + Previous/Next footer. Astro Search only ever holds
  // the first page_size matches the backend returned and cannot fetch more
  // to page through, so showing paging controls would imply a capability
  // that doesn't exist -- see app/crm/astro/page.tsx.
  showPagination: boolean;
}

// The two chrome pieces are independent: Astro Search hides pagination but
// (as of the full-selection/export feature) DOES show selection chrome, so
// they can no longer be driven off a single "simple" boolean. Contacts and
// More Filters pass neither flag (both false, the default).
export function contactResultsMode({
  hideSelection = false,
  hidePagination = false,
}: {
  hideSelection?: boolean;
  hidePagination?: boolean;
} = {}): ContactResultsMode {
  return {
    showSelectionChrome: !hideSelection,
    showPagination: !hidePagination,
  };
}

// The top-of-results summary line. With pagination shown, it states the
// paginated range ("Showing 1-50 of 89 contacts"). With pagination hidden,
// there's no real page to report a range within -- it states the total
// match count, plus how many are actually rendered when that's fewer than
// the total (Astro Search only ever renders the first page_size matches).
export function contactResultsSummaryText({
  hidePagination,
  total,
  page,
  pageSize,
  renderedCount,
}: {
  hidePagination: boolean;
  total: number;
  page: number;
  pageSize: number;
  renderedCount?: number;
}): string | null {
  if (total <= 0) return null;
  if (hidePagination) {
    const shown = renderedCount ?? total;
    const totalText = `${total} contact${total === 1 ? "" : "s"}`;
    return shown < total ? `${totalText} (showing the first ${shown})` : totalText;
  }
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
