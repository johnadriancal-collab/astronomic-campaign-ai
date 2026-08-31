import { AlertTriangle, ArrowDown, ArrowUp, Pencil, Plus, Trash2 } from "lucide-react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { MAIL_TEMPLATE_VARIABLES, type MailSequenceStep } from "@/lib/api";

// Moved as-is from the old single-page layout's "Sequence" card -- same
// handlers, same editable gating, no behavior changes.
export function MailCampaignStepsTab({
  steps,
  editable,
  busy,
  onMoveStep,
  onDeleteStep,
  onAddStep,
  stepSubject,
  setStepSubject,
  stepBody,
  setStepBody,
  stepDelay,
  setStepDelay,
  addingStep,
  stepError,
  editingStepId,
  onStartEditStep,
  onCancelEditStep,
  onSaveEditStep,
  editSubject,
  setEditSubject,
  editBody,
  setEditBody,
  editDelay,
  setEditDelay,
  savingStepEdit,
  stepEditError,
}: {
  steps: MailSequenceStep[];
  editable: boolean;
  busy: boolean;
  onMoveStep: (index: number, direction: -1 | 1) => void;
  onDeleteStep: (stepId: string) => void;
  onAddStep: (e: React.FormEvent) => void;
  stepSubject: string;
  setStepSubject: (value: string) => void;
  stepBody: string;
  setStepBody: (value: string) => void;
  stepDelay: number;
  setStepDelay: (value: number) => void;
  addingStep: boolean;
  stepError: string | null;
  // Inline per-step editing -- a step is only ever edited one at a time
  // (editingStepId holds that one step_id, or null). Cancel restores the
  // last-saved values with no backend write; Save PATCHes only this step,
  // never touching step_number/other steps. See page.tsx's handlers.
  editingStepId: string | null;
  onStartEditStep: (step: MailSequenceStep) => void;
  onCancelEditStep: () => void;
  onSaveEditStep: (stepId: string) => void;
  editSubject: string;
  setEditSubject: (value: string) => void;
  editBody: string;
  setEditBody: (value: string) => void;
  editDelay: number;
  setEditDelay: (value: number) => void;
  savingStepEdit: boolean;
  stepEditError: string | null;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-sm">Sequence</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {steps.length === 0 && <p className="text-sm text-muted-foreground">No steps yet.</p>}
        {steps.map((step, i) => {
          const isEditing = step.step_id === editingStepId;
          return (
            <div key={step.step_id} className="rounded-md border border-border p-3 text-sm">
              {isEditing ? (
                <div className="space-y-2">
                  {stepEditError && (
                    <Alert variant="destructive">
                      <AlertTriangle />
                      <AlertDescription>{stepEditError}</AlertDescription>
                    </Alert>
                  )}
                  <p className="text-xs font-medium text-muted-foreground">
                    Editing Step {step.step_number} -- allowed variables: {MAIL_TEMPLATE_VARIABLES.map((v) => `{{${v}}}`).join(", ")}
                  </p>
                  <Input value={editSubject} onChange={(e) => setEditSubject(e.target.value)} placeholder="Subject" />
                  <Textarea value={editBody} onChange={(e) => setEditBody(e.target.value)} placeholder="Body" rows={3} />
                  <div className="flex items-center gap-2">
                    <label className="text-xs text-muted-foreground">Wait</label>
                    <Input
                      type="number"
                      min={0}
                      value={editDelay}
                      onChange={(e) => setEditDelay(Number(e.target.value))}
                      className="w-20"
                    />
                    <label className="text-xs text-muted-foreground">day(s) after previous step</label>
                  </div>
                  <div className="flex gap-2">
                    <Button
                      size="sm"
                      onClick={() => onSaveEditStep(step.step_id)}
                      disabled={savingStepEdit || !editSubject.trim() || !editBody.trim()}
                    >
                      {savingStepEdit ? "Saving..." : "Save"}
                    </Button>
                    <Button variant="outline" size="sm" onClick={onCancelEditStep} disabled={savingStepEdit}>
                      Cancel
                    </Button>
                  </div>
                </div>
              ) : (
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <p className="font-medium">
                      Step {step.step_number}: {step.subject}
                    </p>
                    <p className="mt-1 whitespace-pre-wrap text-muted-foreground">{step.body}</p>
                    <p className="mt-1 text-xs text-muted-foreground">
                      {step.delay_days === 0 ? "Sent immediately" : `${step.delay_days} day${step.delay_days === 1 ? "" : "s"} after previous step`}
                    </p>
                  </div>
                  {editable && (
                    <div className="flex shrink-0 gap-1">
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => onStartEditStep(step)}
                        disabled={busy || editingStepId !== null}
                      >
                        <Pencil className="h-3.5 w-3.5" />
                      </Button>
                      <Button variant="outline" size="sm" onClick={() => onMoveStep(i, -1)} disabled={busy || i === 0}>
                        <ArrowUp className="h-3.5 w-3.5" />
                      </Button>
                      <Button variant="outline" size="sm" onClick={() => onMoveStep(i, 1)} disabled={busy || i === steps.length - 1}>
                        <ArrowDown className="h-3.5 w-3.5" />
                      </Button>
                      <Button variant="outline" size="sm" onClick={() => onDeleteStep(step.step_id)} disabled={busy}>
                        <Trash2 className="h-3.5 w-3.5" />
                      </Button>
                    </div>
                  )}
                </div>
              )}
            </div>
          );
        })}

        {editable && (
          <form onSubmit={onAddStep} className="space-y-2 rounded-md border border-dashed border-border p-3">
            {stepError && (
              <Alert variant="destructive">
                <AlertTriangle />
                <AlertDescription>{stepError}</AlertDescription>
              </Alert>
            )}
            <p className="text-xs font-medium text-muted-foreground">
              Add a step -- allowed variables: {MAIL_TEMPLATE_VARIABLES.map((v) => `{{${v}}}`).join(", ")}
            </p>
            <Input value={stepSubject} onChange={(e) => setStepSubject(e.target.value)} placeholder="Subject, e.g. Quick intro, {{first_name}}" />
            <Textarea value={stepBody} onChange={(e) => setStepBody(e.target.value)} placeholder="Body" rows={3} />
            <div className="flex items-center gap-2">
              <label className="text-xs text-muted-foreground">Wait</label>
              <Input
                type="number"
                min={0}
                value={stepDelay}
                onChange={(e) => setStepDelay(Number(e.target.value))}
                className="w-20"
              />
              <label className="text-xs text-muted-foreground">day(s) after previous step</label>
            </div>
            <Button type="submit" size="sm" disabled={addingStep || !stepSubject.trim() || !stepBody.trim()} className="gap-1.5">
              <Plus className="h-3.5 w-3.5" />
              {addingStep ? "Adding..." : "Add step"}
            </Button>
          </form>
        )}
      </CardContent>
    </Card>
  );
}
