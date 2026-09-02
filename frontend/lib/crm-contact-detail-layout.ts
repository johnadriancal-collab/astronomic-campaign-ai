/**
 * Layout constants + small pure helpers for the CRM contact detail page.
 * Widened off the old max-w-3xl (a narrow center column leaving a lot of
 * unused desktop whitespace) to max-w-6xl -- matches this app's own
 * established "wide detail page" convention (see
 * lib/mail-campaign-layout.ts's identical MAIL_CAMPAIGN_DETAIL_CONTAINER_CLASS,
 * itself citing the Emails page's max-w-6xl), not an invented width.
 */

import type { CrmContact } from "@/lib/api";

export const CRM_CONTACT_DETAIL_CONTAINER_CLASS = "mx-auto max-w-6xl px-6 py-10";

// Overview + Event History: a single column (Overview first, per DOM order)
// below the lg breakpoint -- `grid-cols-1` is the unprefixed BASE class, so
// mobile/tablet always gets one column regardless of viewport width; `lg:`
// only ADDS the two-column split, it never removes the base. [3fr_2fr]
// (not an even 1fr_1fr) slightly favors Overview, whose content -- a prose
// summary plus wrapping highlight chips -- genuinely needs more room than
// Event History's compact one-line-per-event list.
//
// No `items-start` (and none at any breakpoint): CSS Grid's own default,
// `align-items: stretch`, is what we want here -- both Card elements
// stretch to match the taller of the two on desktop, so a short Event
// History (little/no content) doesn't leave the row visually uneven.
// Below lg, each card is alone in its own single-column row, so stretch
// vs. start makes no visual difference there -- natural content height is
// preserved automatically, not because of any explicit override.
export const OVERVIEW_EVENT_HISTORY_GRID_CLASS = "mb-6 grid grid-cols-1 gap-6 lg:grid-cols-[3fr_2fr]";

// The "Add to List" action on the contact detail page always targets
// exactly the one contact this page is showing -- never any other contact,
// never more than one. AddToListPanel (shared with the Contacts page, More
// Filters, and Astro Search) takes a plain `selectedIds: string[]`; this is
// the single, obviously-correct array for a single-contact page, pulled out
// as its own named function so that invariant is independently testable
// rather than an inline literal easy to get wrong in a future edit.
export function addToListSelectedIds(contact: Pick<CrmContact, "crm_contact_id">): string[] {
  return [contact.crm_contact_id];
}
