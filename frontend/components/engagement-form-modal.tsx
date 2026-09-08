"use client";

import { useEffect, useState } from "react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogClose, DialogDescription, DialogFooter, DialogHeader, DialogPopup, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { ApiError, createClientEngagement, updateClientEngagement, type Client, type Engagement, type EngagementType } from "@/lib/api";
import { formatApiErrorMessage } from "@/lib/add-prospects-flow";
import {
  DINNER_TYPE_OPTIONS,
  ENGAGEMENT_CONTRACT_STATUS_OPTIONS,
  ENGAGEMENT_PAYMENT_STATUS_OPTIONS,
  ENGAGEMENT_STATUS_OPTIONS,
  ENGAGEMENT_TYPE_OPTIONS,
  emptyEngagementFormState,
  engagementCreatePayload,
  engagementFormStateFromEngagement,
  engagementUpdatePatch,
  isDinnerShapedEngagementType,
  isEngagementFormValid,
  type EngagementFormState,
} from "@/lib/client-crm";

// One modal, two modes -- same convention as client-form-modal.tsx/
// client-contact-form-modal.tsx. This form deliberately has no field for
// the reserved event-platform reference on Engagement (Stage 1E's own
// approved scope: leave it out of the user-facing form for now rather
// than build a picker) -- that reference stays reachable only via direct
// API access until a real linkage UI is designed.
export function EngagementFormModal({
  open,
  onOpenChange,
  client,
  existingEngagement,
  onSaved,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  client: Client;
  existingEngagement: Engagement | null;
  onSaved: (engagement: Engagement) => void;
}) {
  const [form, setForm] = useState<EngagementFormState>(() =>
    existingEngagement ? engagementFormStateFromEngagement(existingEngagement) : emptyEngagementFormState()
  );
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setForm(existingEngagement ? engagementFormStateFromEngagement(existingEngagement) : emptyEngagementFormState());
      setError(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, existingEngagement?.engagement_id]);

  function update(patch: Partial<EngagementFormState>) {
    setForm((prev) => ({ ...prev, ...patch }));
  }

  function handleEngagementTypeChange(value: EngagementType) {
    // The form clears/hides Dinner Type for a non-dinner engagement type --
    // the backend remains authoritative regardless (see engagementCreatePayload/
    // engagementUpdatePatch), this is just keeping the UI honest about
    // what will actually be saved.
    update({ engagementType: value, dinnerType: isDinnerShapedEngagementType(value) ? form.dinnerType : "" });
  }

  function handleOpenChange(next: boolean) {
    if (!next && !saving) setError(null);
    onOpenChange(next);
  }

  async function handleSave() {
    if (!isEngagementFormValid(form) || saving) return;
    setSaving(true);
    setError(null);
    try {
      const saved = existingEngagement
        ? await updateClientEngagement(client.client_id, existingEngagement.engagement_id, engagementUpdatePatch(form, existingEngagement))
        : await createClientEngagement(client.client_id, engagementCreatePayload(form));
      onSaved(saved);
      onOpenChange(false);
    } catch (err) {
      setError(err instanceof ApiError ? formatApiErrorMessage(err.message) : "Couldn't reach the backend.");
    } finally {
      setSaving(false);
    }
  }

  const showDinnerType = isDinnerShapedEngagementType(form.engagementType);

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogPopup className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{existingEngagement ? "Edit Engagement" : "Add Engagement"}</DialogTitle>
          <DialogDescription>
            {existingEngagement ? `Update this Engagement for ${client.name}.` : `Record a new Engagement for ${client.name}.`}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          {error && (
            <Alert variant="destructive">
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}

          <div className="space-y-1.5">
            <label className="text-xs font-medium text-muted-foreground">Title*</label>
            <Input
              value={form.title}
              onChange={(e) => update({ title: e.target.value })}
              placeholder="SF Investor Dinner"
              autoFocus
              disabled={saving}
            />
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-1.5">
              <label className="text-xs font-medium text-muted-foreground">Engagement Type</label>
              <select
                value={form.engagementType}
                onChange={(e) => handleEngagementTypeChange(e.target.value as EngagementType)}
                disabled={saving}
                className="h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm disabled:cursor-not-allowed disabled:opacity-60"
              >
                {ENGAGEMENT_TYPE_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            </div>
            {showDinnerType && (
              <div className="space-y-1.5">
                <label className="text-xs font-medium text-muted-foreground">Dinner Type</label>
                <select
                  value={form.dinnerType}
                  onChange={(e) => update({ dinnerType: e.target.value as EngagementFormState["dinnerType"] })}
                  disabled={saving}
                  className="h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm disabled:cursor-not-allowed disabled:opacity-60"
                >
                  <option value="">-- none --</option>
                  {DINNER_TYPE_OPTIONS.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </select>
              </div>
            )}
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-1.5">
              <label className="text-xs font-medium text-muted-foreground">Date</label>
              <Input type="date" value={form.engagementDate} onChange={(e) => update({ engagementDate: e.target.value })} disabled={saving} />
            </div>
            <div className="space-y-1.5">
              <label className="text-xs font-medium text-muted-foreground">Location</label>
              <Input
                value={form.location}
                onChange={(e) => update({ location: e.target.value })}
                placeholder="The Battery, San Francisco"
                disabled={saving}
              />
            </div>
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-1.5">
              <label className="text-xs font-medium text-muted-foreground">Status</label>
              <select
                value={form.status}
                onChange={(e) => update({ status: e.target.value as EngagementFormState["status"] })}
                disabled={saving}
                className="h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm disabled:cursor-not-allowed disabled:opacity-60"
              >
                {ENGAGEMENT_STATUS_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            </div>
            <div className="space-y-1.5">
              <label className="text-xs font-medium text-muted-foreground">Owner</label>
              <Input value={form.owner} onChange={(e) => update({ owner: e.target.value })} placeholder="Chris" disabled={saving} />
            </div>
          </div>

          <div className="rounded-lg border border-border/60 p-3">
            <p className="mb-3 text-xs font-medium text-muted-foreground">Commercial details</p>
            <div className="space-y-3">
              <div className="grid gap-4 sm:grid-cols-2">
                <div className="space-y-1.5">
                  <label className="text-xs font-medium text-muted-foreground">Engagement Fee</label>
                  <Input
                    type="number"
                    value={form.fee}
                    onChange={(e) => update({ fee: e.target.value })}
                    placeholder="5000"
                    disabled={saving}
                  />
                </div>
                <div className="space-y-1.5">
                  <label className="text-xs font-medium text-muted-foreground">Contract Status</label>
                  <select
                    value={form.contractStatus}
                    onChange={(e) => update({ contractStatus: e.target.value as EngagementFormState["contractStatus"] })}
                    disabled={saving}
                    className="h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm disabled:cursor-not-allowed disabled:opacity-60"
                  >
                    {ENGAGEMENT_CONTRACT_STATUS_OPTIONS.map((o) => (
                      <option key={o.value} value={o.value}>
                        {o.label}
                      </option>
                    ))}
                  </select>
                </div>
              </div>
              <div className="space-y-1.5">
                <label className="text-xs font-medium text-muted-foreground">Contract URL</label>
                <Input
                  value={form.contractUrl}
                  onChange={(e) => update({ contractUrl: e.target.value })}
                  placeholder="https://drive.example.com/contract"
                  disabled={saving}
                />
              </div>
              <div className="grid gap-4 sm:grid-cols-2">
                <div className="space-y-1.5">
                  <label className="text-xs font-medium text-muted-foreground">Signed Date</label>
                  <Input type="date" value={form.signedDate} onChange={(e) => update({ signedDate: e.target.value })} disabled={saving} />
                </div>
                <div className="space-y-1.5">
                  <label className="text-xs font-medium text-muted-foreground">Payment Status</label>
                  <select
                    value={form.paymentStatus}
                    onChange={(e) => update({ paymentStatus: e.target.value as EngagementFormState["paymentStatus"] })}
                    disabled={saving}
                    className="h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm disabled:cursor-not-allowed disabled:opacity-60"
                  >
                    {ENGAGEMENT_PAYMENT_STATUS_OPTIONS.map((o) => (
                      <option key={o.value} value={o.value}>
                        {o.label}
                      </option>
                    ))}
                  </select>
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
          <Button type="button" onClick={handleSave} disabled={!isEngagementFormValid(form) || saving}>
            {saving ? "Saving..." : "Save"}
          </Button>
        </DialogFooter>
      </DialogPopup>
    </Dialog>
  );
}
