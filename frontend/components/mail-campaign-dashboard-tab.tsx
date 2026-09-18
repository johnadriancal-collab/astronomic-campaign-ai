import { AlertTriangle, CheckCircle2 } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { MailCampaignStatsStrip } from "@/components/mail-campaign-stats-strip";
import type { MailCampaign, MailCampaignReview, MailCampaignStats, MailEnrollment } from "@/lib/api";

// The command-center tab. The stats strip (2026-09-17, see
// MailCampaignStatsStrip) shows two real metrics computed from actual
// MailEnrollment/MailSuppression data; the other two positions in that
// strip render static "not tracked" copy, since neither has any backing
// data anywhere for Astronomic Mail (confirmed by investigation, not
// assumed -- see MailCampaignStatsService's own module docstring). Never
// a fabricated percentage for either.
//
// Two genuinely different kinds of "suppressed"/audience numbers are both
// surfaced here, deliberately kept in two separate stat groups rather than
// merged into one, matching MailCampaignReview's own docstring:
//   - Audience & Sequence (from the Review, always live): total/missing-
//     email/eligible/steps/theoretical-sends recompute fresh on every load,
//     regardless of campaign status -- meaningful even on a still-editable
//     Draft as a live preview of what marking Ready would snapshot.
//   - Enrollment Progress (from actual MailEnrollment rows): only exist
//     once mark_ready() has run. A Draft campaign has zero enrollment rows
//     by construction (not zero progress -- no snapshot has been taken at
//     all yet), so this section explains that rather than showing a
//     misleading "0 pending / 0 suppressed".
export function MailCampaignDashboardTab({
  campaign,
  review,
  enrollments,
  stats,
}: {
  campaign: MailCampaign;
  review: MailCampaignReview | null;
  enrollments: MailEnrollment[];
  stats: MailCampaignStats | null;
}) {
  const pendingCount = enrollments.filter((e) => e.status === "pending").length;
  const suppressedCount = enrollments.filter((e) => e.status === "suppressed").length;
  const hasEnrollments = campaign.status !== "draft";

  return (
    <div className="space-y-6">
      <MailCampaignStatsStrip stats={stats} />

      {review && review.readiness_warnings.length > 0 && (
        <Alert variant="destructive">
          <AlertTriangle />
          <AlertTitle>Campaign warnings</AlertTitle>
          <AlertDescription>
            <ul className="list-disc space-y-0.5 pl-4">
              {review.readiness_warnings.map((warning) => (
                <li key={warning}>{warning}</li>
              ))}
            </ul>
          </AlertDescription>
        </Alert>
      )}
      {review && review.readiness_warnings.length === 0 && campaign.status === "draft" && (
        <Alert>
          <CheckCircle2 className="h-4 w-4" />
          <AlertTitle>Looks ready</AlertTitle>
          <AlertDescription>
            Audience, sequence, and schedule are all configured -- you can mark this campaign Ready when you&apos;re ready.
          </AlertDescription>
        </Alert>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="text-sm">Audience &amp; Sequence</CardTitle>
        </CardHeader>
        <CardContent>
          <dl className="grid grid-cols-2 gap-4 text-sm sm:grid-cols-3 lg:grid-cols-5">
            <Stat label="Total contacts" value={review?.total_contacts ?? 0} />
            <Stat label="Missing email" value={review?.contacts_missing_email ?? 0} />
            <Stat label="Eligible recipients" value={review?.contacts_eligible ?? 0} emphasize />
            <Stat label="Sequence steps" value={review?.sequence_step_count ?? 0} />
            <Stat label="Theoretical total sends" value={review?.theoretical_total_sends ?? 0} emphasize />
          </dl>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-sm">Campaign Progress</CardTitle>
        </CardHeader>
        <CardContent>
          {hasEnrollments ? (
            <dl className="grid max-w-xs grid-cols-2 gap-4 text-sm">
              <Stat label="Pending" value={pendingCount} />
              <Stat label="Suppressed" value={suppressedCount} />
            </dl>
          ) : (
            <p className="text-sm text-muted-foreground">
              Not enrolled yet -- enrollments (and Pending/Suppressed counts) are created when this campaign is marked
              Ready, as a one-time snapshot of its audience at that moment.
            </p>
          )}
        </CardContent>
      </Card>

      <Alert>
        <AlertTitle>Marking Ready doesn&apos;t send anything</AlertTitle>
        <AlertDescription>
          Marking a campaign Ready only snapshots its audience for review -- it never sends or schedules anything by
          itself. Sending only begins once a campaign is explicitly activated.
        </AlertDescription>
      </Alert>
    </div>
  );
}

function Stat({ label, value, emphasize }: { label: string; value: number; emphasize?: boolean }) {
  return (
    <div>
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className={emphasize ? "text-base font-medium" : ""}>{value}</dd>
    </div>
  );
}
