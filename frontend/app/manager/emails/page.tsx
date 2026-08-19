"use client";

import { Suspense, useEffect, useMemo, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { AlertTriangle, CheckCircle2, Mail, Plus, Search, Unlink } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { ConnectEmailModal } from "@/components/connect-email-modal";
import { DisconnectMailboxModal } from "@/components/disconnect-mailbox-modal";
import { ApiError, listMailboxes, type Mailbox } from "@/lib/api";
import {
  DELIVERABILITY_TOOLTIP,
  EMAIL_ACCOUNT_TABLE_COLUMNS,
  deriveTld,
  filterMailboxes,
  formatSendUsage,
  mailboxDisplayName,
  mailboxStatusBadgeClass,
  mailboxStatusLabel,
  providerLabel,
} from "@/lib/mailboxes";
import { cn } from "@/lib/utils";

// Astronomic Mail Phase 2 -- Google Workspace Mailbox Connection.
// `?connected=1` / `?error=<code>` arrive here after the backend's OAuth
// callback (see app/api/mailboxes.py) redirects back -- this page reads
// them once on mount, shows the matching banner, then strips them from the
// URL so a refresh doesn't re-show a stale result.
const ERROR_MESSAGES: Record<string, string> = {
  access_denied: "Google sign-in was cancelled.",
  state_mismatch: "That connection link expired or was already used -- please try again.",
  missing_code: "Google didn't return an authorization code -- please try again.",
  token_exchange_failed: "Couldn't complete the connection with Google -- please try again.",
  not_configured: "Google Workspace connection isn't configured yet.",
};

// useSearchParams() requires a Suspense boundary above it -- this wrapper
// is the only reason EmailsPageContent isn't the default export directly.
export default function EmailsPage() {
  return (
    <Suspense fallback={null}>
      <EmailsPageContent />
    </Suspense>
  );
}

function EmailsPageContent() {
  const router = useRouter();
  const searchParams = useSearchParams();

  const [mailboxes, setMailboxes] = useState<Mailbox[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [connectOpen, setConnectOpen] = useState(false);
  const [disconnectTarget, setDisconnectTarget] = useState<Mailbox | null>(null);
  const [banner, setBanner] = useState<{ type: "success" | "error"; message: string } | null>(null);

  async function load() {
    try {
      setMailboxes(await listMailboxes());
      setError(null);
    } catch (err) {
      setError(
        err instanceof ApiError
          ? `Couldn't load connected inboxes (${err.status}): ${err.message}`
          : "Couldn't reach the backend."
      );
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const connected = searchParams.get("connected");
    const errorCode = searchParams.get("error");
    if (connected) {
      setBanner({ type: "success", message: "Email connected successfully." });
      router.replace("/manager/emails");
    } else if (errorCode) {
      setBanner({ type: "error", message: ERROR_MESSAGES[errorCode] ?? "Couldn't connect that inbox." });
      router.replace("/manager/emails");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  const filtered = useMemo(() => (mailboxes ? filterMailboxes(mailboxes, query) : []), [mailboxes, query]);
  const hasAnyMailboxes = (mailboxes?.length ?? 0) > 0;

  function handleDisconnected(updated: Mailbox) {
    setMailboxes((prev) => (prev ? prev.map((m) => (m.mailbox_id === updated.mailbox_id ? updated : m)) : prev));
    setDisconnectTarget(null);
  }

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

      {banner && banner.type === "success" && (
        <Alert className="mb-4">
          <CheckCircle2 className="h-4 w-4" />
          <AlertDescription>{banner.message}</AlertDescription>
        </Alert>
      )}
      {banner && banner.type === "error" && (
        <Alert variant="destructive" className="mb-4">
          <AlertTriangle />
          <AlertDescription>{banner.message}</AlertDescription>
        </Alert>
      )}

      {error && (
        <Alert variant="destructive" className="mb-4">
          <AlertTriangle />
          <AlertTitle>Couldn&apos;t load connected inboxes</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {!error && mailboxes === null && (
        <div className="space-y-2">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-12 w-full rounded-lg" />
          ))}
        </div>
      )}

      {!error && mailboxes !== null && hasAnyMailboxes && (
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

      {!error && mailboxes !== null && !hasAnyMailboxes && (
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

      {!error && mailboxes !== null && hasAnyMailboxes && filtered.length === 0 && (
        <div className="rounded-xl border border-dashed border-border/60 py-16 text-center text-sm text-muted-foreground">
          No inboxes match your search.
        </div>
      )}

      {!error && mailboxes !== null && hasAnyMailboxes && filtered.length > 0 && (
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
                <th className="px-3 py-2 text-right font-medium">Status</th>
                <th className="px-3 py-2" />
              </tr>
            </thead>
            <tbody className="divide-y divide-border/60">
              {filtered.map((mailbox) => {
                const tld = deriveTld(mailbox.email);
                return (
                  <tr key={mailbox.mailbox_id} className="hover:bg-secondary/30">
                    <td className="px-3 py-2.5 font-medium">{mailboxDisplayName(mailbox)}</td>
                    <td className="px-3 py-2.5 text-muted-foreground">{mailbox.email}</td>
                    <td className="px-3 py-2.5 text-muted-foreground">{tld ?? "—"}</td>
                    <td className="px-3 py-2.5">
                      <Badge variant="outline" className="rounded-full font-normal text-muted-foreground">
                        {providerLabel(mailbox.provider)}
                      </Badge>
                    </td>
                    <td className="px-3 py-2.5 text-muted-foreground" title={DELIVERABILITY_TOOLTIP}>
                      —
                    </td>
                    <td className="px-3 py-2.5 text-right text-muted-foreground">0</td>
                    <td className="px-3 py-2.5 text-right text-muted-foreground">{formatSendUsage(0, null)}</td>
                    <td className="px-3 py-2.5 text-right text-muted-foreground">0</td>
                    <td className="px-3 py-2.5 text-right">
                      <span
                        className={cn(
                          "rounded-full px-2 py-0.5 text-xs font-medium",
                          mailboxStatusBadgeClass(mailbox.status)
                        )}
                      >
                        {mailboxStatusLabel(mailbox.status)}
                      </span>
                    </td>
                    <td className="px-3 py-2.5 text-right">
                      {mailbox.status !== "disconnected" && (
                        <Button
                          type="button"
                          variant="ghost"
                          size="icon-sm"
                          onClick={() => setDisconnectTarget(mailbox)}
                          title="Disconnect"
                        >
                          <Unlink className="h-3.5 w-3.5" />
                        </Button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <ConnectEmailModal open={connectOpen} onOpenChange={setConnectOpen} />
      <DisconnectMailboxModal
        mailbox={disconnectTarget}
        onOpenChange={(open) => !open && setDisconnectTarget(null)}
        onDisconnected={handleDisconnected}
      />
    </div>
  );
}
