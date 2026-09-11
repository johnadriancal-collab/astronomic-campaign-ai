/**
 * Shared width/padding + top-of-page two-column grid conventions for the
 * Client CRM Client and Engagement detail pages. Widened off the old
 * max-w-3xl (a narrow centered column leaving large unused desktop
 * margins) to max-w-6xl -- the SAME app-wide "wide detail page" value
 * already used by lib/crm-contact-detail-layout.ts's own
 * CRM_CONTACT_DETAIL_CONTAINER_CLASS and lib/mail-campaign-layout.ts's
 * MAIL_CAMPAIGN_DETAIL_CONTAINER_CLASS -- not an invented width. Each
 * detail page keeps its own small dedicated layout-constants module
 * (same one-file-per-page-family convention as those two), rather than a
 * single cross-page shared export, so this change never touches the
 * Contact detail or Mail Campaign pages at all.
 */

export const CLIENT_CRM_DETAIL_CONTAINER_CLASS = "mx-auto max-w-6xl px-6 py-10";

// Client detail page: Overview + Client Information stack in the larger
// left column; Contacts (a compact list of small cards, not a data-heavy
// table) gets the smaller right column. Same [3fr_2fr] "larger primary
// info vs. smaller compact-list" ratio, single-column-below-lg, and
// stretch-not-start convention as the Contact detail page's own
// OVERVIEW_EVENT_HISTORY_GRID_CLASS -- Touchpoints and Engagements stay
// OUTSIDE this grid, full width below it, since both are potentially
// data-heavy (a growing table / a growing list) and gain nothing from a
// narrower column.
export const CLIENT_OVERVIEW_CONTACTS_GRID_CLASS = "grid grid-cols-1 gap-6 lg:grid-cols-[3fr_2fr]";

// Engagement detail page: Overview and Linked Luma Event are two
// comparably-sized single info cards (unlike the asymmetric Overview/
// Contacts pairing above, neither is clearly "the compact one") -- an
// even split, same single-column-below-lg/stretch convention. Commercial,
// Closeout, and Participants stay OUTSIDE this grid, full width below it
// -- Commercial has no natural pairing partner left once Overview/Luma
// are paired, and Closeout/Participants are both potentially large
// (free-text review fields; a growing guest-list table) and explicitly
// benefit from the full container width.
export const ENGAGEMENT_OVERVIEW_LUMA_GRID_CLASS = "grid grid-cols-1 gap-6 lg:grid-cols-2";
