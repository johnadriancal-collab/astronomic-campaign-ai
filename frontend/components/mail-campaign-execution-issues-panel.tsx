"use client";

import { useEffect, useState } from "react";
import { AlertTriangle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  ApiError,
  listMailExecutionSteps,
  resolveMailExecutionStepNotSent,
  resolveMailExecutionStepSent,
  type MailExecutionStepView,
} from "@/lib/api";
import { mailExecutionStepStatusBadgeClass, mailExecutionStepStatusLabel } from "@/lib/mail";
import { cn } from "@/lib/utils";

// P0-2 (2026-09-15) -- the minimal operator surface for failed/unknown
// sends. Deliberately NOT an analytics dashboard: no charts, no rates, no
// historical trends -- just the individual rows that need a human's
// attention, and the two existing resolve-sent/resolve-not-sent actions
// (already real backend routes, previously reachable only via curl/Swagger
// -- see app/api/mail.py's own module docstring) for UNKNOWN rows. FAILED
// rows show no action at all: FAILED is terminal (see
// MailEnrollmentStepStatus's own docstring -- "retrying an identical
// request against an identical recipient would fail identically"), and
// UNKNOWN must stay human-controlled, never auto-retried -- this panel
// never calls either resolve route on its own.
export function MailCampaignExecutionIssuesPanel({ campaignId }: { campaignId: string }) {
  const [rows, setRows] = useState<MailExecutionStepView[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const reload = () => {
    listMailExecutionSteps(campaignId, ["failed", "unknown"])
      .then(setRows)
      .catch((err) => setLoadError(err instanceof ApiError ? err.message : "Couldn't reach the backend."));
  };

  useEffect(() => {
    reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [campaignId]);

  if (loadError) {
    return (
      <Card>
        <CardContent className="py-4 text-sm text-destructive">
          Couldn&apos;t load send status: {loadError}
        </CardContent>
      </Card>
    );
  }

  // Still loading, or nothing to show -- this panel stays silent rather
  // than announcing "0 issues" for the common, healthy case.
  if (rows === null || rows.length === 0) return null;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-sm text-destructive">
          <AlertTriangle className="h-4 w-4" />
          Needs attention ({rows.length})
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 p-0 pb-4">
        <div className="divide-y divide-border">
          {rows.map((row) => (
            <ExecutionIssueRow key={row.enrollment_step_id} row={row} onResolved={reload} />
          ))}
        </div>
      </CardContent>
    </Card>
  );
}

function ExecutionIssueRow({ row, onResolved }: { row: MailExecutionStepView; onResolved: () => void }) {
  const [resolving, setResolving] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [confirmingSent, setConfirmingSent] = useState(false);
  const [providerMessageId, setProviderMessageId] = useState("");
  const [providerThreadId, setProviderThreadId] = useState("");

  const confirmSent = async () => {
    if (!providerMessageId.trim() || !providerThreadId.trim()) {
      setActionError("Both the Gmail message ID and thread ID are required.");
      return;
    }
    setResolving(true);
    setActionError(null);
    try {
      await resolveMailExecutionStepSent(row.enrollment_step_id, providerMessageId.trim(), providerThreadId.trim());
      onResolved();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "Couldn't reach the backend.");
    } finally {
      setResolving(false);
    }
  };

  const confirmNotSent = async () => {
    setResolving(true);
    setActionError(null);
    try {
      await resolveMailExecutionStepNotSent(row.enrollment_step_id);
      onResolved();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "Couldn't reach the backend.");
    } finally {
      setResolving(false);
    }
  };

  return (
    <div className="space-y-2 px-6 py-3 text-sm">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <div className="truncate font-medium">{row.prospect_name || row.prospect_email}</div>
          <div className="truncate text-xs text-muted-foreground">
            {row.prospect_email} · Step {row.step_number}
            {row.last_attempt_at && ` · ${new Date(row.last_attempt_at).toLocaleString()}`}
          </div>
        </div>
        <span className={cn("shrink-0 rounded-full px-2 py-0.5 text-xs font-medium", mailExecutionStepStatusBadgeClass(row.status))}>
          {mailExecutionStepStatusLabel(row.status)}
        </span>
      </div>

      {row.last_error && <p className="text-xs text-muted-foreground">{row.last_error}</p>}
      {row.gmail_message_id && (
        <p className="font-mono text-xs text-muted-foreground/70">Provider message ID: {row.gmail_message_id}</p>
      )}

      {row.status === "unknown" && (
        <div className="space-y-2 rounded-md border border-border bg-secondary/30 p-3">
          <p className="text-xs text-muted-foreground">
            This send&apos;s outcome is unconfirmed (the provider call didn&apos;t return a definite result). Check
            Gmail directly, then record what actually happened -- nothing here is ever retried automatically.
          </p>
          {!confirmingSent ? (
            <div className="flex flex-wrap gap-2">
              <Button size="sm" variant="outline" onClick={() => setConfirmingSent(true)} disabled={resolving}>
                It was sent
              </Button>
              <Button size="sm" variant="outline" onClick={confirmNotSent} disabled={resolving}>
                It was NOT sent
              </Button>
            </div>
          ) : (
            <div className="space-y-2">
              <div className="flex flex-wrap gap-2">
                <Input
                  placeholder="Gmail message ID"
                  value={providerMessageId}
                  onChange={(e) => setProviderMessageId(e.target.value)}
                  className="max-w-56"
                />
                <Input
                  placeholder="Gmail thread ID"
                  value={providerThreadId}
                  onChange={(e) => setProviderThreadId(e.target.value)}
                  className="max-w-56"
                />
              </div>
              <div className="flex gap-2">
                <Button size="sm" onClick={confirmSent} disabled={resolving}>
                  Confirm sent
                </Button>
                <Button size="sm" variant="ghost" onClick={() => setConfirmingSent(false)} disabled={resolving}>
                  Cancel
                </Button>
              </div>
            </div>
          )}
          {actionError && <p className="text-xs text-destructive">{actionError}</p>}
        </div>
      )}
    </div>
  );
}
