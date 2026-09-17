"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { AlertTriangle, Inbox as InboxIcon, RefreshCw } from "lucide-react";
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
import {
  ApiError,
  getInboxReplyBody,
  listInboxReplies,
  type MailInboxReplyBody,
  type MailInboxReplyView,
} from "@/lib/api";
import { mailCampaignStatusBadgeClass, mailCampaignStatusLabel } from "@/lib/mail";
import { cn } from "@/lib/utils";

// Inbox V1 (2026-09-17) -- a real, unified reply inbox across every
// Astronomic Mail campaign, sourced from GET /mail/inbox/replies (a
// read-only join over the existing MailReply/MailEnrollment/
// MailCampaign/CrmContact/Mailbox/MailEnrollmentStep data -- no second
// reply model, no fabricated state).
//
// Inbox V2 (2026-09-17) -- the detail view additionally fetches the
// actual reply text on demand (GET /mail/inbox/replies/{id}/body, under
// gmail.readonly) the moment it opens -- never eagerly for the whole
// list, never persisted. `status` on that response is the only thing
// this page branches on; every non-"ok" status is a normal, expected UI
// state (reconnect needed, Gmail hiccup), not an error that breaks the
// rest of the Inbox. There is no unread/read model anywhere in this
// codebase, so this page never invents one.

function formatDateTime(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

function ReplyBody({ enrollmentId }: { enrollmentId: string }) {
  const [state, setState] = useState<{ loading: boolean; result: MailInboxReplyBody | null; error: string | null }>({
    loading: true,
    result: null,
    error: null,
  });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setState({ loading: true, result: null, error: null });
    getInboxReplyBody(enrollmentId)
      .then((result) => {
        if (!cancelled) setState({ loading: false, result, error: null });
      })
      .catch((err) => {
        if (!cancelled) {
          setState({
            loading: false,
            result: null,
            error: err instanceof ApiError ? `Couldn't load the reply (${err.status}): ${err.message}` : "Couldn't reach the backend.",
          });
        }
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enrollmentId, attempt]);

  if (state.loading) {
    return <p className="text-sm text-muted-foreground">Loading reply…</p>;
  }

  if (state.error) {
    return (
      <div className="space-y-2">
        <p className="text-sm text-destructive">{state.error}</p>
        <button
          type="button"
          onClick={() => setAttempt((n) => n + 1)}
          className="inline-flex items-center gap-1.5 text-xs text-muted-foreground underline hover:no-underline"
        >
          <RefreshCw className="h-3 w-3" /> Try again
        </button>
      </div>
    );
  }

  const result = state.result!;

  if (result.status === "scope_missing" || result.status === "needs_reauth") {
    return (
      <p className="rounded-md bg-secondary/40 px-3 py-2 text-sm text-muted-foreground">
        Reconnect this mailbox to enable reply-content viewing.
      </p>
    );
  }

  if (result.status === "not_found") {
    return <p className="text-sm text-muted-foreground">Gmail no longer has this specific message.</p>;
  }

  if (result.status === "provider_error") {
    return (
      <div className="space-y-2">
        <p className="text-sm text-muted-foreground">Couldn&apos;t load the reply right now.</p>
        <button
          type="button"
          onClick={() => setAttempt((n) => n + 1)}
          className="inline-flex items-center gap-1.5 text-xs text-muted-foreground underline hover:no-underline"
        >
          <RefreshCw className="h-3 w-3" /> Try again
        </button>
      </div>
    );
  }

  if (!result.body_text) {
    return <p className="text-sm text-muted-foreground">(This message has no readable text.)</p>;
  }

  return (
    <div>
      <p className="whitespace-pre-wrap text-sm leading-relaxed">{result.body_text}</p>
      {result.body_source === "html_converted" && (
        <p className="mt-2 text-xs text-muted-foreground/70">Converted from an HTML-only message.</p>
      )}
    </div>
  );
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
                <DialogDescription>
                  {selected.email} · {selected.campaign_name} · {formatDateTime(selected.replied_at)}
                </DialogDescription>
              </DialogHeader>
              <div className="space-y-4 px-6 pb-6">
                <div className="rounded-md border border-border bg-secondary/20 p-4">
                  <ReplyBody enrollmentId={selected.enrollment_id} />
                </div>

                <div className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 text-sm">
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
              </div>
            </>
          )}
        </DialogPopup>
      </Dialog>
    </div>
  );
}
