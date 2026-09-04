import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { SharingSelector } from "@/components/mail-sharing-selector";
import type { CrmContactListSummary, MailCampaignSharing } from "@/lib/api";
import { showsLegacyDailyLimitNote } from "@/lib/mail-trigger";

// Combines the old single-page layout's "Audience" and "Campaign Settings"
// cards -- same handlers, same editable gating, no behavior changes.
export function MailCampaignSettingsTab({
  editable,
  busy,
  name,
  setName,
  sourceListId,
  setSourceListId,
  lists,
  onSaveDetails,
  sharing,
  setSharing,
  startImmediately,
  setStartImmediately,
  dailyLeadStartLimit,
  setDailyLeadStartLimit,
  leadStartMode,
  savingSettings,
  settingsError,
  onSaveSettings,
}: {
  editable: boolean;
  busy: boolean;
  name: string;
  setName: (value: string) => void;
  sourceListId: string;
  setSourceListId: (value: string) => void;
  lists: CrmContactListSummary[];
  onSaveDetails: () => void;
  sharing: MailCampaignSharing;
  setSharing: (value: MailCampaignSharing) => void;
  startImmediately: boolean;
  setStartImmediately: (value: boolean) => void;
  dailyLeadStartLimit: string;
  setDailyLeadStartLimit: (value: string) => void;
  leadStartMode: "immediate" | "triggered";
  savingSettings: boolean;
  settingsError: string | null;
  onSaveSettings: () => void;
}) {
  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <CardTitle className="text-sm">Audience</CardTitle>
        </CardHeader>
        <CardContent className="max-w-md space-y-3">
          <div className="space-y-1">
            <label className="text-xs font-medium text-muted-foreground">Campaign name</label>
            <Input value={name} onChange={(e) => setName(e.target.value)} disabled={!editable} />
          </div>
          <div className="space-y-1">
            <label className="text-xs font-medium text-muted-foreground">CRM List</label>
            <select
              value={sourceListId}
              onChange={(e) => setSourceListId(e.target.value)}
              disabled={!editable}
              className="h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm disabled:cursor-not-allowed disabled:opacity-60"
            >
              <option value="">-- choose a CRM List --</option>
              {lists.map((l) => (
                <option key={l.list_id} value={l.list_id}>
                  {l.name} ({l.contact_count} contact{l.contact_count === 1 ? "" : "s"})
                </option>
              ))}
            </select>
            <p className="text-xs text-muted-foreground">
              Referenced by list -- contacts are never copied. Changes to the list after this campaign is marked
              Ready will not retroactively change its audience.
            </p>
          </div>
          {editable && (
            <Button size="sm" onClick={onSaveDetails} disabled={busy || !name.trim()}>
              Save
            </Button>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-sm">Campaign Settings</CardTitle>
        </CardHeader>
        <CardContent className="max-w-md space-y-4">
          {settingsError && (
            <Alert variant="destructive">
              <AlertDescription>{settingsError}</AlertDescription>
            </Alert>
          )}
          <div className="space-y-1.5">
            <label className="text-xs font-medium text-muted-foreground">Sharing</label>
            <SharingSelector value={sharing} onChange={setSharing} disabled={!editable} />
            <p className="text-xs text-muted-foreground/70">Saved for later -- not yet enforced.</p>
          </div>
          <div className="flex items-start justify-between gap-4 rounded-md border border-border/60 p-3">
            <div>
              <p className="text-sm font-medium">Start campaign immediately</p>
              <p className="text-xs text-muted-foreground">
                When enabled, newly added leads can begin progressing through the campaign once sending is enabled.
              </p>
            </div>
            <Switch
              checked={startImmediately}
              onCheckedChange={(v) => setStartImmediately(Boolean(v))}
              disabled={!editable}
              className="mt-0.5 shrink-0"
            />
          </div>
          <div className="space-y-1.5">
            <label className="text-xs font-medium text-muted-foreground">Number of leads to start daily</label>
            <Input
              type="number"
              min={1}
              step={1}
              value={dailyLeadStartLimit}
              onChange={(e) => setDailyLeadStartLimit(e.target.value)}
              disabled={!editable}
              placeholder="50 (leave blank for unlimited)"
            />
            <p className="text-xs text-muted-foreground/70">
              How many new leads may begin this sequence per day -- separate from a mailbox&apos;s own daily sending limit.
            </p>
            {showsLegacyDailyLimitNote(leadStartMode) && (
              <p className="text-xs text-amber-600 dark:text-amber-500">
                Not used while this campaign uses lead-start triggers.
              </p>
            )}
          </div>
          {editable && (
            <Button size="sm" onClick={onSaveSettings} disabled={savingSettings}>
              {savingSettings ? "Saving..." : "Save settings"}
            </Button>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
