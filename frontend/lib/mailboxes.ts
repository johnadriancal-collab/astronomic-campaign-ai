// Astronomic Mail sending-inbox (mailbox) types and pure helpers.
//
// IMPORTANT -- this module is deliberately backend-free. There is no
// MailboxAccount model, store, or API route anywhere in this app yet (see
// app/models/mail.py's own docstring: "MailboxConfig / Mailbox -- no real
// mailbox exists until Phase 2 (OAuth)"). MAILBOX_ACCOUNTS below is a
// literal empty array, not a fetch result -- the Emails page renders
// straight from it, so there is nothing to "fabricate": zero real mailboxes
// exist, so this shows zero rows, honestly.
//
// This module exists now so that (a) the Emails page has a stable shape to
// render against today, and (b) Phase 2 (Gmail OAuth) can introduce a real
// backend model/endpoint and swap MAILBOX_ACCOUNTS for a real fetch without
// changing anything about the table, search, or column rendering below it.

export type MailboxProvider = "google_workspace";

// Only "unknown" is ever real right now -- see DELIVERABILITY_TOOLTIP.
// The other three are reserved for whatever real signal Phase 2+ builds
// (a simple tier, or a numeric score -- deliverability_score exists on
// MailboxAccount for exactly that, independent of this status tier).
export type DeliverabilityStatus = "good" | "warning" | "poor" | "unknown";

export interface MailboxAccount {
  mailbox_id: string;
  display_name: string;
  email: string;
  provider: MailboxProvider;
  deliverability_status: DeliverabilityStatus;
  deliverability_score: number | null;
  campaign_count: number;
  emails_sent_today: number;
  daily_send_limit: number | null;
  queue_count: number;
  connected_at: string | null;
}

// No mailbox connection exists yet -- always empty until Phase 2 (Gmail
// OAuth) introduces a real backend model and this is replaced by a fetch.
export const MAILBOX_ACCOUNTS: MailboxAccount[] = [];

// The Emails table's exact, approved column set (Campaign Manager
// Integration Phase follow-up) -- deliberately excludes Signature, Custom
// Domain, and Smart Sending Groups, which QuickMail's Email Accounts view
// has but this product does not want. Exported so the page renders its
// headers directly from this array and a test can assert it never drifts.
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

export const DELIVERABILITY_TOOLTIP = "Deliverability monitoring will be added later.";

export function providerLabel(provider: MailboxProvider): string {
  switch (provider) {
    case "google_workspace":
      return "Google Workspace";
  }
}

export function deliverabilityLabel(status: DeliverabilityStatus): string {
  switch (status) {
    case "good":
      return "Good";
    case "warning":
      return "Warning";
    case "poor":
      return "Poor";
    case "unknown":
      return "—";
  }
}

// Same "bold color for the state that matters" convention as
// mailCampaignStatusBadgeClass (lib/mail.ts) / statusBadgeClass
// (lib/email-intake.ts). "unknown" -- the only state that exists in
// practice today -- deliberately gets the same neutral treatment as every
// other not-yet-real metric in this app (e.g. daily_capacity_estimate).
export function deliverabilityBadgeClass(status: DeliverabilityStatus): string {
  switch (status) {
    case "good":
      return "bg-emerald-100 text-emerald-800";
    case "warning":
      return "bg-amber-100 text-amber-800";
    case "poor":
      return "bg-destructive/10 text-destructive";
    case "unknown":
      return "bg-secondary text-muted-foreground";
  }
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

// Filters by display name OR email, case-insensitive -- the only two
// fields the Emails page's search bar is specified to match against.
export function filterMailboxes(mailboxes: MailboxAccount[], query: string): MailboxAccount[] {
  const q = query.trim().toLowerCase();
  if (!q) return mailboxes;
  return mailboxes.filter(
    (m) => m.display_name.toLowerCase().includes(q) || m.email.toLowerCase().includes(q)
  );
}

// "24 / 50"-style future display -- returns null (render a neutral dash)
// when there's no real limit to compare against yet, exactly like
// deliverability_score. Kept here, not inlined in the page, so Phase 2 can
// wire real numbers through this one function.
export function formatSendUsage(sent: number, limit: number | null): string {
  return limit === null ? String(sent) : `${sent} / ${limit}`;
}
