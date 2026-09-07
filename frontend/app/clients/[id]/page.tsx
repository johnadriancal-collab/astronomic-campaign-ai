"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { AlertTriangle, ArrowLeft, Lock } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ClientFormModal } from "@/components/client-form-modal";
import { ApiError, getClient, updateClient, type Client } from "@/lib/api";
import {
  clientRelationshipClassificationBadgeClass,
  clientRelationshipClassificationLabel,
  clientStatusBadgeClass,
  clientStatusLabel,
  formatClientDate,
} from "@/lib/client-crm";
import { cn } from "@/lib/utils";

// Stage 1C: Overview only. This will eventually grow into a tabbed page
// (see the Client CRM investigation report for the planned sections) --
// deliberately NOT built as tabs yet with empty/fake other sections, since
// only Overview has any real, backend-supported data behind it today.
// This page shows no fabricated numbers for anything not built yet --
// a fabricated "0" would misrepresent "not built" as "genuinely zero".
export default function ClientDetailPage() {
  const params = useParams<{ id: string }>();
  const clientId = params.id;

  const [client, setClient] = useState<Client | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editOpen, setEditOpen] = useState(false);
  const [archiving, setArchiving] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  async function load() {
    try {
      setClient(await getClient(clientId));
      setNotFound(false);
      setError(null);
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        setNotFound(true);
        return;
      }
      setError(err instanceof ApiError ? `Couldn't load this Client (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clientId]);

  async function handleArchiveToggle() {
    if (!client) return;
    const nextArchived = !client.archived;
    if (nextArchived) {
      const confirmed = window.confirm(`Archive "${client.name}"? It will be hidden from the default Clients list but can be restored anytime.`);
      if (!confirmed) return;
    }
    setArchiving(true);
    setActionError(null);
    try {
      setClient(await updateClient(client.client_id, { archived: nextArchived }));
    } catch (err) {
      setActionError(err instanceof ApiError ? `Couldn't update this Client (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    } finally {
      setArchiving(false);
    }
  }

  if (notFound) {
    return (
      <div className="mx-auto max-w-3xl px-6 py-10">
        <Alert>
          <AlertTriangle />
          <AlertTitle>Client not found</AlertTitle>
          <AlertDescription>
            This Client doesn&apos;t exist, or may have been removed.{" "}
            <Link href="/clients" className="underline underline-offset-2">
              Back to Clients
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
          <AlertTitle>Couldn&apos;t load this Client</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      </div>
    );
  }

  if (!client) {
    return <div className={cn("mx-auto max-w-3xl px-6 py-10 text-sm text-muted-foreground")}>Loading…</div>;
  }

  return (
    <div className="mx-auto max-w-3xl px-6 py-10">
      <Link href="/clients" className="mb-4 inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground">
        <ArrowLeft className="h-4 w-4" />
        All Clients
      </Link>

      {client.archived && (
        <Alert className="mb-4">
          <Lock className="h-4 w-4" />
          <AlertTitle>Archived</AlertTitle>
          <AlertDescription>
            This Client is archived and hidden from the default Clients list. Its record is unchanged and can be restored
            anytime.
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
            <h1 className="font-serif text-2xl font-medium tracking-tight sm:text-3xl">{client.name}</h1>
            <Badge variant="secondary" className={clientStatusBadgeClass(client.status)}>
              {clientStatusLabel(client.status)}
            </Badge>
            {client.relationship_classification && (
              <Badge variant="secondary" className={clientRelationshipClassificationBadgeClass(client.relationship_classification)}>
                {clientRelationshipClassificationLabel(client.relationship_classification)}
              </Badge>
            )}
          </div>
          {client.website && (
            <a
              href={client.website}
              target="_blank"
              rel="noreferrer"
              className="mt-1 inline-block text-sm text-muted-foreground hover:text-foreground hover:underline"
            >
              {client.website}
            </a>
          )}
        </div>
        <div className="flex shrink-0 gap-2">
          <Button size="sm" onClick={() => setEditOpen(true)}>
            Edit Client
          </Button>
          <Button
            size="sm"
            variant="outline"
            onClick={handleArchiveToggle}
            disabled={archiving}
            className={client.archived ? undefined : "text-muted-foreground hover:text-destructive"}
          >
            {archiving ? "Saving..." : client.archived ? "Restore Client" : "Archive Client"}
          </Button>
        </div>
      </div>

      <div className="space-y-6">
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">Overview</CardTitle>
          </CardHeader>
          <CardContent className="grid gap-4 sm:grid-cols-2">
            <OverviewField label="Relationship" value={clientRelationshipClassificationLabel(client.relationship_classification)} />
            <OverviewField label="Owner" value={client.owner || "—"} />
            <OverviewField label="Industry" value={client.industry || "—"} />
            <OverviewField label="Next Action" value={client.next_action || "—"} />
            <OverviewField label="Next Action Due" value={formatClientDate(client.next_action_due)} />
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-sm">Client Information</CardTitle>
          </CardHeader>
          <CardContent className="grid gap-4 sm:grid-cols-2">
            <OverviewField label="Created" value={formatClientDate(client.created_at)} />
            <OverviewField label="Last Updated" value={formatClientDate(client.updated_at)} />
          </CardContent>
        </Card>
      </div>

      <ClientFormModal open={editOpen} onOpenChange={setEditOpen} existingClient={client} onSaved={setClient} />
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
