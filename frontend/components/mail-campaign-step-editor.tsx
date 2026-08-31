import { useEffect, useState } from "react";
import { AlertTriangle, ArrowDown, ArrowUp, Plus, Trash2 } from "lucide-react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { MAIL_TEMPLATE_VARIABLES, type MailSequenceStep } from "@/lib/api";
import { DEFAULT_FOLLOWUP_DELAY_DAYS, formatDayCount, stepTimingLabel, stepTimingSecondaryLabel } from "@/lib/mail";
import type { StepSelection } from "@/lib/mail-campaign-steps";

// The right column of the Steps tab's two-column sequence builder --
// whatever is currently `selection`ed in the timeline (see
// mail-campaign-steps-timeline.tsx) is what's editable here; there is no
// separate "view mode" distinct from this, matching QuickMail's own
// composer (selecting a step immediately shows its editable fields).
//
// Unlike every other tab on this page, the draft text/number fields
// (emailSubject/emailBody/waitDelay/newStepDelay) are LOCAL state, not
// lifted to page.tsx. That's deliberate: the parent (mail-campaign-steps-
// tab.tsx) gives this component a `key` derived from the current
// selection's identity, so React remounts it -- and therefore re-runs
// these `useState(...)` initializers straight from `step` -- every time
// selection changes, and preserves them untouched across every OTHER
// re-render (a save, a steps[] refetch, a reorder). This is React's own
// documented answer for "reset state when switching which item is being
// edited" (see https://react.dev/learn/you-might-not-need-an-effect --
// "Resetting all state when a prop changes"), and it's what let the
// Steps tab drop the setState-in-a-useEffect anti-pattern that used to
// auto-select Step 1: the parent now derives the default selection
// purely during render, and THIS remount mechanism is what gets the
// right initial Subject/Body/Delay showing without any effect at all.
// Every other tab's forms are edited as a single whole and saved as a
// whole, so they've never needed this per-item "which one am I editing
// right now" reset semantics.
//
// Save handlers take the current values as arguments (not read from
// lifted state the parent doesn't have) -- page.tsx's handlers are pure
// "given these values, call the API and update steps[]" functions.
export function MailCampaignStepEditor({
  steps,
  selection,
  editable,
  busy,
  saving,
  error,
  onSaveEmail,
  onAddStep,
  onSaveWait,
  onCancelNewStep,
  onMoveStep,
  onDeleteStep,
  onDirtyChange,
}: {
  steps: MailSequenceStep[];
  selection: StepSelection;
  editable: boolean;
  busy: boolean;
  saving: boolean;
  error: string | null;
  onSaveEmail: (stepId: string, subject: string, body: string) => void;
  onAddStep: (subject: string, body: string, delayDays: number) => void;
  onSaveWait: (stepId: string, delayDays: number) => void;
  onCancelNewStep: () => void;
  onMoveStep: (index: number, direction: -1 | 1) => void;
  onDeleteStep: (stepId: string) => void;
  onDirtyChange: (dirty: boolean) => void;
}) {
  const variablesHint = `Allowed variables: ${MAIL_TEMPLATE_VARIABLES.map((v) => `{{${v}}}`).join(", ")}`;
  const step =
    selection && selection.type !== "new-email" ? steps.find((s) => s.step_id === selection.stepId) ?? null : null;

  const [emailSubject, setEmailSubject] = useState(selection?.type === "email" ? step?.subject ?? "" : "");
  const [emailBody, setEmailBody] = useState(selection?.type === "email" ? step?.body ?? "" : "");
  const [waitDelay, setWaitDelay] = useState(selection?.type === "wait" ? step?.delay_days ?? 0 : 0);
  const [newStepDelay, setNewStepDelay] = useState(steps.length === 0 ? 0 : DEFAULT_FOLLOWUP_DELAY_DAYS);

  const isDirty =
    selection?.type === "email"
      ? step !== null && (emailSubject !== step.subject || emailBody !== step.body)
      : selection?.type === "wait"
        ? step !== null && waitDelay !== step.delay_days
        : selection?.type === "new-email"
          ? emailSubject.trim() !== "" || emailBody.trim() !== "" || (steps.length > 0 && newStepDelay !== DEFAULT_FOLLOWUP_DELAY_DAYS)
          : false;

  // Notifying the parent of a value derived from this component's own
  // state is a legitimate, standard use of an effect (distinct from the
  // "adjust my OWN state from a prop" anti-pattern the surrounding
  // comment above explains this component avoids for selection itself --
  // see React's "You Might Not Need an Effect" article's own "Notifying
  // parent components about state changes" section). `onDirtyChange` is a
  // prop function, never a hook setter from this component, so this
  // cannot re-trigger the react-hooks/set-state-in-effect rule that ruled
  // out the earlier auto-select design.
  useEffect(() => {
    onDirtyChange(isDirty);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isDirty]);

  if (selection === null) {
    return (
      <Card>
        <CardContent className="flex min-h-48 items-center justify-center text-center text-sm text-muted-foreground">
          No steps yet -- add your first email to start this sequence.
        </CardContent>
      </Card>
    );
  }

  if (selection.type === "new-email") {
    const isFirstEver = steps.length === 0;
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-sm">New Email</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          {error && (
            <Alert variant="destructive">
              <AlertTriangle />
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}
          <p className="text-xs text-muted-foreground">{variablesHint}</p>
          <div className="space-y-1">
            <label className="text-xs font-medium text-muted-foreground">Subject</label>
            <Input value={emailSubject} onChange={(e) => setEmailSubject(e.target.value)} placeholder="e.g. Quick intro, {{first_name}}" />
          </div>
          <div className="space-y-1">
            <label className="text-xs font-medium text-muted-foreground">Body</label>
            <Textarea value={emailBody} onChange={(e) => setEmailBody(e.target.value)} placeholder="Write the email body..." rows={12} />
          </div>
          {isFirstEver ? (
            <p className="text-xs text-muted-foreground">Initial email -- Eligible when the lead enters the campaign</p>
          ) : (
            <div className="flex items-center gap-2">
              <label className="text-xs text-muted-foreground">Wait</label>
              <Input
                type="number"
                min={0}
                value={newStepDelay}
                onChange={(e) => setNewStepDelay(Number(e.target.value))}
                className="w-20"
              />
              <label className="text-xs text-muted-foreground">day(s) after previous step</label>
            </div>
          )}
          <div className="flex gap-2">
            <Button
              size="sm"
              onClick={() => onAddStep(emailSubject, emailBody, newStepDelay)}
              disabled={saving || !emailSubject.trim() || !emailBody.trim()}
              className="gap-1.5"
            >
              <Plus className="h-3.5 w-3.5" />
              {saving ? "Adding..." : "Add step"}
            </Button>
            <Button variant="outline" size="sm" onClick={onCancelNewStep} disabled={saving}>
              Cancel
            </Button>
          </div>
        </CardContent>
      </Card>
    );
  }

  if (selection.type === "wait") {
    if (!step) return null; // stale selection (e.g. the step was just deleted) -- the parent clears this on delete
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-sm">Wait</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          {error && (
            <Alert variant="destructive">
              <AlertTriangle />
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}
          {editable ? (
            <div className="flex items-center gap-2 text-sm">
              <label className="text-muted-foreground">Wait</label>
              <Input type="number" min={0} value={waitDelay} onChange={(e) => setWaitDelay(Number(e.target.value))} className="w-20" />
              <span className="text-muted-foreground">day(s) before the next email</span>
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">{formatDayCount(step.delay_days)} before the next email</p>
          )}
          {editable && (
            <div className="flex gap-2">
              <Button size="sm" onClick={() => onSaveWait(step.step_id, waitDelay)} disabled={saving || waitDelay < 0}>
                {saving ? "Saving..." : "Save"}
              </Button>
              <Button variant="outline" size="sm" onClick={() => setWaitDelay(step.delay_days)} disabled={saving}>
                Cancel
              </Button>
            </div>
          )}
        </CardContent>
      </Card>
    );
  }

  // selection.type === "email"
  if (!step) return null; // stale selection -- see the "wait" branch's identical note
  const stepIndex = steps.findIndex((s) => s.step_id === step.step_id);
  const isStep1 = step.step_number === 1;

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between space-y-0">
        <CardTitle className="text-sm">Email</CardTitle>
        {editable && (
          <div className="flex shrink-0 gap-1">
            <Button variant="outline" size="icon-sm" onClick={() => onMoveStep(stepIndex, -1)} disabled={busy || stepIndex === 0} title="Move up">
              <ArrowUp className="h-3.5 w-3.5" />
            </Button>
            <Button
              variant="outline"
              size="icon-sm"
              onClick={() => onMoveStep(stepIndex, 1)}
              disabled={busy || stepIndex === steps.length - 1}
              title="Move down"
            >
              <ArrowDown className="h-3.5 w-3.5" />
            </Button>
            <Button variant="outline" size="icon-sm" onClick={() => onDeleteStep(step.step_id)} disabled={busy} title="Delete step">
              <Trash2 className="h-3.5 w-3.5" />
            </Button>
          </div>
        )}
      </CardHeader>
      <CardContent className="space-y-3">
        {error && (
          <Alert variant="destructive">
            <AlertTriangle />
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        {editable && <p className="text-xs text-muted-foreground">{variablesHint}</p>}
        <div className="space-y-1">
          <label className="text-xs font-medium text-muted-foreground">Subject</label>
          <Input value={emailSubject} onChange={(e) => setEmailSubject(e.target.value)} disabled={!editable} placeholder="Subject" />
        </div>
        <div className="space-y-1">
          <label className="text-xs font-medium text-muted-foreground">Body</label>
          <Textarea value={emailBody} onChange={(e) => setEmailBody(e.target.value)} disabled={!editable} placeholder="Body" rows={12} />
        </div>
        {/* Timing is read-only HERE, always -- Step 1's is permanently 0 and
            uneditable everywhere; Step 2+'s is edited exclusively via its
            preceding Wait node in the timeline (see mail-campaign-steps-
            timeline.tsx), never duplicated as a second editable control
            here too. */}
        <p className="text-xs text-muted-foreground">
          {stepTimingLabel(step)}
          {stepTimingSecondaryLabel(step) && ` -- ${stepTimingSecondaryLabel(step)}`}
          {!isStep1 && " (edit via the Wait step in the timeline)"}
        </p>
        {editable && (
          <div className="flex gap-2">
            <Button
              size="sm"
              onClick={() => onSaveEmail(step.step_id, emailSubject, emailBody)}
              disabled={saving || !emailSubject.trim() || !emailBody.trim()}
            >
              {saving ? "Saving..." : "Save"}
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                setEmailSubject(step.subject);
                setEmailBody(step.body);
              }}
              disabled={saving}
            >
              Cancel
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
