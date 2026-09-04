import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { mailEnrollmentBatchSourceLabel, summarizeBatchResult, suppressedSubsetNote } from "@/lib/add-prospects-flow";
import type { MailEnrollmentBatch } from "@/lib/api";

// Read-only Add Prospects provenance for the Leads tab (Stage 4B,
// 2026-09-03) -- GET /mail/campaigns/{id}/batches. Deliberately never
// renders batch.import_batch_id or batch.idempotency_key (the user never
// needs to know about either -- see AddProspectsModal's own docstring),
// never fetches or shows raw CSV row contents, and doesn't attempt to
// show the original CSV filename (that would require a separate,
// human-session-only /crm/import/{id} fetch per CSV-sourced batch --
// deferred past V1, not required here).
export function MailCampaignBatchHistory({ batches }: { batches: MailEnrollmentBatch[] }) {
  if (batches.length === 0) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-sm">Batch History</CardTitle>
        </CardHeader>
        <CardContent className="py-6 text-center text-sm text-muted-foreground">
          No prospects have been added to this campaign yet.
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-sm">Batch History</CardTitle>
      </CardHeader>
      <CardContent className="p-0">
        <div className="divide-y divide-border">
          {batches.map((batch) => {
            const summary = summarizeBatchResult(batch);
            return (
              <div key={batch.batch_id} className="space-y-1 px-6 py-3 text-sm">
                <div className="flex items-center justify-between gap-3">
                  <span className="font-medium">{mailEnrollmentBatchSourceLabel(batch.source)}</span>
                  <span className="text-xs text-muted-foreground">{new Date(batch.created_at).toLocaleString()}</span>
                </div>
                {batch.status === "preparing" ? (
                  <p className="text-xs text-muted-foreground">Still processing...</p>
                ) : (
                  <>
                    <p className="text-xs text-muted-foreground">
                      {summary.usableContacts} usable contact{summary.usableContacts === 1 ? "" : "s"} -- {summary.newlyAdded} newly
                      added, {summary.alreadyInCampaign} already in this campaign
                    </p>
                    {summary.suppressedOfNewlyAdded > 0 && (
                      <p className="text-xs text-muted-foreground/80">{suppressedSubsetNote(summary)}</p>
                    )}
                  </>
                )}
              </div>
            );
          })}
        </div>
      </CardContent>
    </Card>
  );
}
