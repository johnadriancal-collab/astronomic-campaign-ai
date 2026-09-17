"use client";

import { Fragment, useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { AlertTriangle, ArrowLeft, RefreshCw } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Card, CardContent } from "@/components/ui/card";
import {
  ApiError,
  getInboxReply,
  getInboxReplyBody,
  type MailInboxReplyBody,
  type MailInboxReplyView,
} from "@/lib/api";
import { mailCampaignStatusBadgeClass, mailCampaignStatusLabel } from "@/lib/mail";
import { linkifySegments, splitReplyQuote } from "@/lib/reply-formatting";
import { cn } from "@/lib/utils";

// Inbox V2 reply detail page (2026-09-17) -- replaces the earlier
// modal-based reply reading with a dedicated, full-width page (see
// frontend/app/manager/inbox/page.tsx, whose rows now navigate here
// instead of opening a Dialog). Same data path as before: reply
// metadata via GET /mail/inbox/replies/{enrollment_id}, body fetched
// on demand via GET /mail/inbox/replies/{enrollment_id}/body -- never
// eagerly, never persisted, never anything beyond the one already-known
// gmail_message_id tied to this enrollment's MailReply row.

function formatDateTime(iso: string): string {
  return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function LinkifiedText({ text }: { text: string }) {
  const segments = linkifySegments(text);
  return (
    <>
      {segments.map((segment, i) =>
        segment.type === "link" ? (
          <a
            key={i}
            href={segment.href}
            target="_blank"
            rel="noopener noreferrer nofollow"
            className="break-all text-primary underline hover:no-underline"
          >
            {segment.label}
          </a>
        ) : (
          <Fragment key={i}>{segment.value}</Fragment>
        )
      )}
    </>
  );
}

function ReplyBodySection({ enrollmentId }: { enrollmentId: string }) {
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
        <RetryButton onClick={() => setAttempt((n) => n + 1)} />
      </div>
    );
  }

  const result = state.result!;

  if (result.status === "scope_missing" || result.status === "needs_reauth") {
    return (
      <p className="rounded-md bg-secondary/40 px-4 py-3 text-sm text-muted-foreground">
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
        <RetryButton onClick={() => setAttempt((n) => n + 1)} />
      </div>
    );
  }

  if (!result.body_text) {
    return <p className="text-sm text-muted-foreground">(This message has no readable text.)</p>;
  }

  const { newReply, quoted } = splitReplyQuote(result.body_text);

  return (
    <div className="space-y-8">
      <div>
        <p className="mb-3 text-xs font-medium uppercase tracking-wide text-muted-foreground">New reply</p>
        <p className="max-w-[65ch] whitespace-pre-wrap break-words text-base leading-relaxed">
          <LinkifiedText text={newReply || "(empty message)"} />
        </p>
        {result.body_source === "html_converted" && (
          <p className="mt-3 text-xs text-muted-foreground/70">Converted from an HTML-only message.</p>
        )}
      </div>

      {quoted && (
        <div className="border-l-2 border-border pl-4">
          <p className="mb-3 text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Previous / quoted message
          </p>
          <p className="max-w-[65ch] whitespace-pre-wrap break-words text-sm leading-relaxed text-muted-foreground">
            <LinkifiedText text={quoted} />
          </p>
        </div>
      )}
    </div>
  );
}

function RetryButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="inline-flex items-center gap-1.5 text-xs text-muted-foreground underline hover:no-underline"
    >
      <RefreshCw className="h-3 w-3" /> Try again
    </button>
  );
}

export default function InboxReplyDetailPage() {
  const params = useParams<{ enrollment_id: string }>();
  const enrollmentId = params.enrollment_id;

  const [reply, setReply] = useState<MailInboxReplyView | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getInboxReply(enrollmentId)
      .then((data) => {
        if (!cancelled) {
          setReply(data);
          setError(null);
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setError(
            err instanceof ApiError
              ? err.status === 404
                ? "No reply found for this enrollment."
                : `Couldn't load this reply (${err.status}): ${err.message}`
              : "Couldn't reach the backend."
          );
        }
      });
    return () => {
      cancelled = true;
    };
  }, [enrollmentId]);

  return (
    <div className="mx-auto max-w-6xl px-6 py-10">
      <Link href="/manager/inbox" className="mb-4 inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground">
        <ArrowLeft className="h-4 w-4" />
        Back to Inbox
      </Link>

      {error && (
        <Alert variant="destructive" className="mb-6">
          <AlertTriangle />
          <AlertTitle>Couldn&apos;t load this reply</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {!error && reply === null && <div className="text-sm text-muted-foreground">Loading…</div>}

      {!error && reply && (
        <>
          <div className="mb-8">
            <h1 className="font-serif text-2xl font-medium tracking-tight sm:text-3xl">
              {reply.contact_name ?? reply.email}
            </h1>
            <p className="mt-1 text-sm text-muted-foreground">{reply.email}</p>
            <div className="mt-3 flex flex-wrap items-center gap-2 text-sm">
              <Link href={`/manager/campaigns/mail/${reply.mail_campaign_id}`} className="underline hover:no-underline">
                {reply.campaign_name}
              </Link>
              <span
                className={cn(
                  "rounded-full px-2 py-0.5 text-xs font-medium",
                  mailCampaignStatusBadgeClass(reply.campaign_status)
                )}
              >
                {mailCampaignStatusLabel(reply.campaign_status)}
              </span>
              <span className="text-muted-foreground">· Replied {formatDateTime(reply.replied_at)}</span>
            </div>
          </div>

          <div className="grid gap-8 lg:grid-cols-[1fr_320px]">
            <Card>
              <CardContent className="p-6 sm:p-8">
                <ReplyBodySection enrollmentId={reply.enrollment_id} />
              </CardContent>
            </Card>

            <div className="space-y-4">
              <Card>
                <CardContent className="space-y-3 p-5 text-sm">
                  <div>
                    <p className="text-xs text-muted-foreground">Campaign</p>
                    <Link href={`/manager/campaigns/mail/${reply.mail_campaign_id}`} className="underline hover:no-underline">
                      {reply.campaign_name}
                    </Link>
                  </div>
                  <div>
                    <p className="text-xs text-muted-foreground">Sender mailbox</p>
                    <p>{reply.mailbox_email ?? "(mailbox no longer available)"}</p>
                  </div>
                  <div>
                    <p className="text-xs text-muted-foreground">Original subject</p>
                    <p>{reply.subject ?? "(not available)"}</p>
                  </div>
                  <div>
                    <p className="text-xs text-muted-foreground">Replied</p>
                    <p>{formatDateTime(reply.replied_at)}</p>
                  </div>
                  <div>
                    <p className="text-xs text-muted-foreground">Enrollment status</p>
                    <p className="capitalize">{reply.enrollment_status}</p>
                  </div>
                  <div>
                    <p className="text-xs text-muted-foreground">Gmail thread ID</p>
                    <p className="break-all font-mono text-xs">{reply.gmail_thread_id}</p>
                  </div>
                  <div>
                    <p className="text-xs text-muted-foreground">Reply message ID</p>
                    <p className="break-all font-mono text-xs">{reply.gmail_message_id}</p>
                  </div>
                  {reply.skipped_step_numbers.length > 0 && (
                    <div>
                      <p className="text-xs text-muted-foreground">Steps skipped</p>
                      <p>
                        Step{reply.skipped_step_numbers.length > 1 ? "s" : ""} {reply.skipped_step_numbers.join(", ")} — never
                        sent because this lead replied.
                      </p>
                    </div>
                  )}
                  {reply.contact_name && (
                    <div>
                      <p className="text-xs text-muted-foreground">Contact</p>
                      <Link href={`/crm/${reply.crm_contact_id}`} className="underline hover:no-underline">
                        View in Contacts
                      </Link>
                    </div>
                  )}
                </CardContent>
              </Card>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
