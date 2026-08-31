import Link from "next/link";
import { AlertTriangle, Inbox, Lock } from "lucide-react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import type { Mailbox } from "@/lib/api";
import { mailboxDisplayName, mailboxStatusBadgeClass, mailboxStatusLabel, providerLabel } from "@/lib/mailboxes";
import { cn } from "@/lib/utils";

// Which already-connected inboxes (see /manager/emails -- the ONLY place a
// mailbox is connected/disconnected) may send THIS campaign. Deliberately
// reuses mailboxDisplayName/providerLabel/mailboxStatusLabel/
// mailboxStatusBadgeClass from lib/mailboxes.ts for full visual consistency
// with the Emails page -- and deliberately leaves out that page's other,
// not-yet-real sending-stat columns (see lib/mailboxes.ts's own docstring):
// those are honest "nothing to fabricate" placeholders for a feature that
// doesn't exist yet, but a fake zero-value count here would be actively
// misleading, since this tab IS the exact relationship those columns
// gesture at.
//
// A mailbox that isn't currently connected can never be NEWLY checked (its
// row is disabled with an explanatory note), but if it was already selected
// before it became disconnected/needs_reauth, it stays checked, visible, and
// removable -- selections are never silently dropped just because a
// mailbox's status changed later.
//
// `readOnly` (true only once the campaign is archived -- a terminal, no-
// un-archive status) freezes this tab: every switch is disabled and the
// Save action disappears entirely, matching the backend's own rejection of
// PUT .../channels for an archived campaign (see set_channel_mailboxes()'s
// MailCampaignChannelsFrozenError) -- this is a UI convenience, not the
// enforcement boundary. Draft AND Ready both stay fully editable here --
// unlike the audience/sequence/schedule lock, replacing a sender on a Ready
// campaign is an intentionally allowed way to recover from a disconnected
// inbox without unlocking the whole campaign.
export function MailCampaignChannelsTab({
  mailboxes,
  selectedMailboxIds,
  onToggle,
  busy,
  saving,
  error,
  onSave,
  readOnly,
}: {
  mailboxes: Mailbox[] | null;
  selectedMailboxIds: string[];
  onToggle: (mailboxId: string, selected: boolean) => void;
  busy: boolean;
  saving: boolean;
  error: string | null;
  onSave: () => void;
  readOnly: boolean;
}) {
  if (mailboxes === null) {
    return (
      <Card>
        <CardContent className="py-10 text-center text-sm text-muted-foreground">Loading connected inboxes…</CardContent>
      </Card>
    );
  }

  if (mailboxes.length === 0) {
    return (
      <Card>
        <CardContent className="flex flex-col items-center gap-4 py-16 text-center">
          <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-secondary/60 text-muted-foreground">
            <Inbox className="h-5 w-5" />
          </div>
          <div>
            <p className="font-medium">No sending inboxes connected</p>
            <p className="mt-1 max-w-sm text-sm text-muted-foreground">
              Connect a Google Workspace inbox on the Emails page, then come back here to choose which ones may send
              this campaign.
            </p>
          </div>
          <Button size="sm" className="mt-1" render={<Link href="/manager/emails">Go to Emails</Link>} />
        </CardContent>
      </Card>
    );
  }

  const selected = new Set(selectedMailboxIds);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-sm">Channels</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="text-xs text-muted-foreground">
          Choose which connected inboxes may send this campaign. Manage connections themselves on the{" "}
          <Link href="/manager/emails" className="underline underline-offset-2">
            Emails
          </Link>{" "}
          page.
        </p>

        {readOnly && (
          <Alert>
            <Lock className="h-4 w-4" />
            <AlertDescription>
              This campaign is archived -- its Channels selection is read-only and can no longer be changed.
            </AlertDescription>
          </Alert>
        )}

        {error && (
          <Alert variant="destructive">
            <AlertTriangle />
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}

        <div className="overflow-x-auto rounded-xl border border-border/60">
          <table className="w-full text-sm">
            <thead className="bg-secondary/40 text-xs text-muted-foreground">
              <tr>
                <th className="w-14 px-3 py-2 text-left font-medium">Select</th>
                <th className="px-3 py-2 text-left font-medium">Name</th>
                <th className="px-3 py-2 text-left font-medium">Email</th>
                <th className="px-3 py-2 text-left font-medium">Provider</th>
                <th className="px-3 py-2 text-right font-medium">Status</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border/60">
              {mailboxes.map((mailbox) => {
                const isSelected = selected.has(mailbox.mailbox_id);
                const canBeNewlySelected = mailbox.status === "connected";
                const toggleDisabled = readOnly || busy || saving || (!isSelected && !canBeNewlySelected);
                return (
                  <tr key={mailbox.mailbox_id} className="hover:bg-secondary/30">
                    <td className="px-3 py-2.5">
                      <Switch
                        checked={isSelected}
                        onCheckedChange={(v) => onToggle(mailbox.mailbox_id, Boolean(v))}
                        disabled={toggleDisabled}
                        title={
                          !isSelected && !canBeNewlySelected
                            ? "Only a currently connected inbox can be newly selected."
                            : undefined
                        }
                      />
                    </td>
                    <td className="px-3 py-2.5 font-medium">{mailboxDisplayName(mailbox)}</td>
                    <td className="px-3 py-2.5 text-muted-foreground">{mailbox.email}</td>
                    <td className="px-3 py-2.5">
                      <Badge variant="outline" className="rounded-full font-normal text-muted-foreground">
                        {providerLabel(mailbox.provider)}
                      </Badge>
                    </td>
                    <td className="px-3 py-2.5 text-right">
                      <span
                        className={cn(
                          "rounded-full px-2 py-0.5 text-xs font-medium",
                          mailboxStatusBadgeClass(mailbox.status)
                        )}
                      >
                        {mailboxStatusLabel(mailbox.status)}
                      </span>
                      {isSelected && mailbox.status !== "connected" && (
                        <p className="mt-1 text-xs text-amber-600 dark:text-amber-500">
                          Selected, but can&apos;t currently send -- reconnect it or remove it below.
                        </p>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>

        {!readOnly && (
          <Button size="sm" onClick={onSave} disabled={saving || busy}>
            {saving ? "Saving..." : "Save Channels"}
          </Button>
        )}
      </CardContent>
    </Card>
  );
}
