"use client";

import { useMemo, useState } from "react";
import { Mail, Plus, Search } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ConnectEmailModal } from "@/components/connect-email-modal";
import {
  deliverabilityBadgeClass,
  deliverabilityLabel,
  deriveTld,
  DELIVERABILITY_TOOLTIP,
  EMAIL_ACCOUNT_TABLE_COLUMNS,
  filterMailboxes,
  formatSendUsage,
  MAILBOX_ACCOUNTS,
  providerLabel,
} from "@/lib/mailboxes";
import { cn } from "@/lib/utils";

// Campaign Manager -> Emails. The canonical home for Astronomic Mail
// sending inboxes -- see lib/mailboxes.ts for why MAILBOX_ACCOUNTS is a
// literal empty array rather than a fetch: no mailbox model, OAuth, or
// connection capability exists yet. Nothing on this page calls a backend
// endpoint, starts an OAuth flow, or invents a row that isn't real.
export default function EmailsPage() {
  const [query, setQuery] = useState("");
  const [connectOpen, setConnectOpen] = useState(false);

  const filtered = useMemo(() => filterMailboxes(MAILBOX_ACCOUNTS, query), [query]);
  const hasAnyMailboxes = MAILBOX_ACCOUNTS.length > 0;

  return (
    <div className="mx-auto max-w-6xl px-6 py-10">
      <div className="mb-6 flex items-start justify-between gap-4">
        <div>
          <h1 className="font-serif text-2xl font-medium tracking-tight sm:text-3xl">Emails</h1>
          <p className="mt-2 max-w-2xl text-sm text-muted-foreground">
            Manage the inboxes used by Astronomic Mail campaigns.
          </p>
        </div>
        <Button type="button" className="shrink-0 gap-1.5" onClick={() => setConnectOpen(true)}>
          <Plus className="h-4 w-4" />
          Connect Email
        </Button>
      </div>

      {hasAnyMailboxes && (
        <div className="relative mb-4 max-w-sm">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search inboxes..."
            className="pl-8"
          />
        </div>
      )}

      {!hasAnyMailboxes && (
        <div className="flex flex-col items-center gap-4 rounded-2xl border border-dashed border-border/60 py-20 text-center">
          <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-secondary/60 text-muted-foreground">
            <Mail className="h-5 w-5" />
          </div>
          <div>
            <p className="font-medium">No sending inboxes connected</p>
            <p className="mt-1 max-w-sm text-sm text-muted-foreground">
              Connect a Google Workspace inbox to use with Astronomic Mail campaigns.
            </p>
          </div>
          <Button type="button" size="sm" className="mt-1 gap-1.5" onClick={() => setConnectOpen(true)}>
            <Plus className="h-4 w-4" />
            Connect Email
          </Button>
        </div>
      )}

      {hasAnyMailboxes && filtered.length === 0 && (
        <div className="rounded-xl border border-dashed border-border/60 py-16 text-center text-sm text-muted-foreground">
          No inboxes match your search.
        </div>
      )}

      {hasAnyMailboxes && filtered.length > 0 && (
        <div className="overflow-x-auto rounded-xl border border-border/60">
          <table className="w-full text-sm">
            <thead className="bg-secondary/40 text-xs text-muted-foreground">
              <tr>
                {EMAIL_ACCOUNT_TABLE_COLUMNS.map((column) => (
                  <th
                    key={column}
                    className={cn(
                      "px-3 py-2 text-left font-medium",
                      ["Campaigns", "Emails Sent Today", "Queue"].includes(column) && "text-right"
                    )}
                  >
                    {column}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-border/60">
              {filtered.map((mailbox) => {
                const tld = deriveTld(mailbox.email);
                return (
                  <tr key={mailbox.mailbox_id} className="hover:bg-secondary/30">
                    <td className="px-3 py-2.5 font-medium">{mailbox.display_name}</td>
                    <td className="px-3 py-2.5 text-muted-foreground">{mailbox.email}</td>
                    <td className="px-3 py-2.5 text-muted-foreground">{tld ?? "—"}</td>
                    <td className="px-3 py-2.5">
                      <Badge variant="outline" className="rounded-full font-normal text-muted-foreground">
                        {providerLabel(mailbox.provider)}
                      </Badge>
                    </td>
                    <td className="px-3 py-2.5">
                      <span
                        title={DELIVERABILITY_TOOLTIP}
                        className={cn(
                          "rounded-full px-2 py-0.5 text-xs font-medium",
                          deliverabilityBadgeClass(mailbox.deliverability_status)
                        )}
                      >
                        {mailbox.deliverability_score ?? deliverabilityLabel(mailbox.deliverability_status)}
                      </span>
                    </td>
                    <td className="px-3 py-2.5 text-right text-muted-foreground">{mailbox.campaign_count}</td>
                    <td className="px-3 py-2.5 text-right text-muted-foreground">
                      {formatSendUsage(mailbox.emails_sent_today, mailbox.daily_send_limit)}
                    </td>
                    <td className="px-3 py-2.5 text-right text-muted-foreground">{mailbox.queue_count}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <ConnectEmailModal open={connectOpen} onOpenChange={setConnectOpen} />
    </div>
  );
}
