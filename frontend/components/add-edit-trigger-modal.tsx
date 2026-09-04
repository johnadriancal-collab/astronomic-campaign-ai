"use client";

import { useEffect, useState } from "react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogClose, DialogDescription, DialogFooter, DialogHeader, DialogPopup, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { SendDaysPicker } from "@/components/send-days-picker";
import {
  ApiError,
  createMailLeadStartTrigger,
  updateMailLeadStartTrigger,
  type MailLeadStartTrigger,
} from "@/lib/api";
import { formatApiErrorMessage } from "@/lib/add-prospects-flow";
import { formatTimeOfDay } from "@/lib/mail";
import { isTriggerFormClientValid, triggerFormValidationError, type TriggerFormState } from "@/lib/mail-trigger";

// Handles BOTH create (`existingTrigger` null) and edit (populated from
// the selected row) -- one form, one submit path, matching this codebase's
// general "one modal, two modes" convention rather than two near-duplicate
// components. Client-side validation here is deliberately minimal (see
// triggerFormValidationError's own docstring) -- the backend's own
// Stage 5E duplicate-schedule check is surfaced verbatim as `error`
// rather than pre-guessed here, so the two rules can never drift apart.
export function AddEditTriggerModal({
  open,
  onOpenChange,
  mailCampaignId,
  existingTrigger,
  onSaved,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  mailCampaignId: string;
  existingTrigger: MailLeadStartTrigger | null;
  onSaved: (trigger: MailLeadStartTrigger) => void;
}) {
  const [form, setForm] = useState<TriggerFormState>(() => formFromTrigger(existingTrigger));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Re-seed the form whenever a DIFFERENT trigger is opened for edit (or
  // the modal reopens for a fresh "create") -- keyed on open/trigger_id
  // rather than a raw effect-syncs-state pattern, since the form is
  // otherwise the user's own uncontrolled draft while the modal is open.
  useEffect(() => {
    if (open) {
      setForm(formFromTrigger(existingTrigger));
      setError(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, existingTrigger?.trigger_id]);

  function update(patch: Partial<TriggerFormState>) {
    setForm((prev) => ({ ...prev, ...patch }));
  }

  function handleOpenChange(next: boolean) {
    if (!next && !saving) setError(null);
    onOpenChange(next);
  }

  async function handleSave() {
    const validationError = triggerFormValidationError(form);
    if (validationError || saving) return;
    setSaving(true);
    setError(null);
    try {
      const input = {
        weekdays: form.weekdays,
        local_time: form.localTime,
        leads_to_start: Number(form.leadsToStart),
        enabled: form.enabled,
      };
      const saved = existingTrigger
        ? await updateMailLeadStartTrigger(mailCampaignId, existingTrigger.trigger_id, input)
        : await createMailLeadStartTrigger(mailCampaignId, input);
      onSaved(saved);
      onOpenChange(false);
    } catch (err) {
      // formatApiErrorMessage (not the raw `${status}: ${message}` some
      // older tabs on this page still use) -- extracts just the
      // backend's own `detail` string (e.g. the Stage 5E duplicate-
      // schedule message) instead of showing the raw `{"detail": "..."}`
      // JSON envelope, matching this codebase's newer-modal convention
      // (see add-prospects-modal.tsx's own identical usage).
      setError(err instanceof ApiError ? formatApiErrorMessage(err.message) : "Couldn't reach the backend.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogPopup className="max-w-md">
        <DialogHeader>
          <DialogTitle>{existingTrigger ? "Edit lead-start trigger" : "Add lead-start trigger"}</DialogTitle>
          <DialogDescription>
            Choose when new prospects begin their sequence. Email delivery still follows your sending hours, mailbox
            limits, and pacing.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-5">
          {error && (
            <Alert variant="destructive">
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}

          <div className="space-y-1.5">
            <label className="text-xs font-medium text-muted-foreground">Days</label>
            <SendDaysPicker days={form.weekdays} onChange={(weekdays) => update({ weekdays })} disabled={saving} />
          </div>

          <div className="space-y-1.5">
            <label className="text-xs font-medium text-muted-foreground">Time</label>
            <input
              type="time"
              value={form.localTime}
              onChange={(e) => update({ localTime: e.target.value })}
              disabled={saving}
              className="h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm disabled:cursor-not-allowed disabled:opacity-60"
            />
          </div>

          <div className="space-y-1.5">
            <label className="text-xs font-medium text-muted-foreground">Leads to start</label>
            <Input
              type="number"
              min={1}
              step={1}
              value={form.leadsToStart}
              onChange={(e) => update({ leadsToStart: e.target.value })}
              disabled={saving}
              placeholder="20"
            />
          </div>

          <div className="flex items-center justify-between gap-4 rounded-md border border-border/60 p-3">
            <div>
              <p className="text-sm font-medium">Enabled</p>
              <p className="text-xs text-muted-foreground">A disabled trigger never starts new leads.</p>
            </div>
            <Switch
              checked={form.enabled}
              onCheckedChange={(v) => update({ enabled: Boolean(v) })}
              disabled={saving}
              className="mt-0.5 shrink-0"
            />
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
          <Button type="button" onClick={handleSave} disabled={!isTriggerFormClientValid(form) || saving}>
            {saving ? "Saving..." : "Save"}
          </Button>
        </DialogFooter>
      </DialogPopup>
    </Dialog>
  );
}

function formFromTrigger(trigger: MailLeadStartTrigger | null): TriggerFormState {
  if (!trigger) {
    return { weekdays: [], localTime: "", leadsToStart: "", enabled: true };
  }
  return {
    weekdays: trigger.weekdays,
    localTime: formatTimeOfDay(trigger.local_time),
    leadsToStart: String(trigger.leads_to_start),
    enabled: trigger.enabled,
  };
}
