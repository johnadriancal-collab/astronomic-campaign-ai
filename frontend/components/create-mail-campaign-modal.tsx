"use client";

import { useState } from "react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogPopup,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { SendDaysPicker } from "@/components/send-days-picker";
import { SharingSelector } from "@/components/mail-sharing-selector";
import { ApiError, createMailCampaign, type MailCampaign, type MailCampaignSharing } from "@/lib/api";
import { DEFAULT_SENDING_DAYS } from "@/lib/mail";
import { DEFAULT_TIMEZONE, TIMEZONE_OPTIONS } from "@/lib/timezones";

const DEFAULT_START_TIME = "08:00";
const DEFAULT_END_TIME = "18:00";

interface FormState {
  name: string;
  sharing: MailCampaignSharing;
  sendingDays: number[];
  timezone: string;
  startTime: string;
  endTime: string;
  allHours: boolean;
  startImmediately: boolean;
  dailyLeadStartLimit: string; // kept as a string for the input; "" = unlimited
}

function initialState(): FormState {
  return {
    name: "",
    sharing: "everyone",
    sendingDays: DEFAULT_SENDING_DAYS,
    timezone: DEFAULT_TIMEZONE,
    startTime: DEFAULT_START_TIME,
    endTime: DEFAULT_END_TIME,
    allHours: false,
    startImmediately: false,
    dailyLeadStartLimit: "",
  };
}

function isValid(form: FormState): boolean {
  if (!form.name.trim()) return false;
  if (form.sendingDays.length === 0) return false;
  if (!form.timezone) return false;
  if (!form.allHours) {
    if (!form.startTime || !form.endTime) return false;
    if (form.startTime >= form.endTime) return false;
  }
  if (form.dailyLeadStartLimit.trim() !== "") {
    const parsed = Number(form.dailyLeadStartLimit);
    if (!Number.isInteger(parsed) || parsed < 1) return false;
  }
  return true;
}

export function CreateMailCampaignModal({
  open,
  onOpenChange,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreated: (campaign: MailCampaign) => void;
}) {
  const [form, setForm] = useState<FormState>(initialState);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function update(patch: Partial<FormState>) {
    setForm((prev) => ({ ...prev, ...patch }));
  }

  function handleOpenChange(next: boolean) {
    if (!next && !creating) {
      setForm(initialState());
      setError(null);
    }
    onOpenChange(next);
  }

  async function handleCreate() {
    if (!isValid(form) || creating) return;
    setCreating(true);
    setError(null);
    try {
      const campaign = await createMailCampaign(form.name.trim(), {
        sharing: form.sharing,
        sending_days: form.sendingDays,
        start_time: form.allHours ? undefined : form.startTime,
        end_time: form.allHours ? undefined : form.endTime,
        timezone: form.timezone,
        all_hours: form.allHours,
        start_immediately: form.startImmediately,
        daily_lead_start_limit: form.dailyLeadStartLimit.trim() === "" ? null : Number(form.dailyLeadStartLimit),
      });
      setForm(initialState());
      onCreated(campaign);
    } catch (err) {
      setError(err instanceof ApiError ? `Couldn't create campaign (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    } finally {
      setCreating(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogPopup className="max-w-xl">
        <DialogHeader>
          <DialogTitle>Create Campaign</DialogTitle>
          <DialogDescription>
            Configure the campaign-level sending rules. You can add leads and build the email sequence after creation.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-5">
          {error && (
            <Alert variant="destructive">
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}

          <div className="space-y-1.5">
            <label className="text-xs font-medium text-muted-foreground">Campaign Name</label>
            <Input
              value={form.name}
              onChange={(e) => update({ name: e.target.value })}
              placeholder="Austin Founder Outreach — August 2026"
              autoFocus
              required
            />
          </div>

          <div className="space-y-1.5">
            <label className="text-xs font-medium text-muted-foreground">Sharing</label>
            <SharingSelector value={form.sharing} onChange={(sharing) => update({ sharing })} />
            <p className="text-xs text-muted-foreground/70">Saved for later -- not yet enforced.</p>
          </div>

          <div className="space-y-1.5">
            <label className="text-xs font-medium text-muted-foreground">Send Days</label>
            <SendDaysPicker days={form.sendingDays} onChange={(sendingDays) => update({ sendingDays })} />
            {form.sendingDays.length === 0 && <p className="text-xs text-destructive">At least one send day is required.</p>}
          </div>

          <div className="space-y-1.5">
            <label className="text-xs font-medium text-muted-foreground">Timezone</label>
            <select
              value={form.timezone}
              onChange={(e) => update({ timezone: e.target.value })}
              required
              className="h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm"
            >
              {TIMEZONE_OPTIONS.map((tz) => (
                <option key={tz.value} value={tz.value}>
                  {tz.label}
                </option>
              ))}
            </select>
          </div>

          <div className="space-y-1.5">
            <div className="flex items-center justify-between">
              <label className="text-xs font-medium text-muted-foreground">Sending Window</label>
              <div className="flex items-center gap-2">
                <span className="text-xs text-muted-foreground">All hours</span>
                <Switch checked={form.allHours} onCheckedChange={(allHours) => update({ allHours: Boolean(allHours) })} />
              </div>
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1">
                <label className="text-xs text-muted-foreground">From</label>
                <input
                  type="time"
                  value={form.startTime}
                  onChange={(e) => update({ startTime: e.target.value })}
                  disabled={form.allHours}
                  className="h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm disabled:cursor-not-allowed disabled:opacity-60"
                />
              </div>
              <div className="space-y-1">
                <label className="text-xs text-muted-foreground">To</label>
                <input
                  type="time"
                  value={form.endTime}
                  onChange={(e) => update({ endTime: e.target.value })}
                  disabled={form.allHours}
                  className="h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm disabled:cursor-not-allowed disabled:opacity-60"
                />
              </div>
            </div>
            {!form.allHours && form.startTime >= form.endTime && (
              <p className="text-xs text-destructive">Start time must be before end time.</p>
            )}
          </div>

          <div className="flex items-start justify-between gap-4 rounded-md border border-border/60 p-3">
            <div>
              <p className="text-sm font-medium">Start campaign immediately</p>
              <p className="text-xs text-muted-foreground">
                When enabled, newly added leads can begin progressing through the campaign once sending is enabled.
              </p>
            </div>
            <Switch
              checked={form.startImmediately}
              onCheckedChange={(startImmediately) => update({ startImmediately: Boolean(startImmediately) })}
              className="mt-0.5 shrink-0"
            />
          </div>

          <div className="space-y-1.5">
            <label className="text-xs font-medium text-muted-foreground">Number of leads to start daily</label>
            <Input
              type="number"
              min={1}
              step={1}
              value={form.dailyLeadStartLimit}
              onChange={(e) => update({ dailyLeadStartLimit: e.target.value })}
              placeholder="50 (leave blank for unlimited)"
            />
            <p className="text-xs text-muted-foreground/70">
              How many new leads may begin this sequence per day -- separate from a mailbox's own daily sending limit.
            </p>
          </div>
        </div>

        <DialogFooter>
          <DialogClose
            disabled={creating}
            render={
              <Button type="button" variant="outline">
                Cancel
              </Button>
            }
          />
          <Button type="button" onClick={handleCreate} disabled={!isValid(form) || creating}>
            {creating ? "Creating..." : "Create Campaign"}
          </Button>
        </DialogFooter>
      </DialogPopup>
    </Dialog>
  );
}
