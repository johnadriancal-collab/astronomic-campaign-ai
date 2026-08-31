/**
 * Shared width/padding convention for the Mail campaign detail page (and any
 * future Campaign Manager detail page) -- a single named constant instead of
 * a literal className string repeated across a page's loading/error/loaded
 * states, so a future width change is a one-line edit here rather than a
 * hunt across the file. Matches the Emails page's own max-w-6xl (the widest
 * existing convention in Campaign Manager) -- not edge-to-edge, still
 * centered with real padding, just no longer the narrowest page in the app.
 */
export const MAIL_CAMPAIGN_DETAIL_CONTAINER_CLASS = "mx-auto max-w-6xl px-6 py-10";
