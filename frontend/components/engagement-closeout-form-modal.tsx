"use client";

import { useEffect, useState } from "react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogClose, DialogDescription, DialogFooter, DialogHeader, DialogPopup, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { ApiError, createEngagementCloseout, updateEngagementCloseout, type Client, type Engagement, type EngagementCloseout } from "@/lib/api";
import { formatApiErrorMessage } from "@/lib/add-prospects-flow";
import {
  emptyEngagementCloseoutFormState,
  engagementCloseoutCreatePayload,
  engagementCloseoutFormStateFromCloseout,
  engagementCloseoutUpdatePatch,
  type EngagementCloseoutFormState,
} from "@/lib/client-crm";

// One modal, two modes -- same convention as client-form-modal.tsx/
// client-contact-form-modal.tsx/engagement-form-modal.tsx. Every field is
// optional (there is no required field on a Closeout, unlike Engagement's
// title), so Save is never disabled here.
export function EngagementCloseoutFormModal({
  open,
  onOpenChange,
  client,
  engagement,
  existingCloseout,
  onSaved,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  client: Client;
  engagement: Engagement;
  existingCloseout: EngagementCloseout | null;
  onSaved: (closeout: EngagementCloseout) => void;
}) {
  const [form, setForm] = useState<EngagementCloseoutFormState>(() =>
    existingCloseout ? engagementCloseoutFormStateFromCloseout(existingCloseout) : emptyEngagementCloseoutFormState()
  );
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setForm(existingCloseout ? engagementCloseoutFormStateFromCloseout(existingCloseout) : emptyEngagementCloseoutFormState());
      setError(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, existingCloseout?.closeout_id]);

  function update(patch: Partial<EngagementCloseoutFormState>) {
    setForm((prev) => ({ ...prev, ...patch }));
  }

  function handleOpenChange(next: boolean) {
    if (!next && !saving) setError(null);
    onOpenChange(next);
  }

  async function handleSave() {
    if (saving) return;
    setSaving(true);
    setError(null);
    try {
      const saved = existingCloseout
        ? await updateEngagementCloseout(client.client_id, engagement.engagement_id, engagementCloseoutUpdatePatch(form, existingCloseout))
        : await createEngagementCloseout(client.client_id, engagement.engagement_id, engagementCloseoutCreatePayload(form));
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
      <DialogPopup className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>{existingCloseout ? "Edit Closeout" : "Add Closeout"}</DialogTitle>
          <DialogDescription>
            {existingCloseout ? `Update the same-day closeout for "${engagement.title}".` : `Record the same-day closeout for "${engagement.title}".`}
          </DialogDescription>
        </DialogHeader>

        <div className="max-h-[65vh] space-y-6 overflow-y-auto pr-1">
          {error && (
            <Alert variant="destructive">
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}

          <div className="rounded-lg border border-border/60 p-3">
            <p className="mb-3 text-xs font-medium text-muted-foreground">Turnout</p>
            <div className="grid gap-3 sm:grid-cols-5">
              <div className="space-y-1.5">
                <label className="text-xs font-medium text-muted-foreground">Confirmed</label>
                <Input type="number" min={0} value={form.confirmedGuestCount} onChange={(e) => update({ confirmedGuestCount: e.target.value })} disabled={saving} />
              </div>
              <div className="space-y-1.5">
                <label className="text-xs font-medium text-muted-foreground">Attended</label>
                <Input type="number" min={0} value={form.attendedCount} onChange={(e) => update({ attendedCount: e.target.value })} disabled={saving} />
              </div>
              <div className="space-y-1.5">
                <label className="text-xs font-medium text-muted-foreground">No-Shows</label>
                <Input type="number" min={0} value={form.noShowCount} onChange={(e) => update({ noShowCount: e.target.value })} disabled={saving} />
              </div>
              <div className="space-y-1.5">
                <label className="text-xs font-medium text-muted-foreground">Cancelled</label>
                <Input type="number" min={0} value={form.cancelledCount} onChange={(e) => update({ cancelledCount: e.target.value })} disabled={saving} />
              </div>
              <div className="space-y-1.5">
                <label className="text-xs font-medium text-muted-foreground">Unexpected/Walk-ins</label>
                <Input
                  type="number"
                  min={0}
                  value={form.unexpectedAttendeeCount}
                  onChange={(e) => update({ unexpectedAttendeeCount: e.target.value })}
                  disabled={saving}
                />
              </div>
            </div>
          </div>

          <div className="rounded-lg border border-border/60 p-3">
            <p className="mb-3 text-xs font-medium text-muted-foreground">Dinner Review</p>
            <div className="space-y-3">
              <div className="space-y-1.5">
                <label className="text-xs font-medium text-muted-foreground">Guest Quality</label>
                <Textarea value={form.guestQuality} onChange={(e) => update({ guestQuality: e.target.value })} disabled={saving} />
              </div>
              <div className="space-y-1.5">
                <label className="text-xs font-medium text-muted-foreground">Dinner Dynamics</label>
                <Textarea value={form.dinnerDynamics} onChange={(e) => update({ dinnerDynamics: e.target.value })} disabled={saving} />
              </div>
              <div className="space-y-1.5">
                <label className="text-xs font-medium text-muted-foreground">Initial Client Experience</label>
                <Textarea
                  value={form.initialClientExperience}
                  onChange={(e) => update({ initialClientExperience: e.target.value })}
                  disabled={saving}
                />
              </div>
            </div>
          </div>

          <div className="rounded-lg border border-border/60 p-3">
            <p className="mb-3 text-xs font-medium text-muted-foreground">Outcomes</p>
            <div className="space-y-3">
              <div className="space-y-1.5">
                <label className="text-xs font-medium text-muted-foreground">Immediate Outcomes</label>
                <Textarea value={form.immediateOutcomes} onChange={(e) => update({ immediateOutcomes: e.target.value })} disabled={saving} />
              </div>
              <div className="space-y-1.5">
                <label className="text-xs font-medium text-muted-foreground">Notable Signals</label>
                <Textarea value={form.notableSignals} onChange={(e) => update({ notableSignals: e.target.value })} disabled={saving} />
              </div>
              <div className="space-y-1.5">
                <label className="text-xs font-medium text-muted-foreground">Issues / Problems</label>
                <Textarea value={form.issues} onChange={(e) => update({ issues: e.target.value })} disabled={saving} />
              </div>
              <div className="space-y-1.5">
                <label className="text-xs font-medium text-muted-foreground">Referrals</label>
                <Textarea value={form.referrals} onChange={(e) => update({ referrals: e.target.value })} disabled={saving} />
              </div>
              <div className="space-y-1.5">
                <label className="text-xs font-medium text-muted-foreground">Future Opportunities</label>
                <Textarea value={form.futureOpportunities} onChange={(e) => update({ futureOpportunities: e.target.value })} disabled={saving} />
              </div>
            </div>
          </div>

          <div className="rounded-lg border border-border/60 p-3">
            <p className="mb-3 text-xs font-medium text-muted-foreground">Internal</p>
            <div className="space-y-3">
              <div className="space-y-1.5">
                <label className="text-xs font-medium text-muted-foreground">Internal Closeout Notes</label>
                <Textarea value={form.internalNotes} onChange={(e) => update({ internalNotes: e.target.value })} disabled={saving} />
              </div>
              <div className="grid gap-3 sm:grid-cols-2">
                <div className="space-y-1.5">
                  <label className="text-xs font-medium text-muted-foreground">Completed By</label>
                  <Input value={form.completedBy} onChange={(e) => update({ completedBy: e.target.value })} placeholder="Chris" disabled={saving} />
                </div>
                <div className="space-y-1.5">
                  <label className="text-xs font-medium text-muted-foreground">Completed At</label>
                  <Input type="date" value={form.completedAt} onChange={(e) => update({ completedAt: e.target.value })} disabled={saving} />
                </div>
              </div>
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
          <Button type="button" onClick={handleSave} disabled={saving}>
            {saving ? "Saving..." : "Save"}
          </Button>
        </DialogFooter>
      </DialogPopup>
    </Dialog>
  );
}
