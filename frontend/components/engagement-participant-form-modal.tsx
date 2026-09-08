"use client";

import { useEffect, useState } from "react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogClose, DialogDescription, DialogFooter, DialogHeader, DialogPopup, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { CrmContactPicker } from "@/components/crm-contact-picker";
import {
  ApiError,
  createEngagementParticipant,
  updateEngagementParticipant,
  type Client,
  type CrmContact,
  type Engagement,
  type EngagementParticipant,
} from "@/lib/api";
import { formatApiErrorMessage } from "@/lib/add-prospects-flow";
import { formatContactName, formatContactTitleCompany } from "@/lib/contact-results-view";
import {
  emptyEngagementParticipantFormState,
  engagementParticipantCreatePayload,
  engagementParticipantFormStateFromParticipant,
  engagementParticipantUpdatePatch,
  participantIdentityIsMeaningful,
  PARTICIPANT_ATTENDANCE_STATUS_OPTIONS,
  PARTICIPANT_ROLE_OPTIONS,
  PARTICIPANT_RSVP_STATUS_OPTIONS,
  type EngagementParticipantFormState,
} from "@/lib/client-crm";

const selectClassName =
  "h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm disabled:cursor-not-allowed disabled:opacity-60";

// One modal, two modes -- same "add vs edit" convention as
// client-contact-form-modal.tsx. Unlike that modal, though, "Select
// Existing Contact" is not the ONLY create path -- Stage 1G's own
// approved architecture requires a legitimate unresolved-participant
// exception (historical lists, walk-ins, incomplete records). The picker
// stays the strongly-favored PRIMARY path (shown first, no toggle needed
// to reach it); "Add Unresolved Participant" is a plain secondary text
// link, never a button of equal visual weight -- see this stage's own
// STOP report for why. An already-resolved participant being edited has
// no picker and no free-text identity fields at all: its name/email/etc.
// are a snapshot from the canonical Contact, not something this form
// re-enters (same "no relink here" precedent as ClientContactFormModal).
// An unresolved participant being edited CAN link to a Contact -- that's
// the explicit unresolved-to-resolved transition this stage requires --
// offered as the same kind of secondary toggle, in the other direction.
export function EngagementParticipantFormModal({
  open,
  onOpenChange,
  client,
  engagement,
  existingParticipant,
  onSaved,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  client: Client;
  engagement: Engagement;
  existingParticipant: EngagementParticipant | null;
  onSaved: (participant: EngagementParticipant) => void;
}) {
  const alreadyResolved = existingParticipant !== null && existingParticipant.crm_contact_id !== null;

  const [form, setForm] = useState<EngagementParticipantFormState>(() =>
    existingParticipant ? engagementParticipantFormStateFromParticipant(existingParticipant) : emptyEngagementParticipantFormState()
  );
  // Only meaningful when !alreadyResolved -- which path is currently shown.
  const [usingPicker, setUsingPicker] = useState(!existingParticipant);
  const [selectedContact, setSelectedContact] = useState<CrmContact | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setForm(existingParticipant ? engagementParticipantFormStateFromParticipant(existingParticipant) : emptyEngagementParticipantFormState());
      setUsingPicker(!existingParticipant);
      setSelectedContact(null);
      setError(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, existingParticipant?.participant_id]);

  function update(patch: Partial<EngagementParticipantFormState>) {
    setForm((prev) => ({ ...prev, ...patch }));
  }

  function handleOpenChange(next: boolean) {
    if (!next && !saving) setError(null);
    onOpenChange(next);
  }

  function handleSelectContact(contact: CrmContact | null) {
    setSelectedContact(contact);
    update({ crmContactId: contact ? contact.crm_contact_id : null });
  }

  function switchToPicker() {
    setUsingPicker(true);
    setSelectedContact(null);
    update({ crmContactId: null });
  }

  function switchToUnresolvedFields() {
    setUsingPicker(false);
    setSelectedContact(null);
    update({ crmContactId: null });
  }

  const canSave = alreadyResolved
    ? true
    : usingPicker
      ? form.crmContactId !== null
      : participantIdentityIsMeaningful(form.firstName, form.lastName, form.email);

  async function handleSave() {
    if (!canSave || saving) return;
    setSaving(true);
    setError(null);
    try {
      const saved = existingParticipant
        ? await updateEngagementParticipant(
            client.client_id,
            engagement.engagement_id,
            existingParticipant.participant_id,
            engagementParticipantUpdatePatch(form, existingParticipant)
          )
        : await createEngagementParticipant(client.client_id, engagement.engagement_id, engagementParticipantCreatePayload(form));
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
          <DialogTitle>{existingParticipant ? "Edit Participant" : "Add Participant"}</DialogTitle>
          <DialogDescription>
            {existingParticipant
              ? `Update this person's participation in ${engagement.title}.`
              : `Add someone to ${engagement.title}.`}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          {error && (
            <Alert variant="destructive">
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}

          {alreadyResolved && existingParticipant && (
            <div className="rounded-md border border-input bg-secondary/30 px-3 py-2 text-sm">
              <p className="font-medium">{[existingParticipant.first_name, existingParticipant.last_name].filter(Boolean).join(" ")}</p>
              <p className="text-xs text-muted-foreground">Linked to an existing Contact -- not editable here.</p>
            </div>
          )}

          {!alreadyResolved && usingPicker && (
            <div className="space-y-1.5">
              <label className="text-xs font-medium text-muted-foreground">Contact*</label>
              <CrmContactPicker selected={selectedContact} onSelect={handleSelectContact} />
              <button type="button" onClick={switchToUnresolvedFields} className="text-xs text-muted-foreground underline underline-offset-2">
                Add an unresolved participant instead
              </button>
            </div>
          )}

          {!alreadyResolved && !usingPicker && (
            <div className="space-y-3">
              {existingParticipant && (
                <button type="button" onClick={switchToPicker} className="text-xs text-muted-foreground underline underline-offset-2">
                  Link to an existing Contact instead
                </button>
              )}
              <div className="grid gap-4 sm:grid-cols-2">
                <div className="space-y-1.5">
                  <label className="text-xs font-medium text-muted-foreground">First Name</label>
                  <Input value={form.firstName} onChange={(e) => update({ firstName: e.target.value })} disabled={saving} />
                </div>
                <div className="space-y-1.5">
                  <label className="text-xs font-medium text-muted-foreground">Last Name</label>
                  <Input value={form.lastName} onChange={(e) => update({ lastName: e.target.value })} disabled={saving} />
                </div>
              </div>
              <div className="space-y-1.5">
                <label className="text-xs font-medium text-muted-foreground">Email</label>
                <Input type="email" value={form.email} onChange={(e) => update({ email: e.target.value })} disabled={saving} />
              </div>
              <div className="grid gap-4 sm:grid-cols-2">
                <div className="space-y-1.5">
                  <label className="text-xs font-medium text-muted-foreground">Title</label>
                  <Input value={form.title} onChange={(e) => update({ title: e.target.value })} disabled={saving} />
                </div>
                <div className="space-y-1.5">
                  <label className="text-xs font-medium text-muted-foreground">Company</label>
                  <Input value={form.company} onChange={(e) => update({ company: e.target.value })} disabled={saving} />
                </div>
              </div>
              <p className="text-xs text-muted-foreground">At least a name or email is required.</p>
            </div>
          )}

          {!alreadyResolved && usingPicker && selectedContact && (
            <div className="rounded-md border border-border/60 px-3 py-2 text-xs text-muted-foreground">
              Will link to <span className="font-medium text-foreground">{formatContactName(selectedContact)}</span>
              {formatContactTitleCompany(selectedContact) ? ` · ${formatContactTitleCompany(selectedContact)}` : ""}
            </div>
          )}

          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-1.5">
              <label className="text-xs font-medium text-muted-foreground">Role</label>
              <select
                value={form.role}
                onChange={(e) => update({ role: e.target.value as EngagementParticipantFormState["role"] })}
                disabled={saving}
                className={selectClassName}
              >
                {PARTICIPANT_ROLE_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            </div>
            <label className="flex items-center gap-2 self-end pb-2 text-sm">
              <input
                type="checkbox"
                checked={form.isWalkIn}
                onChange={(e) => update({ isWalkIn: e.target.checked })}
                disabled={saving}
                className="h-3.5 w-3.5 rounded border-input"
              />
              Walk-in
            </label>
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-1.5">
              <label className="text-xs font-medium text-muted-foreground">RSVP</label>
              <select
                value={form.rsvpStatus}
                onChange={(e) => update({ rsvpStatus: e.target.value as EngagementParticipantFormState["rsvpStatus"] })}
                disabled={saving}
                className={selectClassName}
              >
                <option value="">-- none --</option>
                {PARTICIPANT_RSVP_STATUS_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            </div>
            <div className="space-y-1.5">
              <label className="text-xs font-medium text-muted-foreground">Attendance</label>
              <select
                value={form.attendanceStatus}
                onChange={(e) => update({ attendanceStatus: e.target.value as EngagementParticipantFormState["attendanceStatus"] })}
                disabled={saving}
                className={selectClassName}
              >
                <option value="">-- none --</option>
                {PARTICIPANT_ATTENDANCE_STATUS_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
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
          <Button type="button" onClick={handleSave} disabled={!canSave || saving}>
            {saving ? "Saving..." : "Save"}
          </Button>
        </DialogFooter>
      </DialogPopup>
    </Dialog>
  );
}
