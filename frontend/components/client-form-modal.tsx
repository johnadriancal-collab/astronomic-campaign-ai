"use client";

import { useEffect, useState } from "react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogClose, DialogDescription, DialogFooter, DialogHeader, DialogPopup, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { ApiError, createClient, updateClient, type Client } from "@/lib/api";
import { formatApiErrorMessage } from "@/lib/add-prospects-flow";
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
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setForm(existingClient ? clientFormStateFromClient(existingClient) : emptyClientFormState());
      setError(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, existingClient?.client_id]);

  function update(patch: Partial<ClientFormState>) {
    setForm((prev) => ({ ...prev, ...patch }));
  }

  function handleOpenChange(next: boolean) {
    if (!next && !saving) setError(null);
    onOpenChange(next);
  }

  async function handleSave() {
    if (!isClientFormValid(form) || saving) return;
    setSaving(true);
    setError(null);
    try {
      const saved = existingClient
        ? await updateClient(existingClient.client_id, clientUpdatePatch(form, existingClient))
        : await createClient(clientCreatePayload(form));
      onSaved(saved);
      onOpenChange(false);
    } catch (err) {
      setError(err instanceof ApiError ? formatApiErrorMessage(err.message) : "Couldn't reach the backend.");
    } finally {
      setSaving(false);
    }
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
            <label className="text-xs font-medium text-muted-foreground">Client Name*</label>
            <Input
              value={form.name}
              onChange={(e) => update({ name: e.target.value })}
              placeholder="Hive ASMBLD"
              autoFocus
              disabled={saving}
            />
          </div>

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
