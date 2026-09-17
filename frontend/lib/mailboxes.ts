// Astronomic Mail sending-inbox (mailbox) display helpers -- pure
// formatting/labeling logic kept separate from page components, same split
// as lib/mail.ts and lib/email-intake.ts.
//
// `Mailbox` (the real, backend-connected type) lives in lib/api.ts -- this
// module only formats/derives display values from it. Deliverability
// Index, Campaigns, Emails Sent Today, and Queue have NO backing field on
// Mailbox at all -- a real sending engine and send queue now exist
// backend-side (Phase C), but this Emails page table has no per-mailbox
// stat wired to either yet (no deliverability engine, no campaign<->
// mailbox assignment model exposed here). It renders these as literal
// neutral values, not read off any mailbox field, so there is nothing
// here to fabricate.

import type { Mailbox, MailboxAuthorizationHealth, MailboxProvider, MailboxStatus } from "@/lib/api";

export const DELIVERABILITY_TOOLTIP = "Deliverability monitoring will be added later.";

// The Emails table's exact, approved column set -- deliberately excludes
// Signature, Custom Domain, and Smart Sending Groups, which QuickMail's
// Email Accounts view has but this product does not want. Exported so the
// page renders its headers directly from this array and a test can assert
// it never drifts.
export const EMAIL_ACCOUNT_TABLE_COLUMNS = [
  "Name",
  "Email",
  "TLD",
  "Provider",
  "Deliverability Index",
  "Campaigns",
  "Emails Sent Today",
  "Queue",
] as const;

// Matches app/google/oauth_client.py's GMAIL_SEND_SCOPE exactly -- the
// FIRST of the two scopes this app ever requests beyond base identity.
// Kept as a named constant, not inlined at each call site, so a future
// drift between frontend and backend is a one-line diff to notice, not a
// silent string mismatch.
export const GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send";

// Matches app/google/oauth_client.py's GMAIL_METADATA_SCOPE exactly
// (2026-09-15) -- requested in the SAME upgrade flow as GMAIL_SEND_SCOPE
// above (see MailboxService.begin_gmail_send_upgrade()'s own docstring:
// one reconnect grants both, no separate flow). Added specifically so a
// mailbox that already has gmail.send from before this scope existed
// still shows the upgrade action -- see gmailSendUpgradeState() below;
// without this, a mailbox already at "enabled" would never re-prompt
// for the added scope, exactly the gap that caused a real reconnect to
// silently grant gmail.send-only again.
export const GMAIL_METADATA_SCOPE = "https://www.googleapis.com/auth/gmail.metadata";

// 2026-09-17 (Inbox V2) -- same reasoning as GMAIL_METADATA_SCOPE above,
// one more time: requested in the SAME upgrade flow (see
// MailboxService.begin_gmail_send_upgrade()'s own docstring), added
// here specifically so a mailbox that already has send+metadata from
// before this scope existed still shows the upgrade action. Skipping
// this update is EXACTLY the gap the gmail.metadata comment above
// already warned about -- and exactly what happened here: Victoria's
// mailbox already had send+metadata, so gmailSendUpgradeState() kept
// returning "enabled" and the upgrade button stayed hidden even after
// this scope existed, until this fix.
export const GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly";

export function hasGmailSendScope(mailbox: Mailbox): boolean {
  return mailbox.granted_scopes.includes(GMAIL_SEND_SCOPE);
}

export function hasGmailMetadataScope(mailbox: Mailbox): boolean {
  return mailbox.granted_scopes.includes(GMAIL_METADATA_SCOPE);
}

export function hasGmailReadonlyScope(mailbox: Mailbox): boolean {
  return mailbox.granted_scopes.includes(GMAIL_READONLY_SCOPE);
}

// Astronomic Mail Gmail-send upgrade (see components/enable-gmail-
// sending-modal.tsx) -- derived ENTIRELY from the mailbox's own status
// and granted_scopes, never a separate flag, so this can never drift
// from what the backend would actually do if the upgrade were attempted
// right now. "needs_reconnect" deliberately takes priority over
// "enabled": a NEEDS_REAUTH/DISCONNECTED mailbox is not currently usable
// for sending regardless of what it was once granted, and the UI must
// never imply otherwise. "enabled" requires ALL THREE scopes -- a
// mailbox missing any one of them is "can_enable", so the SAME upgrade
// button/modal remains clickable and re-requests the full desired scope
// set (Google's include_granted_scopes=true keeps whatever was already
// granted either way).
export type GmailSendUpgradeState = "enabled" | "can_enable" | "needs_reconnect";

export function gmailSendUpgradeState(mailbox: Mailbox): GmailSendUpgradeState {
  if (mailbox.status !== "connected") return "needs_reconnect";
  return hasGmailSendScope(mailbox) && hasGmailMetadataScope(mailbox) && hasGmailReadonlyScope(mailbox)
    ? "enabled"
    : "can_enable";
}

export function providerLabel(provider: MailboxProvider): string {
  switch (provider) {
    case "google":
      return "Google Workspace";
  }
}

// Same "bold color for the state that matters" convention as
// mailCampaignStatusBadgeClass (lib/mail.ts) / statusBadgeClass
// (lib/email-intake.ts).
export function mailboxStatusLabel(status: MailboxStatus): string {
  switch (status) {
    case "connected":
      return "Connected";
    case "needs_reauth":
      return "Needs Reauthorization";
    case "disconnected":
      return "Disconnected";
  }
}

export function mailboxStatusBadgeClass(status: MailboxStatus): string {
  switch (status) {
    case "connected":
      return "bg-emerald-100 text-emerald-800";
    case "needs_reauth":
      return "bg-amber-100 text-amber-800";
    case "disconnected":
      return "bg-secondary text-muted-foreground";
  }
}

// Name column: Google account display name where available, falling back
// safely to the email address if Google returned no name.
export function mailboxDisplayName(mailbox: Mailbox): string {
  return mailbox.display_name || mailbox.email;
}

// Derives a display TLD from an email's domain without storing it
// redundantly. Returns null for anything that doesn't look like a real
// address with a real domain suffix (no "@", nothing after "@", or a
// domain with no dot at all, e.g. a bare hostname) -- callers should
// render that as a neutral dash, never throw or guess.
export function deriveTld(email: string): string | null {
  const at = email.lastIndexOf("@");
  if (at === -1 || at === email.length - 1) return null;
  const domain = email.slice(at + 1).trim();
  const parts = domain.split(".").filter(Boolean);
  if (parts.length < 2) return null;
  return parts[parts.length - 1].toLowerCase();
}

// Filters by display name (falling back to email, matching what's actually
// rendered in the Name column) OR email, case-insensitive -- the only two
// fields the Emails page's search bar is specified to match against.
export function filterMailboxes(mailboxes: Mailbox[], query: string): Mailbox[] {
  const q = query.trim().toLowerCase();
  if (!q) return mailboxes;
  return mailboxes.filter(
    (m) => mailboxDisplayName(m).toLowerCase().includes(q) || m.email.toLowerCase().includes(q)
  );
}

// "24 / 50"-style future display -- returns a plain count (never fabricates
// a limit) when there's no real limit to compare against yet. Kept here,
// not inlined in the page, so a future sending engine can wire real numbers
// through this one function.
export function formatSendUsage(sent: number, limit: number | null): string {
  return limit === null ? String(sent) : `${sent} / ${limit}`;
}

// --- Proactive Gmail OAuth expiration warnings (2026-09-17) ----------------
//
// `Mailbox.authorization_health` is computed backend-side, fresh on every
// GET /mailboxes call (see app/services/mailbox_authorization_health.py) --
// these are pure display helpers only, never a second place that decides
// the state itself. `formatAuthorizationAge`/`estimatedExpiryLabel` DO
// compute their own live "time remaining" text client-side from
// `estimated_expires_at`, which is fine (pure formatting of an already-
// backend-computed timestamp), but they never re-derive the health STATE.

export function mailboxAuthorizationHealthLabel(health: MailboxAuthorizationHealth): string {
  switch (health) {
    case "connected":
      return "Connected";
    case "reconnect_soon":
      return "Reconnect soon";
    case "needs_reauth":
      return "Needs reauthorization";
    case null:
      return "";
  }
}

export function mailboxAuthorizationHealthBadgeClass(health: MailboxAuthorizationHealth): string {
  switch (health) {
    case "connected":
      return "bg-emerald-100 text-emerald-800";
    case "reconnect_soon":
      return "bg-amber-100 text-amber-800";
    case "needs_reauth":
      return "bg-red-100 text-red-800";
    case null:
      return "bg-secondary text-muted-foreground";
  }
}

// "expires in approximately 18 hours" / "expired ~2 hours ago" -- rounds
// to the coarsest unit that stays readable (days when >= 1 day away,
// otherwise hours), matching the approved spec's own example copy. null
// input (no estimate available, e.g. Testing-mode expiry disabled, or a
// disconnected mailbox) returns null so the caller can skip the sentence
// entirely rather than render something misleading.
export function estimatedExpiryLabel(estimatedExpiresAt: string | null, now: Date = new Date()): string | null {
  if (!estimatedExpiresAt) return null;
  const diffMs = new Date(estimatedExpiresAt).getTime() - now.getTime();
  const absHours = Math.abs(diffMs) / (1000 * 60 * 60);
  const verb = diffMs >= 0 ? "expires in approximately" : "expired approximately";
  if (absHours >= 24) {
    const days = Math.round(absHours / 24);
    return `Google authorization ${verb} ${days} day${days === 1 ? "" : "s"}.`;
  }
  const hours = Math.max(1, Math.round(absHours));
  return `Google authorization ${verb} ${hours} hour${hours === 1 ? "" : "s"}.`;
}

// Plain "X days ago" / "X hours ago" age display for the Emails page's
// "Last authorized" column -- distinct from estimatedExpiryLabel (which is
// forward-looking, toward expiry) even though both are derived from the
// same underlying timestamp.
export function formatAuthorizationAge(ageSeconds: number | null): string {
  if (ageSeconds === null) return "—";
  const hours = ageSeconds / 3600;
  if (hours < 1) return "Less than an hour ago";
  if (hours < 24) {
    const h = Math.round(hours);
    return `${h} hour${h === 1 ? "" : "s"} ago`;
  }
  const days = Math.round(hours / 24);
  return `${days} day${days === 1 ? "" : "s"} ago`;
}

// A campaign's assigned mailbox is at risk of missing a scheduled send if
// its estimated expiry falls BEFORE that send's next_send_at -- pure
// comparison of two already-computed timestamps, no new decision logic.
export function reconnectRequiredBeforeNextSend(estimatedExpiresAt: string | null, nextSendAt: string | null): boolean {
  if (!estimatedExpiresAt || !nextSendAt) return false;
  return new Date(nextSendAt).getTime() > new Date(estimatedExpiresAt).getTime();
}
