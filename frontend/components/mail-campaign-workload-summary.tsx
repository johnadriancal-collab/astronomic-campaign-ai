import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { WORKLOAD_FIELD_LABELS } from "@/lib/add-prospects-flow";
import type { MailCampaignWorkload } from "@/lib/api";

// Compact operational status for the Leads tab (Stage 4B, 2026-09-03) --
// GET /mail/campaigns/{id}/workload, real backend fields only. Workload is
// enrollment-status counts, deliberately independent of the campaign's own
// lifecycle `status` -- this component never infers or displays a
// lifecycle claim from these numbers (a campaign is never shown as
// "completed" here merely because workload has drained to zero; that's
// MailCampaign.status's job, shown elsewhere on this page). Not the
// future Journeys/analytics dashboard -- no rates, no Open/Reply/Bounce,
// nothing beyond exactly what this endpoint returns.
export function MailCampaignWorkloadSummary({ workload }: { workload: MailCampaignWorkload }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-sm">Workload -- {workload.total} total</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-wrap gap-x-6 gap-y-1 text-sm">
        {WORKLOAD_FIELD_LABELS.map(({ key, label }) => (
          <span key={key} className="text-muted-foreground">
            {label}: <span className="font-medium text-foreground">{workload[key]}</span>
          </span>
        ))}
      </CardContent>
    </Card>
  );
}
