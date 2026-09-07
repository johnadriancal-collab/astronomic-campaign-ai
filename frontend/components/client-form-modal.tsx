"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button, buttonVariants } from "@/components/ui/button";
import { Dialog, DialogClose, DialogDescription, DialogFooter, DialogHeader, DialogPopup, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { CrmContactPicker } from "@/components/crm-contact-picker";
import { ApiError, createClient, createClientContact, updateClient, type Client, type CrmContact } from "@/lib/api";
import { formatApiErrorMessage } from "@/lib/add-prospects-flow";
import { formatContactName } from "@/lib/contact-results-view";
import { cn } from "@/lib/utils";
import {
  CLIENT_RELATIONSHIP_CLASSIFICATION_OPTIONS,
  CLIENT_STATUS_OPTIONS,
  clientCreatePayload,
  clientFormStateFromClient,
  clientUpdatePatch,
  emptyClientFormState,
  isClientFormValid,
  type ClientFormState,
} from "@/lib/client-crm";

// Handles BOTH create (`existingClient` null) and edit (populated from the
// selected Client) -- one form, one submit path, same "one modal, two
// modes" convention as add-edit-trigger-modal.tsx. Duplicate Client names
// are never checked or blocked here -- the backend allows them
// deliberately (see ClientCrmService's own docstring); this modal never
// invents a dedup/warning step Stage 1C wasn't asked to build.
export function ClientFormModal({
  open,
  onOpenChange,
  existingClient,
  onSaved,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  existingClient: Client | null;
  onSaved: (client: Client) => void;
}) {
  const [form, setForm] = useState<ClientFormState>(() =>
    existingClient ? clientFormStateFromClient(existingClient) : emptyClientFormState()
  );
  const [primaryContact, setPrimaryContact] = useState<CrmContact | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Set only when Client creation succeeded but linking the selected
  // Primary Contact failed -- see handleSave's own comment for why this
  // is a distinct state from `error` (a plain create/update failure)
  // rather than treated the same way. Never invented for the edit-
  // existing-Client path -- Stage 1D's Primary Contact picker only
  // appears on creation (see the module docstring below `existingClient`
  // guard in the JSX).
  const [partialFailure, setPartialFailure] = useState<{ client: Client; contact: CrmContact; message: string } | null>(
    null
  );

  useEffect(() => {
    if (open) {
      setForm(existingClient ? clientFormStateFromClient(existingClient) : emptyClientFormState());
      setPrimaryContact(null);
      setError(null);
      setPartialFailure(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, existingClient?.client_id]);

  function update(patch: Partial<ClientFormState>) {
    setForm((prev) => ({ ...prev, ...patch }));
  }

  function handleOpenChange(next: boolean) {
    if (!next && !saving) {
      setError(null);
      setPartialFailure(null);
    }
    onOpenChange(next);
  }

  async function linkPrimaryContact(client: Client, contact: CrmContact) {
    try {
      await createClientContact(client.client_id, { crm_contact_id: contact.crm_contact_id, is_primary_contact: true });
      setPartialFailure(null);
      onSaved(client);
      onOpenChange(false);
    } catch (err) {
      // The Client itself already exists at this point -- this must never
      // be reported as if the whole operation failed. Surfacing it as its
      // own distinct state (rather than the plain `error` used for a
      // failed create/update) is what keeps that true: the modal stays
      // open with an honest "created, but..." message and a real way
      // forward (retry, or go to the Client that DOES now exist), instead
      // of silently discarding the failure or claiming full success.
      const message = err instanceof ApiError ? formatApiErrorMessage(err.message) : "Couldn't reach the backend.";
      setPartialFailure({ client, contact, message });
    }
  }

  async function handleSave() {
    if (!isClientFormValid(form) || saving) return;
    setSaving(true);
    setError(null);
    try {
      if (existingClient) {
        const saved = await updateClient(existingClient.client_id, clientUpdatePatch(form, existingClient));
        onSaved(saved);
        onOpenChange(false);
        return;
      }
      const client = await createClient(clientCreatePayload(form));
      if (primaryContact) {
        await linkPrimaryContact(client, primaryContact);
      } else {
        onSaved(client);
        onOpenChange(false);
      }
    } catch (err) {
      setError(err instanceof ApiError ? formatApiErrorMessage(err.message) : "Couldn't reach the backend.");
    } finally {
      setSaving(false);
    }
  }

  if (partialFailure) {
    return (
      <Dialog open={open} onOpenChange={handleOpenChange}>
        <DialogPopup className="max-w-lg">
          <DialogHeader>
            <DialogTitle>Client created</DialogTitle>
            <DialogDescription>
              &quot;{partialFailure.client.name}&quot; was created, but linking {formatContactName(partialFailure.contact)} as
              Primary Contact failed: {partialFailure.message} You can add Contacts from the Client detail page instead.
            </DialogDescription>
          </DialogHeader>

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => linkPrimaryContact(partialFailure.client, partialFailure.contact)}
            >
              Retry linking
            </Button>
            <Link
              href={`/clients/${partialFailure.client.client_id}`}
              onClick={() => {
                onSaved(partialFailure.client);
                onOpenChange(false);
              }}
              className={cn(buttonVariants({}))}
            >
              Go to {partialFailure.client.name}
            </Link>
          </DialogFooter>
        </DialogPopup>
      </Dialog>
    );
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogPopup className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{existingClient ? "Edit Client" : "New Client"}</DialogTitle>
          <DialogDescription>
            {existingClient
              ? "Update this Client's relationship details."
              : "Add an organization to Client CRM -- your relationship/sales record, separate from Contacts, Astronomic's database of people."}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          {error && (
            <Alert variant="destructive">
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}

          <div className="space-y-1.5">
            <label className="text-xs font-medium text-muted-foreground">Company Name*</label>
            <Input
              value={form.name}
              onChange={(e) => update({ name: e.target.value })}
              placeholder="Hive ASMBLD"
              autoFocus
              disabled={saving}
            />
          </div>

          {!existingClient && (
            <div className="space-y-1.5">
              <label className="text-xs font-medium text-muted-foreground">Primary Contact</label>
              <CrmContactPicker selected={primaryContact} onSelect={setPrimaryContact} />
            </div>
          )}

          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-1.5">
              <label className="text-xs font-medium text-muted-foreground">Website</label>
              <Input
                value={form.website}
                onChange={(e) => update({ website: e.target.value })}
                placeholder="https://example.com"
                disabled={saving}
              />
            </div>
            <div className="space-y-1.5">
              <label className="text-xs font-medium text-muted-foreground">Industry</label>
              <Input value={form.industry} onChange={(e) => update({ industry: e.target.value })} disabled={saving} />
            </div>
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-1.5">
              <label className="text-xs font-medium text-muted-foreground">Status</label>
              <select
                value={form.status}
                onChange={(e) => update({ status: e.target.value as ClientFormState["status"] })}
                disabled={saving}
                className="h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm disabled:cursor-not-allowed disabled:opacity-60"
              >
                {CLIENT_STATUS_OPTIONS.map((s) => (
                  <option key={s.value} value={s.value}>
                    {s.label}
                  </option>
                ))}
              </select>
            </div>
            <div className="space-y-1.5">
              <label className="text-xs font-medium text-muted-foreground">Relationship</label>
              <select
                value={form.relationshipClassification}
                onChange={(e) => update({ relationshipClassification: e.target.value as ClientFormState["relationshipClassification"] })}
                disabled={saving}
                className="h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm disabled:cursor-not-allowed disabled:opacity-60"
              >
                <option value="">-- not yet assessed --</option>
                {CLIENT_RELATIONSHIP_CLASSIFICATION_OPTIONS.map((c) => (
                  <option key={c.value} value={c.value}>
                    {c.label}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div className="space-y-1.5">
            <label className="text-xs font-medium text-muted-foreground">Owner</label>
            <Input value={form.owner} onChange={(e) => update({ owner: e.target.value })} placeholder="Chris" disabled={saving} />
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-1.5">
              <label className="text-xs font-medium text-muted-foreground">Next Action</label>
              <Input
                value={form.nextAction}
                onChange={(e) => update({ nextAction: e.target.value })}
                placeholder="Schedule Q1 check-in"
                disabled={saving}
              />
            </div>
            <div className="space-y-1.5">
              <label className="text-xs font-medium text-muted-foreground">Next Action Due</label>
              <Input
                type="date"
                value={form.nextActionDue}
                onChange={(e) => update({ nextActionDue: e.target.value })}
                disabled={saving}
              />
            </div>
          </div>
        </div>

        <DialogFooter>
          <DialogClose
            disabled={saving}
            render={
              <Button type="button" variant="outline">
                Cancel
              </Button>
            }
          />
          <Button type="button" onClick={handleSave} disabled={!isClientFormValid(form) || saving}>
            {saving ? "Saving..." : "Save"}
          </Button>
        </DialogFooter>
      </DialogPopup>
    </Dialog>
  );
}
