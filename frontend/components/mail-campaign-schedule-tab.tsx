import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import { SendDaysPicker } from "@/components/send-days-picker";
import { timezoneOptionsIncluding } from "@/lib/timezones";

// Moved as-is from the old single-page layout's "Schedule" card -- same
// handlers, same editable gating, no behavior changes.
export function MailCampaignScheduleTab({
  editable,
  sendingDays,
  setSendingDays,
  allHours,
  setAllHours,
  startTime,
  setStartTime,
  endTime,
  setEndTime,
  timezone,
  setTimezone,
  savingSchedule,
  onSave,
}: {
  editable: boolean;
  sendingDays: number[];
  setSendingDays: (days: number[]) => void;
  allHours: boolean;
  setAllHours: (value: boolean) => void;
  startTime: string;
  setStartTime: (value: string) => void;
  endTime: string;
  setEndTime: (value: string) => void;
  timezone: string;
  setTimezone: (value: string) => void;
  savingSchedule: boolean;
  onSave: () => void;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-sm">Schedule</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <div>
          <p className="mb-1.5 text-xs font-medium text-muted-foreground">Sending days</p>
          <SendDaysPicker days={sendingDays} onChange={setSendingDays} disabled={!editable} />
        </div>
        <div className="flex items-center justify-between rounded-md border border-border/60 px-3 py-2">
          <span className="text-sm">All hours</span>
          <Switch checked={allHours} onCheckedChange={(v) => setAllHours(Boolean(v))} disabled={!editable} />
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div className="space-y-1">
            <label className="text-xs font-medium text-muted-foreground">Start time</label>
            <input
              type="time"
              value={startTime}
              onChange={(e) => setStartTime(e.target.value)}
              disabled={!editable || allHours}
              className="h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm disabled:cursor-not-allowed disabled:opacity-60"
            />
          </div>
          <div className="space-y-1">
            <label className="text-xs font-medium text-muted-foreground">End time</label>
            <input
              type="time"
              value={endTime}
              onChange={(e) => setEndTime(e.target.value)}
              disabled={!editable || allHours}
              className="h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm disabled:cursor-not-allowed disabled:opacity-60"
            />
          </div>
        </div>
        <div className="space-y-1">
          <label className="text-xs font-medium text-muted-foreground">Timezone</label>
          <select
            value={timezone}
            onChange={(e) => setTimezone(e.target.value)}
            disabled={!editable}
            className="h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm disabled:cursor-not-allowed disabled:opacity-60"
          >
            <option value="">-- choose a timezone --</option>
            {timezoneOptionsIncluding(timezone || null).map((tz) => (
              <option key={tz.value} value={tz.value}>
                {tz.label}
              </option>
            ))}
          </select>
        </div>
        {editable && (
          <Button size="sm" onClick={onSave} disabled={savingSchedule}>
            {savingSchedule ? "Saving..." : "Save schedule"}
          </Button>
        )}
      </CardContent>
    </Card>
  );
}
