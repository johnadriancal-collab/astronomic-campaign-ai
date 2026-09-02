import { Archive, Unlock } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type { MailCampaign } from "@/lib/api";
import { mailCampaignStatusBadgeClass, mailCampaignStatusLabel } from "@/lib/mail";

// The campaign name + status + primary state-transition actions, all in one
// header block -- previously the status badge was a hand-rolled <span> and
// the actions lived in their own "Status" card at the bottom of the page.
// Reuses the same tested mailCampaignStatusBadgeClass()/Label() mapping via
// the real shared Badge component instead of a second, parallel style.
export function MailCampaignHeader({
  campaign,
  busy,
  onMarkReady,
  onUnlock,
  onArchive,
}: {
  campaign: MailCampaign;
  busy: boolean;
  onMarkReady: () => void;
  onUnlock: () => void;
  onArchive: () => void;
}) {
  return (
    <div className="mb-6 flex items-start justify-between gap-4">
      <div>
        <h1 className="font-serif text-2xl font-medium tracking-tight">{campaign.name}</h1>
        <p className="mt-1 text-xs text-muted-foreground">Astronomic Mail</p>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <Badge variant="secondary" className={mailCampaignStatusBadgeClass(campaign.status)}>
          {mailCampaignStatusLabel(campaign.status)}
        </Badge>
        {campaign.status === "draft" && (
          <Button size="sm" onClick={onMarkReady} disabled={busy}>
            Mark Ready
          </Button>
        )}
        {campaign.status === "ready" && (
          <Button variant="outline" size="sm" onClick={onUnlock} disabled={busy} className="gap-1.5">
            <Unlock className="h-3.5 w-3.5" />
            Unlock to edit
          </Button>
        )}
        {campaign.status !== "archived" && (
          <Button variant="outline" size="sm" onClick={onArchive} disabled={busy} className="gap-1.5">
            <Archive className="h-3.5 w-3.5" />
            Archive
          </Button>
        )}
      </div>
    </div>
  );
}
