import { AlertTriangle, Lock } from "lucide-react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ScheduleDayRow, type EditableWindow } from "@/components/schedule-day-row";
import { timezoneOptionsIncluding } from "@/lib/timezones";

export type TabWindow = EditableWindow & { day: number };

const ALL_DAYS = [0, 1, 2, 3, 4, 5, 6];

// The real, campaign-execution-affecting Schedule -- DRAFT-only, same
// boundary as audience/sequence (unlike Channels, which stays editable on
// READY -- see mail_campaign_service.py's set_schedule() docstring for why
// that split is intentional). `editable` is exactly `campaign.status ===
// "draft"`, the SAME flag every other DRAFT-only tab on this page already
// uses -- READY/ARCHIVED both render this read-only, with every window
// still fully visible (never hidden just because it can't be edited).
//
// Real send windows only -- this deliberately has no automation-rule/
// start-condition control of any kind (Astronomic has no such engine at
// all yet, see this feature's investigation report) and no per-window
// sending-volume badge (no per-mailbox limit or sending pipeline exists --
// see MailCampaignReview's daily-estimate note, reworded alongside this
// feature to stop implying otherwise now that Channels exists).
export function MailCampaignScheduleTab({
  timezone,
  setTimezone,
  windows,
  setWindows,
  editable,
  saving,
  error,
  onSave,
}: {
  timezone: string;
  setTimezone: (value: string) => void;
  windows: TabWindow[];
  setWindows: (next: TabWindow[]) => void;
  editable: boolean;
  saving: boolean;
  error: string | null;
  onSave: () => void;
}) {
  function windowsForDay(day: number): EditableWindow[] {
    return windows.filter((w) => w.day === day).map(({ id, start, end }) => ({ id, start, end }));
  }

  function setWindowsForDay(day: number, next: EditableWindow[]) {
    const others = windows.filter((w) => w.day !== day);
    setWindows([...others, ...next.map((w) => ({ ...w, day }))]);
  }

  const firstWindowAnywhere = windows.length > 0 ? { start: windows[0].start, end: windows[0].end } : null;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-sm">Schedule</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {!editable && (
          <Alert>
            <Lock className="h-4 w-4" />
            <AlertDescription>
              This schedule is locked. Unlock the campaign back to Draft (see the button at the top of the page) to
              make changes.
            </AlertDescription>
          </Alert>
        )}

        {error && (
          <Alert variant="destructive">
            <AlertTriangle />
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}

        <div className="max-w-xs space-y-1">
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

        <div>
          {ALL_DAYS.map((day) => (
            <ScheduleDayRow
              key={day}
              day={day}
              windows={windowsForDay(day)}
              onWindowsChange={(next) => setWindowsForDay(day, next)}
              anyWindowInCampaign={firstWindowAnywhere}
              readOnly={!editable}
            />
          ))}
        </div>

        {editable && (
          <Button size="sm" onClick={onSave} disabled={saving}>
            {saving ? "Saving..." : "Save Schedule"}
          </Button>
        )}
      </CardContent>
    </Card>
  );
}
