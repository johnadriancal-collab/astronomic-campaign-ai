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

// QuickMail-style relative time (2026-09-18) -- "about 7 hours ago",
// "about 1 day ago", "3 days ago", "just now". Computed fresh from the
// real replied_at timestamp every render, never a stored/cached value.
// The exact local date/time (browser's own timezone, via the no-options
// toLocaleString() below) is exposed separately as a title tooltip on
// the cell that renders this -- see the Last reply column.
function formatRelativeTime(iso: string, now: Date = new Date()): string {
  const diffMs = Math.max(0, now.getTime() - new Date(iso).getTime());
  const minutes = Math.floor(diffMs / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `about ${minutes} minute${minutes === 1 ? "" : "s"} ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `about ${hours} hour${hours === 1 ? "" : "s"} ago`;
  const days = Math.floor(hours / 24);
  if (days === 1) return "about 1 day ago";
  return `${days} days ago`;
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
        r.campaign_name.toLowerCase().includes(query) ||
        (r.subject ?? "").toLowerCase().includes(query) ||
        (r.reply_preview ?? "").toLowerCase().includes(query)
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
                placeholder="Search name, email, subject, or campaign…"
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
                <table className="w-full min-w-[1380px] text-sm">
                  <thead className="border-b border-border bg-secondary/30 text-xs">
                    <tr>
                      <th className="w-[160px] px-3 py-2 text-left font-medium text-muted-foreground">Lead</th>
                      <th className="w-[200px] px-3 py-2 text-left font-medium text-muted-foreground">Email</th>
                      <th className="max-w-[300px] px-3 py-2 text-left font-medium text-muted-foreground">Subject</th>
                      <th className="max-w-[300px] px-3 py-2 text-left font-medium text-muted-foreground">Reply</th>
                      <th className="w-[220px] px-3 py-2 text-left font-medium text-muted-foreground">Campaign</th>
                      <th className="w-[140px] px-3 py-2 text-right font-medium text-muted-foreground">Last reply</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {filtered.map((reply) => {
                      const name = reply.contact_name ?? reply.email;
                      const href = `/manager/inbox/${reply.enrollment_id}`;
                      const subject = reply.subject ?? "—";
                      const replyPreview = reply.reply_preview ?? "—";
                      const exactReplyTime = new Date(reply.replied_at).toLocaleString();
                      return (
                        <tr key={reply.enrollment_id} className="hover:bg-secondary/20">
                          <td className="w-[160px] max-w-[160px] p-0">
                            <Link
                              href={href}
                              title={name}
                              className="block truncate whitespace-nowrap px-3 py-1.5 font-medium hover:underline"
                            >
                              {name}
                            </Link>
                          </td>
                          <td className="w-[200px] max-w-[200px] p-0">
                            <Link href={href} title={reply.email} className="block truncate whitespace-nowrap px-3 py-1.5 text-muted-foreground">
                              {reply.email}
                            </Link>
                          </td>
                          <td className="max-w-[300px] p-0">
                            <Link href={href} title={subject} className="block truncate whitespace-nowrap px-3 py-1.5 text-muted-foreground">
                              {subject}
                            </Link>
                          </td>
                          <td className="max-w-[300px] p-0">
                            <Link
                              href={href}
                              title={reply.reply_preview ?? undefined}
                              className="block truncate whitespace-nowrap px-3 py-1.5 text-muted-foreground"
                            >
                              {replyPreview}
                            </Link>
                          </td>
                          <td className="w-[220px] max-w-[220px] p-0">
                            <Link
                              href={href}
                              title={reply.campaign_name}
                              className="block truncate whitespace-nowrap px-3 py-1.5 text-muted-foreground"
                            >
                              {reply.campaign_name}
                            </Link>
                          </td>
                          <td className="w-[140px] p-0">
                            <Link
                              href={href}
                              title={exactReplyTime}
                              className="block whitespace-nowrap px-3 py-1.5 text-right text-muted-foreground"
                            >
                              {formatRelativeTime(reply.replied_at)}
                            </Link>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </CardContent>
            </Card>
          )}
        </>
      )}
    </div>
  );
}
