"use client";

import { useEffect, useState } from "react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogClose, DialogDescription, DialogFooter, DialogHeader, DialogPopup, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { ApiError, createClientTouchpoint, updateClientTouchpoint, type Client, type ClientContact, type ClientTouchpoint } from "@/lib/api";
import { formatApiErrorMessage } from "@/lib/add-prospects-flow";
import {
  clientContactDisplayName,
  CONTACT_TYPE_OPTIONS,
  CONTACTED_BY_OPTIONS,
  emptyTouchpointFormState,
  isCuratedContactedBy,
  touchpointCreatePayload,
  touchpointFormStateFromTouchpoint,
  touchpointUpdatePatch,
  type TouchpointFormState,
} from "@/lib/client-crm";

const selectClassName =
  "h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm disabled:cursor-not-allowed disabled:opacity-60";

const OTHER_SENTINEL = "__other__";

function initialContactedBySelection(existingTouchpoint: ClientTouchpoint | null): string {
  const value = existingTouchpoint?.contacted_by ?? "";
  if (!value) return "";
  return isCuratedContactedBy(value) ? value : OTHER_SENTINEL;
}

// One modal, two modes -- same "add vs edit" convention as the other
// Client CRM form modals (client-contact-form-modal.tsx,
// engagement-participant-form-modal.tsx). The Contact picker here is
// deliberately a plain <select> over THIS Client's already-loaded active
// ClientContacts, NOT the shared CrmContactPicker (which searches every
// AstroHub Contact) -- Stage 2A's backend requires crm_contact_id to
// already be an active ClientContact of this same Client, so exposing
// anything wider here would just produce a guaranteed 400 on save.
// "Contacted By" is a curated dropdown (confirmed roster, see
// CONTACTED_BY_OPTIONS in lib/client-crm.ts) with a required free-text
// "Other" escape hatch -- an existing stored value outside the roster
// (e.g. someone since removed from it) still renders via that escape
// hatch rather than being silently dropped.
export function TouchpointFormModal({
  open,
  onOpenChange,
  client,
  activeContacts,
  existingTouchpoint,
  onSaved,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  client: Client;
  activeContacts: ClientContact[];
  existingTouchpoint: ClientTouchpoint | null;
  onSaved: (touchpoint: ClientTouchpoint) => void;
}) {
  const [form, setForm] = useState<TouchpointFormState>(() =>
    existingTouchpoint ? touchpointFormStateFromTouchpoint(existingTouchpoint) : emptyTouchpointFormState()
  );
  const [contactedBySelection, setContactedBySelection] = useState<string>(() => initialContactedBySelection(existingTouchpoint));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setForm(existingTouchpoint ? touchpointFormStateFromTouchpoint(existingTouchpoint) : emptyTouchpointFormState());
      setContactedBySelection(initialContactedBySelection(existingTouchpoint));
      setError(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, existingTouchpoint?.touchpoint_id]);

  function update(patch: Partial<TouchpointFormState>) {
    setForm((prev) => ({ ...prev, ...patch }));
  }

  function handleOpenChange(next: boolean) {
    if (!next && !saving) setError(null);
    onOpenChange(next);
  }

  function handleContactedBySelectionChange(value: string) {
    setContactedBySelection(value);
    if (value !== OTHER_SENTINEL) {
      update({ contactedBy: value });
    } else if (isCuratedContactedBy(form.contactedBy)) {
      update({ contactedBy: "" });
    }
  }

  const canSave = form.occurredAt !== "" && form.contactedBy.trim() !== "" && !saving;

  async function handleSave() {
    if (!canSave) return;
    setSaving(true);
    setError(null);
    try {
      const saved = existingTouchpoint
        ? await updateClientTouchpoint(client.client_id, existingTouchpoint.touchpoint_id, touchpointUpdatePatch(form, existingTouchpoint))
        : await createClientTouchpoint(client.client_id, touchpointCreatePayload(form));
      onSaved(saved);
      onOpenChange(false);
    } catch (err) {
      setError(err instanceof ApiError ? formatApiErrorMessage(err.message) : "Couldn't reach the backend.");
    } finally {
      setSaving(false);
    }
  }

  const contactOptions = activeContacts.filter((c) => !c.archived && c.crm_contact_id);

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogPopup className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{existingTouchpoint ? "Edit Touchpoint" : "Log Touchpoint"}</DialogTitle>
          <DialogDescription>
            {existingTouchpoint ? `Update this interaction with ${client.name}.` : `Record an interaction with ${client.name}.`}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          {error && (
            <Alert variant="destructive">
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}

          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-1.5">
              <label className="text-xs font-medium text-muted-foreground">Date*</label>
              <Input type="date" value={form.occurredAt} onChange={(e) => update({ occurredAt: e.target.value })} disabled={saving} />
            </div>
            <div className="space-y-1.5">
              <label className="text-xs font-medium text-muted-foreground">Contact Type*</label>
              <select
                value={form.contactType}
                onChange={(e) => update({ contactType: e.target.value as TouchpointFormState["contactType"] })}
                disabled={saving}
                className={selectClassName}
              >
                {CONTACT_TYPE_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div className="space-y-1.5">
            <label className="text-xs font-medium text-muted-foreground">Contact</label>
            <select
              value={form.crmContactId ?? ""}
              onChange={(e) => update({ crmContactId: e.target.value || null })}
              disabled={saving || contactOptions.length === 0}
              className={selectClassName}
            >
              <option value="">-- none --</option>
              {contactOptions.map((contact) => (
                <option key={contact.client_contact_id} value={contact.crm_contact_id ?? ""}>
                  {clientContactDisplayName(contact)}
                  {contact.title ? ` — ${contact.title}` : contact.email ? ` — ${contact.email}` : ""}
                </option>
              ))}
            </select>
            {contactOptions.length === 0 && <p className="text-xs text-muted-foreground">No client contacts available.</p>}
          </div>

          <div className="space-y-1.5">
            <label className="text-xs font-medium text-muted-foreground">Contacted By*</label>
            <select
              value={contactedBySelection}
              onChange={(e) => handleContactedBySelectionChange(e.target.value)}
              disabled={saving}
              className={selectClassName}
            >
              <option value="">-- select --</option>
              {CONTACTED_BY_OPTIONS.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
              <option value={OTHER_SENTINEL}>Other...</option>
            </select>
            {contactedBySelection === OTHER_SENTINEL && (
              <Input value={form.contactedBy} onChange={(e) => update({ contactedBy: e.target.value })} placeholder="Name" disabled={saving} />
            )}
          </div>

          <div className="space-y-1.5">
            <label className="text-xs font-medium text-muted-foreground">Activity / Note</label>
            <Input value={form.note} onChange={(e) => update({ note: e.target.value })} placeholder="Dinner proposal sent" disabled={saving} />
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
          <Button type="button" onClick={handleSave} disabled={!canSave}>
            {saving ? "Saving..." : "Save"}
          </Button>
        </DialogFooter>
      </DialogPopup>
    </Dialog>
  );
}
