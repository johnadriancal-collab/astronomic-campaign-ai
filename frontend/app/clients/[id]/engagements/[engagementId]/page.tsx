"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { AlertTriangle, ArrowLeft, Lock } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EngagementFormModal } from "@/components/engagement-form-modal";
import { ApiError, getClient, getClientEngagement, updateClientEngagement, type Client, type Engagement } from "@/lib/api";
import {
  dinnerProgramLabel,
  engagementContractStatusLabel,
  engagementPaymentStatusLabel,
  engagementStatusBadgeClass,
  engagementStatusLabel,
  engagementTypeLabel,
  formatEngagementDate,
} from "@/lib/client-crm";
import { cn } from "@/lib/utils";

// Stage 1E: Overview only. This will eventually grow (a same-day debrief,
// staged relationship follow-ups, guest list, notes/activity -- see the
// Stage 1E investigation report for the planned sections) -- deliberately
// NOT built as tabs yet with empty/fake other sections, since only
// Overview has any real, backend-supported data behind it today. Nothing
// about post-event outcomes is fabricated anywhere on this page -- none
// of that exists yet.
export default function EngagementDetailPage() {
  const params = useParams<{ id: string; engagementId: string }>();
  const clientId = params.id;
  const engagementId = params.engagementId;

  const [client, setClient] = useState<Client | null>(null);
  const [engagement, setEngagement] = useState<Engagement | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editOpen, setEditOpen] = useState(false);
  const [archiving, setArchiving] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  async function load() {
    try {
      const [loadedClient, loadedEngagement] = await Promise.all([
        getClient(clientId),
        getClientEngagement(clientId, engagementId),
      ]);
      setClient(loadedClient);
      setEngagement(loadedEngagement);
      setNotFound(false);
      setError(null);
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        setNotFound(true);
        return;
      }
      setError(err instanceof ApiError ? `Couldn't load this Engagement (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clientId, engagementId]);

  async function handleArchiveToggle() {
    if (!engagement) return;
    const nextArchived = !engagement.archived;
    if (nextArchived) {
      const confirmed = window.confirm(
        `Archive "${engagement.title}"? It will be hidden from the default Engagements list but can be restored anytime.`
      );
      if (!confirmed) return;
    }
    setArchiving(true);
    setActionError(null);
    try {
      setEngagement(await updateClientEngagement(clientId, engagement.engagement_id, { archived: nextArchived }));
    } catch (err) {
      setActionError(err instanceof ApiError ? `Couldn't update this Engagement (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    } finally {
      setArchiving(false);
    }
  }

  if (notFound) {
    return (
      <div className="mx-auto max-w-3xl px-6 py-10">
        <Alert>
          <AlertTriangle />
          <AlertTitle>Engagement not found</AlertTitle>
          <AlertDescription>
            This Engagement doesn&apos;t exist, or may have been removed.{" "}
            <Link href={`/clients/${clientId}`} className="underline underline-offset-2">
              Back to Client
            </Link>
            .
          </AlertDescription>
        </Alert>
      </div>
    );
  }

  if (error) {
    return (
      <div className="mx-auto max-w-3xl px-6 py-10">
        <Alert variant="destructive">
          <AlertTriangle />
          <AlertTitle>Couldn&apos;t load this Engagement</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      </div>
    );
  }

  if (!client || !engagement) {
    return <div className={cn("mx-auto max-w-3xl px-6 py-10 text-sm text-muted-foreground")}>Loading…</div>;
  }

  return (
    <div className="mx-auto max-w-3xl px-6 py-10">
      <Link
        href={`/clients/${client.client_id}`}
        className="mb-4 inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="h-4 w-4" />
        {client.name}
      </Link>

      {engagement.archived && (
        <Alert className="mb-4">
          <Lock className="h-4 w-4" />
          <AlertTitle>Archived</AlertTitle>
          <AlertDescription>
            This Engagement is archived and hidden from the default Engagements list. Its record is unchanged and can be
            restored anytime.
          </AlertDescription>
        </Alert>
      )}

      {actionError && (
        <Alert variant="destructive" className="mb-4">
          <AlertTriangle />
          <AlertDescription>{actionError}</AlertDescription>
        </Alert>
      )}

      <div className="mb-6 flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="font-serif text-2xl font-medium tracking-tight sm:text-3xl">{engagement.title}</h1>
            <Badge variant="secondary" className={engagementStatusBadgeClass(engagement.status)}>
              {engagementStatusLabel(engagement.status)}
            </Badge>
          </div>
          <p className="mt-1 text-sm text-muted-foreground">
            {formatEngagementDate(engagement.engagement_date)}
            {engagement.location ? ` · ${engagement.location}` : ""}
          </p>
        </div>
        <div className="flex shrink-0 gap-2">
          <Button size="sm" onClick={() => setEditOpen(true)}>
            Edit Engagement
          </Button>
          <Button
            size="sm"
            variant="outline"
            onClick={handleArchiveToggle}
            disabled={archiving}
            className={engagement.archived ? undefined : "text-muted-foreground hover:text-destructive"}
          >
            {archiving ? "Saving..." : engagement.archived ? "Restore Engagement" : "Archive Engagement"}
          </Button>
        </div>
      </div>

      <div className="space-y-6">
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">Overview</CardTitle>
          </CardHeader>
          <CardContent className="grid gap-4 sm:grid-cols-2">
            <OverviewField label="Engagement Type" value={engagementTypeLabel(engagement.engagement_type)} />
            <OverviewField label="Dinner Program" value={dinnerProgramLabel(engagement.dinner_program)} />
            <OverviewField label="Date" value={formatEngagementDate(engagement.engagement_date)} />
            <OverviewField label="Location" value={engagement.location || "—"} />
            <OverviewField label="Owner" value={engagement.owner || "—"} />
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-sm">Commercial</CardTitle>
          </CardHeader>
          <CardContent className="grid gap-4 sm:grid-cols-2">
            <OverviewField label="Engagement Fee" value={engagement.fee === null ? "—" : `$${engagement.fee.toLocaleString()}`} />
            <OverviewField label="Contract Status" value={engagementContractStatusLabel(engagement.contract_status)} />
            <OverviewField label="Signed Date" value={formatEngagementDate(engagement.signed_date)} />
            <OverviewField label="Payment Status" value={engagementPaymentStatusLabel(engagement.payment_status)} />
            {engagement.contract_url && (
              <div className="sm:col-span-2">
                <p className="text-xs font-medium text-muted-foreground">Contract URL</p>
                <a
                  href={engagement.contract_url}
                  target="_blank"
                  rel="noreferrer"
                  className="mt-0.5 inline-block break-all text-sm text-foreground hover:underline"
                >
                  {engagement.contract_url}
                </a>
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      <EngagementFormModal
        open={editOpen}
        onOpenChange={setEditOpen}
        client={client}
        existingEngagement={engagement}
        onSaved={setEngagement}
      />
    </div>
  );
}

function OverviewField({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="text-xs font-medium text-muted-foreground">{label}</p>
      <p className="mt-0.5 text-sm">{value}</p>
    </div>
  );
}
