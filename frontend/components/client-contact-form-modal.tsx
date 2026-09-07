"use client";

import { useEffect, useState } from "react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogClose, DialogDescription, DialogFooter, DialogHeader, DialogPopup, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { CrmContactPicker } from "@/components/crm-contact-picker";
import { ApiError, createClientContact, updateClientContact, type Client, type ClientContact, type CrmContact } from "@/lib/api";
import { formatApiErrorMessage } from "@/lib/add-prospects-flow";
import {
  clientContactCreatePayload,
  clientContactFormStateFromContact,
  clientContactUpdatePatch,
  emptyClientContactFormState,
  type ClientContactFormState,
} from "@/lib/client-crm";

// One modal, two modes -- same "add vs edit" convention as
// client-form-modal.tsx. Create mode requires picking an existing
// CrmContact via CrmContactPicker (no free-text path -- see this stage's
// own STOP report); edit mode has no picker at all, since re-linking an
// existing relationship to a different person isn't supported in V1
// (archive this relationship and add a new one instead).
export function ClientContactFormModal({
  open,
  onOpenChange,
  client,
  existingContact,
  onSaved,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  client: Client;
  existingContact: ClientContact | null;
  onSaved: (contact: ClientContact) => void;
}) {
  const [form, setForm] = useState<ClientContactFormState>(() =>
    existingContact ? clientContactFormStateFromContact(existingContact) : emptyClientContactFormState()
  );
  const [selectedContact, setSelectedContact] = useState<CrmContact | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setForm(existingContact ? clientContactFormStateFromContact(existingContact) : emptyClientContactFormState());
      setSelectedContact(null);
      setError(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, existingContact?.client_contact_id]);

  function update(patch: Partial<ClientContactFormState>) {
    setForm((prev) => ({ ...prev, ...patch }));
  }

  function handleOpenChange(next: boolean) {
    if (!next && !saving) setError(null);
    onOpenChange(next);
  }

  const canSave = existingContact ? true : selectedContact !== null;

  async function handleSave() {
    if (!canSave || saving) return;
    setSaving(true);
    setError(null);
    try {
      const saved = existingContact
        ? await updateClientContact(client.client_id, existingContact.client_contact_id, clientContactUpdatePatch(form, existingContact))
        : await createClientContact(client.client_id, clientContactCreatePayload(form, selectedContact!.crm_contact_id));
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
          <DialogTitle>{existingContact ? "Edit Contact" : "Add Contact"}</DialogTitle>
          <DialogDescription>
            {existingContact
              ? "Update this person's relationship to " + client.name + "."
              : "Link an existing Astronomic Contact to " + client.name + "."}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          {error && (
            <Alert variant="destructive">
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}

          {!existingContact && (
            <div className="space-y-1.5">
              <label className="text-xs font-medium text-muted-foreground">Contact*</label>
              <CrmContactPicker selected={selectedContact} onSelect={setSelectedContact} />
            </div>
          )}

          <div className="space-y-1.5">
            <label className="text-xs font-medium text-muted-foreground">Title at {client.name}</label>
            <Input value={form.title} onChange={(e) => update({ title: e.target.value })} placeholder="VP of BD" disabled={saving} />
          </div>

          <div className="space-y-1.5">
            <label className="text-xs font-medium text-muted-foreground">Role notes</label>
            <Input
              value={form.roleNotes}
              onChange={(e) => update({ roleNotes: e.target.value })}
              placeholder="Introduced us to the CFO"
              disabled={saving}
            />
          </div>

          <div className="flex flex-wrap gap-4">
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={form.isPrimaryContact}
                onChange={(e) => update({ isPrimaryContact: e.target.checked })}
                disabled={saving}
                className="h-3.5 w-3.5 rounded border-input"
              />
              Primary Contact
            </label>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={form.isDecisionMaker}
                onChange={(e) => update({ isDecisionMaker: e.target.checked })}
                disabled={saving}
                className="h-3.5 w-3.5 rounded border-input"
              />
              Decision Maker
            </label>
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
          <Button type="button" onClick={handleSave} disabled={!canSave || saving}>
            {saving ? "Saving..." : "Save"}
          </Button>
        </DialogFooter>
      </DialogPopup>
    </Dialog>
  );
}
