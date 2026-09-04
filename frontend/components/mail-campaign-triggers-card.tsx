"use client";

import { useState } from "react";
import { AlertTriangle, Lock, Plus } from "lucide-react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogClose, DialogDescription, DialogFooter, DialogHeader, DialogPopup, DialogTitle } from "@/components/ui/dialog";
import { Switch } from "@/components/ui/switch";
import { AddEditTriggerModal } from "@/components/add-edit-trigger-modal";
import {
  ApiError,
  deleteMailLeadStartTrigger,
  updateMailLeadStartTrigger,
  type MailCampaign,
  type MailLeadStartTrigger,
} from "@/lib/api";
import { formatApiErrorMessage } from "@/lib/add-prospects-flow";
import { formatSendingDays, formatTimeOfDay } from "@/lib/mail";
import {
  formatLeadsToStart,
  formatTriggerTimezoneCopy,
  formatWaitingToStartCopy,
  hasZeroEnabledTriggers,
  isTriggerEditable,
  needsFirstTriggerConfirmation,
} from "@/lib/mail-trigger";

// The Lead-start Triggers card -- a SECOND, independent Card below the
// existing sending-hours Card in the Schedule tab (see
// mail-campaign-schedule-tab.tsx, left completely unchanged). Editability
// here is governed ONLY by isTriggerEditable(campaign.status) -- DRAFT/
// READY/ACTIVE/PAUSED, matching MailTriggerService's own
// _TRIGGER_CONFIGURABLE_STATUSES -- deliberately never the surrounding
// tab's own DRAFT-only `editable` flag, so Triggers stay editable on
// READY/ACTIVE/PAUSED while the sending-hours Card above it stays locked.
export function MailCampaignTriggersCard({
  campaign,
  triggers,
  triggersError,
  workloadPending,
  onTriggersRefresh,
}: {
  campaign: MailCampaign;
  triggers: MailLeadStartTrigger[];
  // Only ever set by a POST-mutation refresh failure (see
  // handleSaved/handleToggleEnabled/handleDelete below) -- the INITIAL
  // fetch is covered by the page's own top-level loading/error gate
  // (see page.tsx's `if (error)`/`if (!campaign)` guards), so `triggers`
  // is always a real, successfully-fetched list by the time this card
  // ever renders at all.
  triggersError: string | null;
  workloadPending: number | null;
  onTriggersRefresh: () => Promise<void>;
}) {
  const [addEditOpen, setAddEditOpen] = useState(false);
  const [editingTrigger, setEditingTrigger] = useState<MailLeadStartTrigger | null>(null);
  const [firstTriggerConfirmOpen, setFirstTriggerConfirmOpen] = useState(false);
  const [pendingTriggerIds, setPendingTriggerIds] = useState<Set<string>>(new Set());
  const [rowError, setRowError] = useState<string | null>(null);

  const editable = isTriggerEditable(campaign.status);

  function setPending(triggerId: string, pending: boolean) {
    setPendingTriggerIds((prev) => {
      const next = new Set(prev);
      if (pending) next.add(triggerId);
      else next.delete(triggerId);
      return next;
    });
  }

  function openAddFlow() {
    if (needsFirstTriggerConfirmation(campaign.lead_start_mode)) {
      setFirstTriggerConfirmOpen(true);
      return;
    }
    setEditingTrigger(null);
    setAddEditOpen(true);
  }

  function handleFirstTriggerContinue() {
    // Closing dialog A and opening dialog B in the SAME synchronous
    // update (React batches both state changes into one render) leaves
    // Base UI's exit transition for A stuck mid-animation -- its backdrop
    // never finishes fading out/unmounting, and (still `pointer-events:
    // auto`) silently eats every subsequent click on the page. Letting
    // A's close commit as its own render first, then opening B on the
    // next tick, avoids the two dialogs' transitions ever overlapping.
    setFirstTriggerConfirmOpen(false);
    setTimeout(() => {
      setEditingTrigger(null);
      setAddEditOpen(true);
    }, 0);
  }

  function openEditFlow(trigger: MailLeadStartTrigger) {
    setEditingTrigger(trigger);
    setAddEditOpen(true);
  }

  async function handleSaved() {
    // Re-fetches both the trigger list AND the campaign itself -- the
    // very first successful create flips campaign.lead_start_mode on the
    // backend, and this page must reflect that immediately (e.g. to stop
    // showing the first-trigger confirmation again, and to start showing
    // the zero-enabled warning/legacy-limit note where relevant).
    setRowError(null);
    await onTriggersRefresh();
  }

  async function handleToggleEnabled(trigger: MailLeadStartTrigger, nextEnabled: boolean) {
    setRowError(null);
    setPending(trigger.trigger_id, true);
    try {
      await updateMailLeadStartTrigger(campaign.mail_campaign_id, trigger.trigger_id, { enabled: nextEnabled });
      await onTriggersRefresh();
    } catch (err) {
      setRowError(
        err instanceof ApiError ? `Couldn't update trigger: ${formatApiErrorMessage(err.message)}` : "Couldn't reach the backend."
      );
      // Authoritative state is whatever onTriggersRefresh's own last
      // successful fetch left in `triggers` -- nothing is optimistically
      // changed here, so a failed toggle simply leaves the Switch showing
      // the real, unchanged backend value once `pending` clears below.
    } finally {
      setPending(trigger.trigger_id, false);
    }
  }

  async function handleDelete(trigger: MailLeadStartTrigger) {
    const confirmed = window.confirm("Delete this lead-start trigger?");
    if (!confirmed) return;
    setRowError(null);
    setPending(trigger.trigger_id, true);
    try {
      await deleteMailLeadStartTrigger(campaign.mail_campaign_id, trigger.trigger_id);
      await onTriggersRefresh();
    } catch (err) {
      setRowError(
        err instanceof ApiError ? `Couldn't delete trigger: ${formatApiErrorMessage(err.message)}` : "Couldn't reach the backend."
      );
      setPending(trigger.trigger_id, false);
    }
  }

  const zeroEnabledWarning = hasZeroEnabledTriggers(campaign.lead_start_mode, triggers);
  const waitingToStartCopy = formatWaitingToStartCopy(workloadPending);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-sm">Lead start triggers</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="text-xs text-muted-foreground">
          Choose when new prospects begin their sequence. Email delivery still follows your sending hours, mailbox
          limits, and pacing.
        </p>

        <p className="text-xs text-muted-foreground">{formatTriggerTimezoneCopy(campaign.timezone)}</p>

        {waitingToStartCopy && <p className="text-xs text-muted-foreground">{waitingToStartCopy}</p>}

        {!editable && (
          <Alert>
            <Lock className="h-4 w-4" />
            <AlertDescription>
              {campaign.status === "archived"
                ? "This campaign is archived -- its lead-start triggers are read-only and can no longer be changed."
                : "This campaign is completed -- its lead-start triggers are read-only and can no longer be changed."}
            </AlertDescription>
          </Alert>
        )}

        {zeroEnabledWarning && (
          <Alert>
            <AlertTriangle className="h-4 w-4" />
            <AlertDescription>
              No active lead-start triggers. New prospects will remain waiting to start until a trigger is enabled.
            </AlertDescription>
          </Alert>
        )}

        {(triggersError || rowError) && (
          <Alert variant="destructive">
            <AlertTriangle className="h-4 w-4" />
            <AlertDescription>{triggersError ?? rowError}</AlertDescription>
          </Alert>
        )}

        {triggers.length === 0 && (
          <div className="flex flex-col items-center gap-2 rounded-xl border border-dashed border-border/60 py-10 text-center">
            <p className="text-sm font-medium">No lead-start triggers yet</p>
            <p className="max-w-sm text-xs text-muted-foreground">
              Without a trigger, new prospects wait for you to start them manually once sending is enabled.
            </p>
          </div>
        )}

        {triggers.length > 0 && (
          <div className="overflow-x-auto rounded-xl border border-border/60">
            <table className="w-full text-sm">
              <thead className="bg-secondary/40 text-xs text-muted-foreground">
                <tr>
                  <th className="px-3 py-2 text-left font-medium">Days</th>
                  <th className="px-3 py-2 text-left font-medium">Time</th>
                  <th className="px-3 py-2 text-left font-medium">Leads</th>
                  <th className="px-3 py-2 text-left font-medium">Enabled</th>
                  <th className="px-3 py-2 text-right font-medium">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border/60">
                {triggers.map((trigger) => {
                  const rowPending = pendingTriggerIds.has(trigger.trigger_id);
                  return (
                    <tr key={trigger.trigger_id} className="hover:bg-secondary/30">
                      <td className="px-3 py-2.5">{formatSendingDays(trigger.weekdays)}</td>
                      <td className="px-3 py-2.5">{formatTimeOfDay(trigger.local_time)}</td>
                      <td className="px-3 py-2.5 text-muted-foreground">{formatLeadsToStart(trigger.leads_to_start)}</td>
                      <td className="px-3 py-2.5">
                        <Switch
                          checked={trigger.enabled}
                          onCheckedChange={(v) => handleToggleEnabled(trigger, Boolean(v))}
                          disabled={!editable || rowPending}
                        />
                      </td>
                      <td className="px-3 py-2.5 text-right">
                        <div className="flex justify-end gap-1.5">
                          <Button
                            type="button"
                            variant="ghost"
                            size="sm"
                            onClick={() => openEditFlow(trigger)}
                            disabled={!editable || rowPending}
                          >
                            Edit
                          </Button>
                          <Button
                            type="button"
                            variant="ghost"
                            size="sm"
                            onClick={() => handleDelete(trigger)}
                            disabled={!editable || rowPending}
                            className="text-destructive hover:text-destructive"
                          >
                            Delete
                          </Button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        {editable && (
          <Button type="button" variant="outline" size="sm" onClick={openAddFlow} className="gap-1.5">
            <Plus className="h-3.5 w-3.5" />
            Add trigger
          </Button>
        )}
      </CardContent>

      <Dialog open={firstTriggerConfirmOpen} onOpenChange={setFirstTriggerConfirmOpen}>
        <DialogPopup className="max-w-md">
          <DialogHeader>
            <DialogTitle>Use scheduled lead starts?</DialogTitle>
            <DialogDescription>
              Adding your first trigger changes this campaign to scheduled lead starts. New prospects will wait for a
              trigger before beginning their sequence.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <DialogClose render={<Button type="button" variant="outline">Cancel</Button>} />
            <Button type="button" onClick={handleFirstTriggerContinue}>
              Continue
            </Button>
          </DialogFooter>
        </DialogPopup>
      </Dialog>

      <AddEditTriggerModal
        open={addEditOpen}
        onOpenChange={setAddEditOpen}
        mailCampaignId={campaign.mail_campaign_id}
        existingTrigger={editingTrigger}
        onSaved={handleSaved}
      />
    </Card>
  );
}
