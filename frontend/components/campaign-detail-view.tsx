"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { AlertTriangle, ArrowLeft, CheckCircle2, Loader2, Pause, Play, RefreshCw } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import { CampaignStatusBadge } from "@/components/campaign-status-badge";
import { CampaignMessagesSection } from "@/components/campaign-messages-section";
import { DataSourceBadge } from "@/components/data-source-badge";
import { FilterBadges } from "@/components/filter-badges";
import { EmailStepCard } from "@/components/email-step-card";
import {
  ApiError,
  activateCampaign,
  getCampaign,
  getCampaignSequence,
  listCampaignLeads,
  markCampaignReady,
  pauseCampaign,
  syncCampaignSequence,
  type Campaign,
  type CampaignLeadView,
  type EmailSequenceStatus,
  type EmailSequenceWithSteps,
} from "@/lib/api";

type Action = "ready" | "activate" | "pause";

const SEQUENCE_STATUS_META: Record<EmailSequenceStatus, { label: string; variant: "outline" | "secondary" | "default" }> = {
  active: { label: "Active", variant: "default" },
  paused: { label: "Paused", variant: "outline" },
  archived: { label: "Archived", variant: "secondary" },
};

function formatDateTime(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function leadName(lead: CampaignLeadView): string {
  return [lead.first_name, lead.last_name].filter(Boolean).join(" ") || "—";
}

export function CampaignDetailView({ campaignId }: { campaignId: string }) {
  const [campaign, setCampaign] = useState<Campaign | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [leads, setLeads] = useState<CampaignLeadView[] | null>(null);
  const [leadsError, setLeadsError] = useState<string | null>(null);

  const [actionLoading, setActionLoading] = useState<Action | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const [sequence, setSequence] = useState<EmailSequenceWithSteps | null>(null);
  const [sequenceNotSynced, setSequenceNotSynced] = useState(false);
  const [sequenceError, setSequenceError] = useState<string | null>(null);
  const [sequenceLoaded, setSequenceLoaded] = useState(false);
  const [syncLoading, setSyncLoading] = useState(false);
  const [syncError, setSyncError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setError(null);
      try {
        const data = await getCampaign(campaignId);
        if (!cancelled) setCampaign(data);
      } catch (err) {
        if (!cancelled) {
          setError(
            err instanceof ApiError
              ? err.status === 404
                ? "No campaign exists with this ID."
                : `Couldn't load this campaign (${err.status}): ${err.message}`
              : "Couldn't reach the backend to load this campaign."
          );
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [campaignId]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLeadsError(null);
      try {
        const data = await listCampaignLeads(campaignId);
        if (!cancelled) setLeads(data);
      } catch (err) {
        if (!cancelled) {
          setLeadsError(
            err instanceof ApiError
              ? `Couldn't load leads (${err.status}): ${err.message}`
              : "Couldn't reach the backend to load this campaign's leads."
          );
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [campaignId]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setSequenceError(null);
      setSequenceNotSynced(false);
      try {
        const data = await getCampaignSequence(campaignId);
        if (!cancelled) setSequence(data);
      } catch (err) {
        if (!cancelled) {
          if (err instanceof ApiError && err.status === 404) {
            setSequenceNotSynced(true);
          } else {
            setSequenceError(
              err instanceof ApiError
                ? `Couldn't load the sequence (${err.status}): ${err.message}`
                : "Couldn't reach the backend to load the sequence."
            );
          }
        }
      } finally {
        if (!cancelled) setSequenceLoaded(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [campaignId]);

  async function runSync() {
    setSyncLoading(true);
    setSyncError(null);
    try {
      const data = await syncCampaignSequence(campaignId);
      setSequence(data);
      setSequenceNotSynced(false);
    } catch (err) {
      setSyncError(
        err instanceof ApiError
          ? `Sync failed (${err.status}): ${err.message}`
          : "Couldn't reach the backend to sync this sequence."
      );
    } finally {
      setSyncLoading(false);
    }
  }

  async function runAction(action: Action) {
    setActionLoading(action);
    setActionError(null);
    try {
      const fn = action === "ready" ? markCampaignReady : action === "activate" ? activateCampaign : pauseCampaign;
      const updated = await fn(campaignId);
      setCampaign(updated);
    } catch (err) {
      setActionError(
        err instanceof ApiError
          ? `Couldn't ${action} this campaign (${err.status}): ${err.message}`
          : `Couldn't reach the backend to ${action} this campaign.`
      );
    } finally {
      setActionLoading(null);
    }
  }

  return (
    <div className="mx-auto max-w-4xl px-6 py-10">
      <Link
        href="/manager/campaigns"
        className="mb-6 flex items-center gap-1.5 text-sm text-muted-foreground transition-colors hover:text-foreground"
      >
        <ArrowLeft className="h-3.5 w-3.5" />
        All campaigns
      </Link>

      {error && (
        <Alert variant="destructive">
          <AlertTriangle />
          <AlertTitle>Couldn&apos;t load campaign</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {!error && !campaign && (
        <div className="space-y-6">
          <Skeleton className="h-10 w-2/3" />
          <Skeleton className="h-20 w-full" />
          <Skeleton className="h-40 w-full" />
        </div>
      )}

      {!error && campaign && (
        <div className="space-y-8">
          {/* Overview */}
          <div>
            <div className="flex flex-wrap items-center gap-3">
              <h1 className="font-serif text-2xl font-medium tracking-tight sm:text-3xl">
                {campaign.plan.campaign_name}
              </h1>
              <CampaignStatusBadge status={campaign.status} />
            </div>
            <p className="mt-2 max-w-2xl text-sm text-muted-foreground">{campaign.original_prompt}</p>
            <p className="mt-3 text-xs text-muted-foreground/70">
              Created {formatDateTime(campaign.created_at)} · Campaign ID:{" "}
              <span className="font-mono">{campaign.campaign_id}</span>
            </p>

            <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
              <div className="rounded-xl border border-border/60 p-3">
                <p className="text-xs text-muted-foreground">Total matches</p>
                <p className="mt-0.5 text-lg font-semibold">
                  {campaign.total_matches !== null ? campaign.total_matches.toLocaleString() : "—"}
                </p>
              </div>
              <div className="rounded-xl border border-border/60 p-3">
                <p className="text-xs text-muted-foreground">Selected leads</p>
                <p className="mt-0.5 text-lg font-semibold">{campaign.selected_prospect_count}</p>
              </div>
              <div className="rounded-xl border border-border/60 p-3">
                <p className="text-xs text-muted-foreground">Contacts created</p>
                <p className="mt-0.5 text-lg font-semibold">{campaign.contacts_created}</p>
              </div>
              <div className="rounded-xl border border-border/60 p-3">
                <p className="text-xs text-muted-foreground">Contacts enrolled</p>
                <p className="mt-0.5 text-lg font-semibold">{campaign.contacts_enrolled}</p>
              </div>
            </div>
            <div className="mt-2">
              <Badge
                variant="outline"
                className="rounded-full border-border/60 font-normal text-muted-foreground"
              >
                {campaign.activated ? "Activated in Apollo" : "Not yet activated"}
              </Badge>
            </div>
          </div>

          <Separator className="bg-border/60" />

          {/* Filters */}
          <section>
            <h2 className="mb-4 text-sm font-medium text-muted-foreground">Audience filters</h2>
            <FilterBadges filters={campaign.plan.filters} />
          </section>

          <Separator className="bg-border/60" />

          {/* Leads */}
          <section>
            <h2 className="mb-4 text-sm font-medium text-muted-foreground">
              Leads {leads !== null && `· ${leads.length}`}
            </h2>

            {leadsError && (
              <Alert variant="destructive">
                <AlertTriangle />
                <AlertTitle>Couldn&apos;t load leads</AlertTitle>
                <AlertDescription>{leadsError}</AlertDescription>
              </Alert>
            )}

            {!leadsError && leads === null && <Skeleton className="h-32 w-full rounded-xl" />}

            {!leadsError && leads !== null && leads.length === 0 && (
              <p className="text-sm text-muted-foreground">
                No leads yet — leads are created once this campaign is built and its Apollo contacts
                exist.
              </p>
            )}

            {!leadsError && leads !== null && leads.length > 0 && (
              <div className="overflow-hidden rounded-xl border border-border/60">
                <table className="w-full text-sm">
                  <thead className="bg-secondary/40 text-xs text-muted-foreground">
                    <tr>
                      <th className="px-3 py-2 text-left font-medium">Name</th>
                      <th className="px-3 py-2 text-left font-medium">Title</th>
                      <th className="px-3 py-2 text-left font-medium">Company</th>
                      <th className="px-3 py-2 text-left font-medium">Status</th>
                      <th className="px-3 py-2 text-right font-medium">Score</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border/60">
                    {leads.map((lead) => (
                      <tr key={lead.lead_id} className="cursor-pointer hover:bg-secondary/30">
                        <td className="p-0">
                          <Link href={`/manager/leads/${lead.lead_id}`} className="block px-3 py-2.5">
                            {leadName(lead)}
                          </Link>
                        </td>
                        <td className="px-3 py-2.5 text-muted-foreground">{lead.title ?? "—"}</td>
                        <td className="px-3 py-2.5 text-muted-foreground">{lead.company ?? "—"}</td>
                        <td className="px-3 py-2.5">
                          <Badge variant="outline" className="rounded-full font-normal">
                            {lead.lead_status}
                          </Badge>
                        </td>
                        <td className="px-3 py-2.5 text-right text-muted-foreground" title={lead.claude_reason ?? undefined}>
                          {lead.claude_score ?? "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <Separator className="bg-border/60" />

          {/* Deployed Configuration -- OUR snapshot of the sequence steps.
              Sourced from the synced EmailSequenceStep records once a sync
              has happened; falls back to the Builder's plan (clearly
              labeled as such) before the first sync. */}
          <section>
            <h2 className="mb-4 text-sm font-medium text-muted-foreground">
              Deployed Configuration
              {sequence ? ` · ${sequence.steps.length} steps` : campaign.plan.sequence.length > 0 ? ` · ${campaign.plan.sequence.length} steps` : ""}
            </h2>
            {sequence ? (
              sequence.steps.length > 0 ? (
                <div className="grid gap-4 sm:grid-cols-2">
                  {sequence.steps.map((step, i) => (
                    <EmailStepCard key={step.email_sequence_step_id} step={step} index={i} />
                  ))}
                </div>
              ) : (
                <p className="text-sm text-muted-foreground">No sequence steps deployed.</p>
              )
            ) : (
              <>
                {campaign.plan.sequence.length > 0 ? (
                  <div className="grid gap-4 sm:grid-cols-2">
                    {campaign.plan.sequence.map((step, i) => (
                      <EmailStepCard key={`${step.day}-${i}`} step={step} index={i} />
                    ))}
                  </div>
                ) : (
                  <p className="text-sm text-muted-foreground">No sequence steps generated.</p>
                )}
                {sequenceLoaded && sequenceNotSynced && (
                  <p className="mt-3 text-xs text-muted-foreground/70">
                    Showing Campaign Builder&apos;s original plan — sync below to confirm what was
                    actually deployed to Apollo.
                  </p>
                )}
              </>
            )}
          </section>

          <Separator className="bg-border/60" />

          {/* Apollo Status -- a synced mirror of Apollo's own sequence
              state, refreshed only by the explicit Sync button below.
              Deliberately separate from Deployed Configuration above:
              this data can change on Apollo's side (including Apollo
              auto-pausing a sequence) independent of anything we did. */}
          <section>
            <div className="mb-4 flex items-center justify-between">
              <div className="flex items-center gap-2">
                <h2 className="text-sm font-medium text-muted-foreground">Apollo Status</h2>
                {sequence && <DataSourceBadge source="synced_apollo" />}
              </div>
              {campaign.apollo_sequence_id && (
                <Button onClick={runSync} disabled={syncLoading} variant="outline" size="sm" className="gap-1.5">
                  {syncLoading ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <RefreshCw className="h-3.5 w-3.5" />
                  )}
                  Sync now
                </Button>
              )}
            </div>

            {syncError && (
              <Alert variant="destructive" className="mb-3">
                <AlertTriangle />
                <AlertTitle>Sync failed</AlertTitle>
                <AlertDescription>{syncError}</AlertDescription>
              </Alert>
            )}

            {!campaign.apollo_sequence_id && (
              <p className="text-sm text-muted-foreground">
                Build this campaign in Campaign Builder before its Apollo sequence can be synced.
              </p>
            )}

            {campaign.apollo_sequence_id && sequenceError && (
              <Alert variant="destructive">
                <AlertTriangle />
                <AlertTitle>Couldn&apos;t load sequence status</AlertTitle>
                <AlertDescription>{sequenceError}</AlertDescription>
              </Alert>
            )}

            {campaign.apollo_sequence_id && !sequenceError && !sequenceLoaded && (
              <Skeleton className="h-24 w-full rounded-xl" />
            )}

            {campaign.apollo_sequence_id && !sequenceError && sequenceLoaded && sequenceNotSynced && (
              <p className="text-sm text-muted-foreground">
                Not yet synced — click &quot;Sync now&quot; to pull this sequence&apos;s real status
                and engagement stats from Apollo.
              </p>
            )}

            {sequence && (
              <div>
                <div className="mb-4 flex flex-wrap items-center gap-2">
                  <Badge variant={SEQUENCE_STATUS_META[sequence.status].variant} className="rounded-full font-normal">
                    {SEQUENCE_STATUS_META[sequence.status].label}
                  </Badge>
                  {sequence.status_reason && (
                    <span className="text-xs text-muted-foreground">{sequence.status_reason}</span>
                  )}
                  <span className="ml-auto text-xs text-muted-foreground/70">
                    Last synced {sequence.last_synced_at ? formatDateTime(sequence.last_synced_at) : "never"}
                  </span>
                </div>
                <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                  {[
                    { label: "Scheduled", value: sequence.unique_scheduled },
                    { label: "Delivered", value: sequence.unique_delivered },
                    { label: "Opened", value: sequence.unique_opened },
                    { label: "Clicked", value: sequence.unique_clicked },
                    { label: "Replied", value: sequence.unique_replied },
                    { label: "Bounced", value: sequence.unique_bounced },
                    { label: "Unsubscribed", value: sequence.unique_unsubscribed },
                  ].map((stat) => (
                    <div key={stat.label} className="rounded-xl border border-border/60 p-3">
                      <p className="text-xs text-muted-foreground">{stat.label}</p>
                      <p className="mt-0.5 text-lg font-semibold">{stat.value}</p>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </section>

          <Separator className="bg-border/60" />

          {/* Messages -- real, synced EmailMessage/EmailMessageEvent records
              plus any clearly-labeled local test fixtures. Each row's
              DataSourceBadge is the thing that keeps these from ever being
              confused with each other. */}
          <CampaignMessagesSection campaignId={campaignId} leads={leads ?? []} />

          <Separator className="bg-border/60" />

          {/* Campaign actions */}
          <section>
            <h2 className="mb-4 text-sm font-medium text-muted-foreground">Campaign actions</h2>

            {actionError && (
              <Alert variant="destructive" className="mb-3">
                <AlertTriangle />
                <AlertTitle>Action failed</AlertTitle>
                <AlertDescription>{actionError}</AlertDescription>
              </Alert>
            )}

            <div className="flex flex-wrap items-center gap-2">
              {campaign.status === "built" && (
                <Button onClick={() => runAction("ready")} disabled={actionLoading !== null} size="sm" className="gap-1.5">
                  {actionLoading === "ready" ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <CheckCircle2 className="h-3.5 w-3.5" />
                  )}
                  Mark ready to activate
                </Button>
              )}

              {(campaign.status === "ready" || campaign.status === "paused") && (
                <Button onClick={() => runAction("activate")} disabled={actionLoading !== null} size="sm" className="gap-1.5">
                  {actionLoading === "activate" ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <Play className="h-3.5 w-3.5" />
                  )}
                  Activate
                </Button>
              )}

              {campaign.status === "active" && (
                <Button
                  onClick={() => runAction("pause")}
                  disabled={actionLoading !== null}
                  variant="outline"
                  size="sm"
                  className="gap-1.5"
                >
                  {actionLoading === "pause" ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <Pause className="h-3.5 w-3.5" />
                  )}
                  Pause
                </Button>
              )}

              {(campaign.status === "draft" || campaign.status === "searched" || campaign.status === "building") && (
                <p className="text-sm text-muted-foreground">Build this campaign in Campaign Builder first.</p>
              )}

              {campaign.status === "failed" && (
                <p className="text-sm text-muted-foreground">
                  The build failed — retry it from Campaign Builder.
                </p>
              )}
            </div>

            <div className="mt-4 flex flex-wrap gap-1.5">
              <Badge variant="outline" className="rounded-full font-normal text-muted-foreground/60">
                Edit filters/templates — Coming next
              </Badge>
              <Badge variant="outline" className="rounded-full font-normal text-muted-foreground/60">
                Completed status — Coming next
              </Badge>
            </div>
          </section>

          <Separator className="bg-border/60" />

          <section>
            <h2 className="mb-3 text-xs font-medium uppercase tracking-wide text-muted-foreground/70">
              Debug info
            </h2>
            <div className="grid gap-x-6 gap-y-1.5 text-xs text-muted-foreground/80 sm:grid-cols-2">
              <p>
                Apollo list ID: <span className="font-mono">{campaign.apollo_list_id ?? "—"}</span>
              </p>
              <p>
                Apollo sequence ID:{" "}
                <span className="font-mono">{campaign.apollo_sequence_id ?? "—"}</span>
              </p>
              <p>Errors logged: {campaign.errors.length}</p>
            </div>
          </section>
        </div>
      )}
    </div>
  );
}
