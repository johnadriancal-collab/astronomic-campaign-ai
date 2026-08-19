// Astronomic Mail sending-inbox (mailbox) display helpers -- pure
// formatting/labeling logic kept separate from page components, same split
// as lib/mail.ts and lib/email-intake.ts.
//
// `Mailbox` (the real, backend-connected type) lives in lib/api.ts -- this
// module only formats/derives display values from it. Deliverability
// Index, Campaigns, Emails Sent Today, and Queue have NO backing field on
// Mailbox at all (no deliverability engine, no campaign<->mailbox
// assignment model, no sending engine, no send queue exist yet) -- the
// Emails page renders these as literal neutral values, not read off any
// mailbox field, so there is nothing here to fabricate.

import type { Mailbox, MailboxProvider, MailboxStatus } from "@/lib/api";

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
