"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { AlertTriangle, Inbox as InboxIcon } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  Dialog,
  DialogPopup,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { ApiError, listInboxReplies, type MailInboxReplyView } from "@/lib/api";
import { mailCampaignStatusBadgeClass, mailCampaignStatusLabel } from "@/lib/mail";
import { cn } from "@/lib/utils";

// Inbox V1 (2026-09-17) -- a real, unified reply inbox across every
// Astronomic Mail campaign, sourced entirely from GET /mail/inbox/replies
// (itself a read-only join over the existing MailReply/MailEnrollment/
// MailCampaign/CrmContact/Mailbox/MailEnrollmentStep data -- no second
// reply model, no fabricated state). Deliberately shows metadata only:
// this system has reply detection under the gmail.metadata scope, which
// never carries body/snippet content -- see MailInboxReplyView's backend
// docstring. There is no unread/read model anywhere in this codebase, so
// this page never invents one.

function formatDateTime(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

export default function InboxPage() {
  const [replies, setReplies] = useState<MailInboxReplyView[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [campaignFilter, setCampaignFilter] = useState<string>("all");
  const [selected, setSelected] = useState<MailInboxReplyView | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const data = await listInboxReplies();
        if (!cancelled) {
          setReplies(data);
          setError(null);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof ApiError ? `Couldn't load the Inbox (${err.status}): ${err.message}` : "Couldn't reach the backend.");
        }
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, []);

  const campaigns = useMemo(() => {
    if (!replies) return [];
    const seen = new Map<string, string>();
    for (const r of replies) seen.set(r.mail_campaign_id, r.campaign_name);
    return Array.from(seen.entries());
  }, [replies]);

  const filtered = useMemo(() => {
    if (!replies) return [];
    const query = search.trim().toLowerCase();
    return replies.filter((r) => {
      if (campaignFilter !== "all" && r.mail_campaign_id !== campaignFilter) return false;
      if (!query) return true;
      return (
        (r.contact_name ?? "").toLowerCase().includes(query) ||
        r.email.toLowerCase().includes(query) ||
        r.campaign_name.toLowerCase().includes(query)
      );
    });
  }, [replies, search, campaignFilter]);

  return (
    <div className="mx-auto max-w-4xl px-6 py-10">
      <div className="mb-6">
        <h1 className="font-serif text-2xl font-medium tracking-tight sm:text-3xl">Inbox</h1>
        <p className="mt-2 max-w-2xl text-sm text-muted-foreground">
          Replies from leads across your Astronomic Mail campaigns.
        </p>
      </div>

      {error && (
        <Alert variant="destructive" className="mb-6">
          <AlertTriangle />
          <AlertTitle>Couldn&apos;t load the Inbox</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {!error && replies === null && <div className="text-sm text-muted-foreground">Loading…</div>}

      {!error && replies !== null && replies.length === 0 && (
        <Card>
          <CardContent className="py-14 text-center">
            <div className="mx-auto mb-4 flex h-11 w-11 items-center justify-center rounded-2xl bg-secondary/60 text-muted-foreground">
              <InboxIcon className="h-5 w-5" />
            </div>
            <p className="text-sm text-muted-foreground">
              No replies yet. Once a lead replies to a campaign, it will show up here automatically.
            </p>
          </CardContent>
        </Card>
      )}

      {!error && replies !== null && replies.length > 0 && (
        <>
          <div className="mb-4 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <h2 className="text-sm font-medium text-muted-foreground">
              Inbox ({filtered.length}
              {filtered.length !== replies.length ? ` of ${replies.length}` : ""})
            </h2>
            <div className="flex flex-col gap-2 sm:flex-row">
              <Input
                placeholder="Search name, email, or campaign…"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                className="sm:w-64"
              />
              {campaigns.length > 1 && (
                <select
                  value={campaignFilter}
                  onChange={(e) => setCampaignFilter(e.target.value)}
                  className="rounded-md border border-input bg-background px-3 py-2 text-sm"
                >
                  <option value="all">All campaigns</option>
                  {campaigns.map(([id, name]) => (
                    <option key={id} value={id}>
                      {name}
                    </option>
                  ))}
                </select>
              )}
            </div>
          </div>

          {filtered.length === 0 ? (
            <Card>
              <CardContent className="py-10 text-center text-sm text-muted-foreground">
                No replies match this search.
              </CardContent>
            </Card>
          ) : (
            <Card>
              <CardContent className="p-0">
                <div className="divide-y divide-border">
                  {filtered.map((reply) => (
                    <button
                      key={reply.enrollment_id}
                      type="button"
                      onClick={() => setSelected(reply)}
                      className="flex w-full items-center justify-between gap-3 px-6 py-3 text-left text-sm transition-colors hover:bg-secondary/40"
                    >
                      <div className="min-w-0">
                        <div className="flex items-center gap-2">
                          <span className="truncate font-medium">{reply.contact_name ?? reply.email}</span>
                          <span
                            className={cn(
                              "shrink-0 rounded-full px-2 py-0.5 text-xs font-medium",
                              "bg-emerald-100 text-emerald-800"
                            )}
                          >
                            Replied
                          </span>
                        </div>
                        <div className="truncate text-xs text-muted-foreground">
                          {reply.email} · {reply.campaign_name}
                        </div>
                      </div>
                      <span className="shrink-0 whitespace-nowrap text-xs text-muted-foreground">
                        {formatDateTime(reply.replied_at)}
                      </span>
                    </button>
                  ))}
                </div>
              </CardContent>
            </Card>
          )}
        </>
      )}

      <Dialog open={selected !== null} onOpenChange={(open) => !open && setSelected(null)}>
        <DialogPopup className="max-w-lg">
          {selected && (
            <>
              <DialogHeader>
                <DialogTitle>{selected.contact_name ?? selected.email}</DialogTitle>
                <DialogDescription>{selected.email}</DialogDescription>
              </DialogHeader>
              <div className="space-y-4 px-6 pb-6 text-sm">
                <div className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2">
                  <span className="text-muted-foreground">Campaign</span>
                  <span className="flex items-center gap-2">
                    <Link href={`/manager/campaigns/mail/${selected.mail_campaign_id}`} className="underline hover:no-underline">
                      {selected.campaign_name}
                    </Link>
                    <span
                      className={cn(
                        "rounded-full px-2 py-0.5 text-xs font-medium",
                        mailCampaignStatusBadgeClass(selected.campaign_status)
                      )}
                    >
                      {mailCampaignStatusLabel(selected.campaign_status)}
                    </span>
                  </span>

                  <span className="text-muted-foreground">Sender mailbox</span>
                  <span>{selected.mailbox_email ?? "(mailbox no longer available)"}</span>

                  <span className="text-muted-foreground">Original subject</span>
                  <span>{selected.subject ?? "(not available)"}</span>

                  <span className="text-muted-foreground">Replied</span>
                  <span>{formatDateTime(selected.replied_at)}</span>

                  <span className="text-muted-foreground">Gmail thread ID</span>
                  <span className="break-all font-mono text-xs">{selected.gmail_thread_id}</span>

                  <span className="text-muted-foreground">Reply message ID</span>
                  <span className="break-all font-mono text-xs">{selected.gmail_message_id}</span>

                  {selected.skipped_step_numbers.length > 0 && (
                    <>
                      <span className="text-muted-foreground">Steps skipped</span>
                      <span>
                        Step{selected.skipped_step_numbers.length > 1 ? "s" : ""}{" "}
                        {selected.skipped_step_numbers.join(", ")} — never sent because this lead replied.
                      </span>
                    </>
                  )}
                </div>
                <p className="rounded-md bg-secondary/40 px-3 py-2 text-xs text-muted-foreground">
                  Metadata only — this Inbox shows who replied and when, not the reply&apos;s content. Full
                  message-body viewing would require a separate Gmail scope decision (this mailbox currently
                  has gmail.metadata, not gmail.readonly).
                </p>
              </div>
            </>
          )}
        </DialogPopup>
      </Dialog>
    </div>
  );
}
