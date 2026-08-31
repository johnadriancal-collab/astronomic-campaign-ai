import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { MailCampaign, MailEnrollment } from "@/lib/api";
import { mailEnrollmentStatusBadgeClass, mailEnrollmentStatusLabel } from "@/lib/mail";
import { cn } from "@/lib/utils";

// Real MailEnrollment rows only -- pending/suppressed, exactly the two
// states that exist (see MailEnrollmentStatus). No send/open/reply state is
// invented here; Astronomic Mail has no such states yet. MailEnrollment
// doesn't carry a contact name (only crm_contact_id + email_at_enrollment),
// so this shows email + status rather than fetching each contact's name
// individually (would be an N+1 request per lead for a list that can be
// hundreds of rows) -- a deliberate, disclosed simplification.
export function MailCampaignLeadsTab({ campaign, enrollments }: { campaign: MailCampaign; enrollments: MailEnrollment[] }) {
  if (campaign.status === "draft") {
    return (
      <Card>
        <CardContent className="py-10 text-center text-sm text-muted-foreground">
          No leads yet -- enrollments are created when this campaign is marked Ready, as a one-time snapshot of its
          audience at that moment.
        </CardContent>
      </Card>
    );
  }

  if (enrollments.length === 0) {
    return (
      <Card>
        <CardContent className="py-10 text-center text-sm text-muted-foreground">
          This campaign was marked Ready with zero eligible contacts, so no leads were enrolled.
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-sm">Leads ({enrollments.length})</CardTitle>
      </CardHeader>
      <CardContent className="p-0">
        <div className="divide-y divide-border">
          {enrollments.map((enrollment) => (
            <div key={enrollment.enrollment_id} className="flex items-center justify-between gap-3 px-6 py-2.5 text-sm">
              <span className="truncate">{enrollment.email_at_enrollment}</span>
              <span
                className={cn(
                  "shrink-0 rounded-full px-2 py-0.5 text-xs font-medium",
                  mailEnrollmentStatusBadgeClass(enrollment.status)
                )}
              >
                {mailEnrollmentStatusLabel(enrollment.status)}
              </span>
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}
