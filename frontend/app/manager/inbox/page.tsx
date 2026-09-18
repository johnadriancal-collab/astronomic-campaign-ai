"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { AlertTriangle, Inbox as InboxIcon } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { ApiError, listInboxReplies, type MailInboxReplyView } from "@/lib/api";
import { MAIL_CAMPAIGN_DETAIL_CONTAINER_CLASS } from "@/lib/mail-campaign-layout";

// Inbox V1 (2026-09-17) -- a real, unified reply inbox across every
// Astronomic Mail campaign, sourced from GET /mail/inbox/replies (a
// read-only join over the existing MailReply/MailEnrollment/
// MailCampaign/CrmContact/Mailbox/MailEnrollmentStep data -- no second
// reply model, no fabricated state).
//
// Inbox V2 (2026-09-17): reply reading moved from a modal to a
// dedicated full page at /manager/inbox/[enrollment_id] (see that
// route) -- clicking a row here is a real navigation (next/link),
// not a Dialog open. There is no unread/read model anywhere in this
// codebase, so this page never invents one.

// Compact -- omits the year, same convention as the Campaigns/Leads list
// pages' own formatDateTime, so the reply timestamp stays single-line at
// the tighter row height below.
function formatDateTime(iso: string): string {
  return new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

export default function InboxPage() {
  const [replies, setReplies] = useState<MailInboxReplyView[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [campaignFilter, setCampaignFilter] = useState<string>("all");

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
    <div className={MAIL_CAMPAIGN_DETAIL_CONTAINER_CLASS}>
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
          <div className="mb-4 flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
            <h2 className="text-sm font-medium text-muted-foreground">
              Inbox ({filtered.length}
              {filtered.length !== replies.length ? ` of ${replies.length}` : ""})
            </h2>
            <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-center">
              <Input
                placeholder="Search name, email, or campaign…"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                className="w-full min-w-0 sm:w-64"
              />
              {campaigns.length > 1 && (
                <select
                  value={campaignFilter}
                  onChange={(e) => setCampaignFilter(e.target.value)}
                  className="w-full min-w-0 rounded-md border border-input bg-background px-3 py-2 text-sm sm:w-56"
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
              <CardContent className="overflow-x-auto p-0">
                <div className="min-w-[880px] divide-y divide-border">
                  {filtered.map((reply) => {
                    const name = reply.contact_name ?? reply.email;
                    return (
                      <Link
                        key={reply.enrollment_id}
                        href={`/manager/inbox/${reply.enrollment_id}`}
                        className="flex w-full items-center gap-3 whitespace-nowrap px-4 py-2 text-left text-sm transition-colors hover:bg-secondary/40"
                      >
                        <span className="min-w-0 w-[170px] shrink-0 truncate font-medium" title={name}>
                          {name}
                        </span>
                        <span className="shrink-0 rounded-full bg-emerald-100 px-2 py-0.5 text-xs font-medium text-emerald-800">
                          Replied
                        </span>
                        <span
                          className="min-w-0 w-[210px] shrink-0 truncate text-xs text-muted-foreground"
                          title={reply.email}
                        >
                          {reply.email}
                        </span>
                        <span
                          className="min-w-0 w-[220px] shrink-0 truncate text-xs text-muted-foreground"
                          title={reply.campaign_name}
                        >
                          {reply.campaign_name}
                        </span>
                        <span className="ml-auto shrink-0 whitespace-nowrap text-right text-xs text-muted-foreground">
                          {formatDateTime(reply.replied_at)}
                        </span>
                      </Link>
                    );
                  })}
                </div>
              </CardContent>
            </Card>
          )}
        </>
      )}
    </div>
  );
}
