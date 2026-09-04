"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { AddProspectsModal } from "@/components/add-prospects-modal";
import { MailCampaignBatchHistory } from "@/components/mail-campaign-batch-history";
import { MailCampaignWorkloadSummary } from "@/components/mail-campaign-workload-summary";
import { isAddProspectsEligible } from "@/lib/add-prospects-flow";
import type { CrmContactListSummary, MailCampaign, MailCampaignWorkload, MailEnrollment, MailEnrollmentBatch } from "@/lib/api";
import { mailEnrollmentStatusBadgeClass, mailEnrollmentStatusLabel } from "@/lib/mail";
import { cn } from "@/lib/utils";

// Real MailEnrollment rows -- see MailEnrollmentStatus in api.ts for the
// full real set (pending/active/paused/completed/suppressed/failed).
// MailEnrollment doesn't carry a contact name (only crm_contact_id +
// email_at_enrollment), so this shows email + status rather than fetching
// each contact's name individually (would be an N+1 request per lead for
// a list that can be hundreds of rows) -- a deliberate, disclosed
// simplification. Engagement-tracking fields are Phase 3 (Journeys), not
// implemented yet, and this file deliberately never fabricates them.
export function MailCampaignLeadsTab({
  campaign,
  enrollments,
  workload,
  batches,
  lists,
  onProspectsAdded,
}: {
  campaign: MailCampaign;
  enrollments: MailEnrollment[];
  workload: MailCampaignWorkload | null;
  batches: MailEnrollmentBatch[];
  lists: CrmContactListSummary[];
  onProspectsAdded: (batch: MailEnrollmentBatch) => void;
}) {
  const [addProspectsOpen, setAddProspectsOpen] = useState(false);

  // Frontend gating is convenience only -- it never replaces the backend's
  // own authoritative eligibility check in MailCampaignService.
  // add_prospects() (and the same check re-run by MailCampaignCsvProspectService's
  // preflight). A legacy COMPLETED campaign is intentionally included:
  // the backend can reopen it to ACTIVE once Add Prospects genuinely
  // enrolls someone new -- see that method's own docstring.
  const canAddProspects = isAddProspectsEligible(campaign.status);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-medium text-muted-foreground">Leads</h2>
        {canAddProspects && <Button onClick={() => setAddProspectsOpen(true)}>Add Prospects</Button>}
      </div>

      {workload && <MailCampaignWorkloadSummary workload={workload} />}

      {campaign.status === "draft" ? (
        <Card>
          <CardContent className="py-10 text-center text-sm text-muted-foreground">
            No leads yet -- an initial batch of enrollments is created when this campaign is marked Ready, as a
            snapshot of its audience at that moment. More prospects can be added after the campaign is activated.
          </CardContent>
        </Card>
      ) : enrollments.length === 0 ? (
        <Card>
          <CardContent className="py-10 text-center text-sm text-muted-foreground">
            {canAddProspects
              ? "No leads yet. Use Add Prospects to bring in contacts from a CRM List or a CSV upload."
              : "This campaign was marked Ready with zero eligible contacts, so no leads were enrolled."}
          </CardContent>
        </Card>
      ) : (
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
      )}

      <MailCampaignBatchHistory batches={batches} />

      <AddProspectsModal
        open={addProspectsOpen}
        onOpenChange={setAddProspectsOpen}
        campaignId={campaign.mail_campaign_id}
        lists={lists}
        onSuccess={onProspectsAdded}
      />
    </div>
  );
}
