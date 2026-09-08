"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { AlertTriangle, ArrowLeft, Lock, Plus } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ClientFormModal } from "@/components/client-form-modal";
import { ClientContactFormModal } from "@/components/client-contact-form-modal";
import { EngagementFormModal } from "@/components/engagement-form-modal";
import {
  ApiError,
  getClient,
  listClientContacts,
  listClientEngagements,
  updateClient,
  updateClientContact,
  type Client,
  type ClientContact,
  type Engagement,
} from "@/lib/api";
import {
  clientContactDisplayName,
  clientRelationshipClassificationBadgeClass,
  clientRelationshipClassificationLabel,
  clientStatusBadgeClass,
  clientStatusLabel,
  dinnerTypeLabel,
  engagementStatusBadgeClass,
  engagementStatusLabel,
  engagementTypeLabel,
  formatClientDate,
  formatEngagementDate,
} from "@/lib/client-crm";
import { cn } from "@/lib/utils";

// Stage 1C shipped Overview only; Stage 1D added a real Contacts section;
// Stage 1E adds a real Engagements section now that Engagement CRUD
// actually exists (see this stage's own STOP report). Still deliberately
// NOT a tabbed page with other empty/fake sections (a same-day debrief,
// staged relationship follow-ups, notes/activity) -- only Overview,
// Contacts, and Engagements have any real, backend-supported data behind
// them today. This page shows no fabricated numbers for anything not
// built yet -- a fabricated "0" would misrepresent "not built" as
// "genuinely zero".
export default function ClientDetailPage() {
  const params = useParams<{ id: string }>();
  const clientId = params.id;

  const [client, setClient] = useState<Client | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editOpen, setEditOpen] = useState(false);
  const [archiving, setArchiving] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const [contacts, setContacts] = useState<ClientContact[] | null>(null);
  const [contactsError, setContactsError] = useState<string | null>(null);
  const [contactModalOpen, setContactModalOpen] = useState(false);
  const [editingContact, setEditingContact] = useState<ClientContact | null>(null);
  const [contactActionError, setContactActionError] = useState<string | null>(null);
  const [busyContactId, setBusyContactId] = useState<string | null>(null);

  const [engagements, setEngagements] = useState<Engagement[] | null>(null);
  const [engagementsError, setEngagementsError] = useState<string | null>(null);
  const [engagementModalOpen, setEngagementModalOpen] = useState(false);

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

  async function loadContacts() {
    try {
      setContacts(await listClientContacts(clientId));
      setContactsError(null);
    } catch (err) {
      setContactsError(err instanceof ApiError ? `Couldn't load Contacts (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    }
  }

  async function loadEngagements() {
    try {
      setEngagements(await listClientEngagements(clientId));
      setEngagementsError(null);
    } catch (err) {
      setEngagementsError(
        err instanceof ApiError ? `Couldn't load Engagements (${err.status}): ${err.message}` : "Couldn't reach the backend."
      );
    }
  }

  useEffect(() => {
    load();
    loadContacts();
    loadEngagements();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clientId]);

  function handleEngagementSaved(engagement: Engagement) {
    setEngagements((prev) => {
      if (!prev) return [engagement];
      const index = prev.findIndex((e) => e.engagement_id === engagement.engagement_id);
      if (index === -1) return [...prev, engagement];
      const next = [...prev];
      next[index] = engagement;
      return next;
    });
  }

  function handleContactSaved(contact: ClientContact) {
    setContacts((prev) => {
      if (!prev) return [contact];
      const index = prev.findIndex((c) => c.client_contact_id === contact.client_contact_id);
      if (index === -1) return [...prev, contact];
      const next = [...prev];
      next[index] = contact;
      return next;
    });
  }

  async function handleSetPrimary(contact: ClientContact) {
    if (!client) return;
    setBusyContactId(contact.client_contact_id);
    setContactActionError(null);
    try {
      // A refetch (rather than just patching this one row locally) is
      // what picks up the OTHER Contact that just lost Primary status --
      // the backend clears it atomically in the same operation, but only
      // this row's own response comes back from updateClientContact.
      await updateClientContact(client.client_id, contact.client_contact_id, { is_primary_contact: true });
      await loadContacts();
    } catch (err) {
      setContactActionError(
        err instanceof ApiError ? `Couldn't set this Contact as Primary (${err.status}): ${err.message}` : "Couldn't reach the backend."
      );
    } finally {
      setBusyContactId(null);
    }
  }

  async function handleArchiveContactToggle(contact: ClientContact) {
    if (!client) return;
    const nextArchived = !contact.archived;
    if (nextArchived) {
      const confirmed = window.confirm(
        `Remove ${clientContactDisplayName(contact)} from ${client.name}'s Contacts? This can be undone anytime.`
      );
      if (!confirmed) return;
    }
    setBusyContactId(contact.client_contact_id);
    setContactActionError(null);
    try {
      const updated = await updateClientContact(client.client_id, contact.client_contact_id, { archived: nextArchived });
      handleContactSaved(updated);
    } catch (err) {
      setContactActionError(err instanceof ApiError ? `Couldn't update this Contact (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    } finally {
      setBusyContactId(null);
    }
  }

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

        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0">
            <CardTitle className="text-sm">Contacts</CardTitle>
            <Button
              size="sm"
              variant="outline"
              className="gap-1.5"
              onClick={() => {
                setEditingContact(null);
                setContactModalOpen(true);
              }}
            >
              <Plus className="h-3.5 w-3.5" />
              Add Contact
            </Button>
          </CardHeader>
          <CardContent>
            {contactsError && (
              <Alert variant="destructive" className="mb-3">
                <AlertDescription>{contactsError}</AlertDescription>
              </Alert>
            )}
            {contactActionError && (
              <Alert variant="destructive" className="mb-3">
                <AlertDescription>{contactActionError}</AlertDescription>
              </Alert>
            )}

            {contacts === null && !contactsError && <p className="text-sm text-muted-foreground">Loading…</p>}

            {contacts !== null && contacts.length === 0 && (
              <p className="text-sm text-muted-foreground">No Contacts linked yet.</p>
            )}

            {contacts !== null && contacts.length > 0 && (
              <ul className="space-y-3">
                {contacts.map((contact) => (
                  <li key={contact.client_contact_id} className="flex flex-wrap items-start justify-between gap-3 rounded-lg border border-border/60 p-3">
                    <div>
                      <div className="flex flex-wrap items-center gap-1.5">
                        {contact.crm_contact_id ? (
                          <Link href={`/crm/${contact.crm_contact_id}`} className="font-medium hover:underline">
                            {clientContactDisplayName(contact)}
                          </Link>
                        ) : (
                          <span className="font-medium">{clientContactDisplayName(contact)}</span>
                        )}
                        {contact.is_primary_contact && (
                          <Badge variant="secondary" className="bg-emerald-100 text-emerald-800">
                            Primary
                          </Badge>
                        )}
                        {contact.is_decision_maker && (
                          <Badge variant="secondary" className="bg-violet-100 text-violet-800">
                            Decision Maker
                          </Badge>
                        )}
                        {contact.archived && (
                          <Badge variant="secondary" className="bg-secondary text-muted-foreground">
                            Archived
                          </Badge>
                        )}
                      </div>
                      <p className="text-xs text-muted-foreground">{contact.title || "—"}</p>
                      {contact.email && <p className="text-xs text-muted-foreground">{contact.email}</p>}
                    </div>
                    <div className="flex shrink-0 flex-wrap gap-2">
                      {!contact.archived && !contact.is_primary_contact && (
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={busyContactId === contact.client_contact_id}
                          onClick={() => handleSetPrimary(contact)}
                        >
                          Set Primary
                        </Button>
                      )}
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={busyContactId === contact.client_contact_id}
                        onClick={() => {
                          setEditingContact(contact);
                          setContactModalOpen(true);
                        }}
                      >
                        Edit
                      </Button>
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={busyContactId === contact.client_contact_id}
                        className={contact.archived ? undefined : "text-muted-foreground hover:text-destructive"}
                        onClick={() => handleArchiveContactToggle(contact)}
                      >
                        {contact.archived ? "Restore" : "Remove"}
                      </Button>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0">
            <CardTitle className="text-sm">Engagements</CardTitle>
            <Button size="sm" variant="outline" className="gap-1.5" onClick={() => setEngagementModalOpen(true)}>
              <Plus className="h-3.5 w-3.5" />
              Add Engagement
            </Button>
          </CardHeader>
          <CardContent>
            {engagementsError && (
              <Alert variant="destructive" className="mb-3">
                <AlertDescription>{engagementsError}</AlertDescription>
              </Alert>
            )}

            {engagements === null && !engagementsError && <p className="text-sm text-muted-foreground">Loading…</p>}

            {engagements !== null && engagements.length === 0 && (
              <p className="text-sm text-muted-foreground">No Engagements recorded yet.</p>
            )}

            {engagements !== null && engagements.length > 0 && (
              <ul className="space-y-3">
                {engagements.map((engagement) => (
                  <li key={engagement.engagement_id}>
                    <Link
                      href={`/clients/${client.client_id}/engagements/${engagement.engagement_id}`}
                      className="block rounded-lg border border-border/60 p-3 hover:bg-secondary/30"
                    >
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span className="font-medium">{engagement.title}</span>
                        {engagement.archived && (
                          <Badge variant="secondary" className="bg-secondary text-muted-foreground">
                            Archived
                          </Badge>
                        )}
                      </div>
                      <p className="text-xs text-muted-foreground">
                        {formatEngagementDate(engagement.engagement_date)}
                        {engagement.location ? ` · ${engagement.location}` : ""}
                      </p>
                      <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
                        <span>{engagementTypeLabel(engagement.engagement_type)}</span>
                        {engagement.dinner_type && (
                          <>
                            <span>·</span>
                            <span>{dinnerTypeLabel(engagement.dinner_type)}</span>
                          </>
                        )}
                        <span>·</span>
                        <Badge variant="secondary" className={engagementStatusBadgeClass(engagement.status)}>
                          {engagementStatusLabel(engagement.status)}
                        </Badge>
                      </div>
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      </div>

      <ClientFormModal open={editOpen} onOpenChange={setEditOpen} existingClient={client} onSaved={setClient} />
      <ClientContactFormModal
        open={contactModalOpen}
        onOpenChange={setContactModalOpen}
        client={client}
        existingContact={editingContact}
        onSaved={handleContactSaved}
      />
      <EngagementFormModal
        open={engagementModalOpen}
        onOpenChange={setEngagementModalOpen}
        client={client}
        existingEngagement={null}
        onSaved={handleEngagementSaved}
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
