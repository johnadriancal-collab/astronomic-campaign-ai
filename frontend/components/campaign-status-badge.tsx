import { Badge } from "@/components/ui/badge";
import type { CampaignStatus } from "@/lib/api";

const STATUS_META: Record<CampaignStatus, { label: string; variant: "outline" | "secondary" | "default" | "destructive" }> = {
  draft: { label: "Draft", variant: "outline" },
  searched: { label: "Searched", variant: "secondary" },
  building: { label: "Building", variant: "outline" },
  built: { label: "Built", variant: "default" },
  failed: { label: "Failed", variant: "destructive" },
  ready: { label: "Ready", variant: "secondary" },
  active: { label: "Active", variant: "default" },
  paused: { label: "Paused", variant: "outline" },
};

export function CampaignStatusBadge({ status }: { status: CampaignStatus }) {
  const meta = STATUS_META[status];
  return (
    <Badge variant={meta.variant} className="rounded-full font-normal">
      {meta.label}
    </Badge>
  );
}
