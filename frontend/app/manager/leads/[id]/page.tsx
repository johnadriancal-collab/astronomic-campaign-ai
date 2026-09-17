"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { AlertTriangle, ArrowLeft, MessageSquare } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Card, CardContent } from "@/components/ui/card";
import { ApiError, getMailLead, type MailLeadDetail } from "@/lib/api";
import {
  mailCampaignStatusBadgeClass,
  mailCampaignStatusLabel,
  mailEnrollmentStatusBadgeClass,
  mailEnrollmentStatusLabel,
  mailExecutionStepStatusBadgeClass,
  mailExecutionStepStatusLabel,
} from "@/lib/mail";
import { MAIL_CAMPAIGN_DETAIL_CONTAINER_CLASS } from "@/lib/mail-campaign-layout";
import { cn } from "@/lib/utils";

// Campaign Manager Lead detail page V1 (2026-09-17). Same dedicated-
// full-page convention as the Inbox reply detail page (no modal) --
// route stays /manager/leads/[id], but `id` is now a crm_contact_id
// resolved via GET /mail/leads/{crm_contact_id}, not the old Apollo
// lead_id. A "Lead" is a Campaign-Manager-shaped VIEW of an existing
// CrmContact -- this page always links back to the real Contacts
// record, never duplicates or replaces it.

function formatDateTime(iso: string): string {
  return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export default function LeadDetailPage() {
  const params = useParams<{ id: string }>();
  const crmContactId = params.id;

  const [lead, setLead] = useState<MailLeadDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getMailLead(crmContactId)
      .then((data) => {
        if (!cancelled) {
          setLead(data);
          setError(null);
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setError(
            err instanceof ApiError
              ? err.status === 404
                ? "No Campaign Manager Lead found for this contact."
                : `Couldn't load this Lead (${err.status}): ${err.message}`
              : "Couldn't reach the backend."
          );
        }
      });
    return () => {
      cancelled = true;
    };
  }, [crmContactId]);

  return (
    <div className={MAIL_CAMPAIGN_DETAIL_CONTAINER_CLASS}>
      <Link href="/manager/leads" className="mb-4 inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground">
        <ArrowLeft className="h-4 w-4" />
        Back to Leads
      </Link>

      {error && (
        <Alert variant="destructive" className="mb-6">
          <AlertTriangle />
          <AlertTitle>Couldn&apos;t load this Lead</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {!error && lead === null && <div className="text-sm text-muted-foreground">Loading…</div>}

      {!error && lead && (
        <>
          <div className="mb-8 flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
            <div>
              <h1 className="font-serif text-2xl font-medium tracking-tight sm:text-3xl">
                {lead.name ?? lead.email ?? "(unknown)"}
              </h1>
              <p className="mt-1 text-sm text-muted-foreground">{lead.email}</p>
              {(lead.company || lead.title) && (
                <p className="mt-0.5 text-sm text-muted-foreground">
                  {[lead.title, lead.company].filter(Boolean).join(" · ")}
                </p>
              )}
              <Link href={`/crm/${lead.crm_contact_id}`} className="mt-2 inline-block text-sm underline hover:no-underline">
                View in Contacts
              </Link>
            </div>
            <span
              className={cn("shrink-0 rounded-full px-2.5 py-1 text-xs font-medium", mailEnrollmentStatusBadgeClass(lead.status))}
            >
              {mailEnrollmentStatusLabel(lead.status)}
            </span>
          </div>

          <div className="mb-8 grid grid-cols-2 gap-4 sm:grid-cols-4">
            <Card>
              <CardContent className="p-4">
                <p className="text-xs text-muted-foreground">Campaigns</p>
                <p className="mt-1 text-2xl font-medium">{lead.campaigns_count}</p>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="p-4">
                <p className="text-xs text-muted-foreground">Replies</p>
                <p className="mt-1 text-2xl font-medium">{lead.replies_count}</p>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="p-4">
                <p className="text-xs text-muted-foreground">First campaign</p>
                <p className="mt-1 text-sm font-medium">{formatDateTime(lead.first_campaign_at)}</p>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="p-4">
                <p className="text-xs text-muted-foreground">Last activity</p>
                <p className="mt-1 text-sm font-medium">{formatDateTime(lead.last_activity_at)}</p>
              </CardContent>
            </Card>
          </div>

          <h2 className="mb-3 text-sm font-medium text-muted-foreground">Campaign history</h2>
          <div className="space-y-4">
            {lead.campaign_history.map((entry) => (
              <Card key={entry.enrollment_id}>
                <CardContent className="p-5">
                  <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                    <div className="flex flex-wrap items-center gap-2">
                      <Link href={`/manager/campaigns/mail/${entry.mail_campaign_id}`} className="font-medium hover:underline">
                        {entry.campaign_name}
                      </Link>
                      <span
                        className={cn(
                          "rounded-full px-2 py-0.5 text-xs font-medium",
                          mailCampaignStatusBadgeClass(entry.campaign_status)
                        )}
                      >
                        {mailCampaignStatusLabel(entry.campaign_status)}
                      </span>
                      <span
                        className={cn(
                          "rounded-full px-2 py-0.5 text-xs font-medium",
                          mailEnrollmentStatusBadgeClass(entry.enrollment_status)
                        )}
                      >
                        {mailEnrollmentStatusLabel(entry.enrollment_status)}
                      </span>
                    </div>
                    {entry.has_reply && entry.reply_enrollment_id && (
                      <Link
                        href={`/manager/inbox/${entry.reply_enrollment_id}`}
                        className="inline-flex items-center gap-1 text-xs text-emerald-700 hover:underline"
                      >
                        <MessageSquare className="h-3.5 w-3.5" />
                        View reply in Inbox
                      </Link>
                    )}
                  </div>

                  <div className="mb-3 grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-muted-foreground sm:grid-cols-4">
                    <div>
                      <span className="block text-muted-foreground/70">Sender mailbox</span>
                      {entry.mailbox_email ?? "—"}
                    </div>
                    <div>
                      <span className="block text-muted-foreground/70">Enrolled</span>
                      {formatDateTime(entry.enrolled_at)}
                    </div>
                    {entry.replied_at && (
                      <div>
                        <span className="block text-muted-foreground/70">Replied</span>
                        {formatDateTime(entry.replied_at)}
                      </div>
                    )}
                  </div>

                  {entry.steps.length > 0 && (
                    <div className="space-y-1 border-t border-border pt-3">
                      {entry.steps.map((step) => (
                        <div key={step.step_number} className="flex items-center justify-between gap-2 text-sm">
                          <span className="text-muted-foreground">
                            Step {step.step_number}
                            {step.subject ? ` — ${step.subject}` : ""}
                          </span>
                          <span className="flex items-center gap-2">
                            {step.sent_at && (
                              <span className="text-xs text-muted-foreground">{formatDateTime(step.sent_at)}</span>
                            )}
                            <span
                              className={cn(
                                "rounded-full px-2 py-0.5 text-xs font-medium",
                                mailExecutionStepStatusBadgeClass(step.status)
                              )}
                            >
                              {mailExecutionStepStatusLabel(step.status)}
                            </span>
                          </span>
                        </div>
                      ))}
                    </div>
                  )}
                </CardContent>
              </Card>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
