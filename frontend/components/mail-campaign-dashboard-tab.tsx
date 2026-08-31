import { AlertTriangle, CheckCircle2 } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { MailCampaign, MailCampaignReview, MailEnrollment } from "@/lib/api";

// The command-center tab -- real planning/progress data only. No email
// engagement percentages appear anywhere here: Astronomic Mail cannot send
// yet, so there is nothing to have been opened, clicked, replied to,
// bounced, or unsubscribed from. Showing five "Not available yet"
// placeholders would be as much visual noise as fake 0% values, so this
// tab omits that strip entirely rather than including it in either form --
// see this feature's investigation report for the full reasoning.
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
}: {
  campaign: MailCampaign;
  review: MailCampaignReview | null;
  enrollments: MailEnrollment[];
}) {
  const pendingCount = enrollments.filter((e) => e.status === "pending").length;
  const suppressedCount = enrollments.filter((e) => e.status === "suppressed").length;
  const hasEnrollments = campaign.status !== "draft";

  return (
    <div className="space-y-6">
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
          <p className="mt-3 text-xs text-muted-foreground/70">
            Theoretical total sends is a planning statistic (eligible recipients &times; sequence steps) -- Astronomic
            Mail has no scheduler yet, so this is not a projected send date or a guarantee.
          </p>
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
        <AlertTitle>Sending will be enabled after mailbox setup</AlertTitle>
        <AlertDescription>
          There is no Gmail connection and no send queue in this phase. Marking a campaign Ready only snapshots its
          audience for review -- it never sends or schedules anything.
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
