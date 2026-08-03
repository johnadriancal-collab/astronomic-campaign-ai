"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { AlertTriangle, ArrowLeft } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import { CampaignStatusBadge } from "@/components/campaign-status-badge";
import { ApiError, getLead, type LeadDetail } from "@/lib/api";

function formatDateTime(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function leadName(lead: LeadDetail): string {
  return [lead.first_name, lead.last_name].filter(Boolean).join(" ") || "Unnamed lead";
}

export function LeadDetailView({ leadId }: { leadId: string }) {
  const [lead, setLead] = useState<LeadDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    (async () => {
      setError(null);
      try {
        const data = await getLead(leadId);
        if (!cancelled) setLead(data);
      } catch (err) {
        if (!cancelled) {
          setError(
            err instanceof ApiError
              ? err.status === 404
                ? "No lead exists with this ID."
                : `Couldn't load this lead (${err.status}): ${err.message}`
              : "Couldn't reach the backend to load this lead."
          );
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [leadId]);

  return (
    <div className="mx-auto max-w-3xl px-6 py-10">
      <Link
        href="/manager/leads"
        className="mb-6 flex items-center gap-1.5 text-sm text-muted-foreground transition-colors hover:text-foreground"
      >
        <ArrowLeft className="h-3.5 w-3.5" />
        All leads
      </Link>

      {error && (
        <Alert variant="destructive">
          <AlertTriangle />
          <AlertTitle>Couldn&apos;t load lead</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {!error && !lead && (
        <div className="space-y-6">
          <Skeleton className="h-10 w-1/2" />
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-32 w-full" />
        </div>
      )}

      {!error && lead && (
        <div className="space-y-8">
          <div>
            <div className="flex flex-wrap items-center gap-3">
              <h1 className="text-2xl font-semibold tracking-tight sm:text-3xl">{leadName(lead)}</h1>
              <Badge variant="outline" className="rounded-full font-normal">
                {lead.status}
              </Badge>
            </div>
            <p className="mt-2 text-sm text-muted-foreground">
              {[lead.title, lead.company].filter(Boolean).join(" · ") || "No title or company on file"}
            </p>
            <p className="mt-3 text-xs text-muted-foreground/70">
              Lead ID: <span className="font-mono">{lead.lead_id}</span> · Created{" "}
              {formatDateTime(lead.created_at)}
            </p>
          </div>

          <Separator className="bg-border/60" />

          <section>
            <h2 className="mb-4 text-sm font-medium text-muted-foreground">Contact info</h2>
            <div className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
              <p>
                <span className="text-muted-foreground">Email:</span> {lead.email ?? "—"}
              </p>
              <p>
                <span className="text-muted-foreground">Title:</span> {lead.title ?? "—"}
              </p>
              <p>
                <span className="text-muted-foreground">Company:</span> {lead.company ?? "—"}
              </p>
              <p>
                <span className="text-muted-foreground">Company domain:</span>{" "}
                {lead.company_domain ?? "—"}
              </p>
            </div>
          </section>

          <Separator className="bg-border/60" />

          <section>
            <h2 className="mb-4 text-sm font-medium text-muted-foreground">
              Campaigns · {lead.campaigns.length}
            </h2>
            {lead.campaigns.length > 0 ? (
              <div className="space-y-2">
                {lead.campaigns.map((membership) => (
                  <Link
                    key={membership.campaign_id}
                    href={`/manager/campaigns/${membership.campaign_id}`}
                    className="block rounded-xl border border-border/60 px-4 py-3 transition-colors hover:bg-secondary/40"
                  >
                    <div className="flex items-center justify-between">
                      <span className="text-sm font-medium">{membership.campaign_name}</span>
                      <div className="flex items-center gap-2">
                        <CampaignStatusBadge status={membership.campaign_status} />
                        <span className="text-xs text-muted-foreground">
                          Added {formatDateTime(membership.added_at)}
                        </span>
                      </div>
                    </div>
                    {membership.claude_score !== null && (
                      <p className="mt-1.5 text-xs text-muted-foreground">
                        Claude score for this campaign: <span className="font-medium text-foreground">{membership.claude_score}</span>
                        {membership.claude_reason && ` — ${membership.claude_reason}`}
                      </p>
                    )}
                  </Link>
                ))}
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">Not linked to any campaign.</p>
            )}
          </section>

          <Separator className="bg-border/60" />

          <section>
            <h2 className="mb-3 text-xs font-medium uppercase tracking-wide text-muted-foreground/70">
              Debug info
            </h2>
            <div className="grid gap-x-6 gap-y-1.5 text-xs text-muted-foreground/80 sm:grid-cols-2">
              <p>
                Apollo contact ID: <span className="font-mono">{lead.apollo_contact_id}</span>
              </p>
            </div>
          </section>
        </div>
      )}
    </div>
  );
}
