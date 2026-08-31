import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogClose, DialogDescription, DialogFooter, DialogHeader, DialogPopup, DialogTitle } from "@/components/ui/dialog";
import { MailCampaignStepsTimeline } from "@/components/mail-campaign-steps-timeline";
import { MailCampaignStepEditor } from "@/components/mail-campaign-step-editor";
import type { MailSequenceStep } from "@/lib/api";
import type { StepSelection } from "@/lib/mail-campaign-steps";

// The Steps tab's own two-column sequence builder layout -- a narrow
// timeline on the left (mail-campaign-steps-timeline.tsx), a persistent
// editor for whatever's selected on the right (mail-campaign-step-editor.tsx).
// Stacks to a single column below `lg` (1024px) -- same breakpoint choice
// as the Schedule tab's own desktop/mobile split (see schedule-day-row.tsx's
// docstring for why lg, not md, was picked there), timeline first so a
// narrow-viewport user sees the sequence structure before an editor for
// whichever step happens to be selected.
//
// This component owns exactly two pieces of local state, both scoped to
// mediating Timeline<->Editor interaction and deliberately NOT lifted to
// page.tsx: `isEditorDirty` (reported by the editor whenever its unsaved
// draft differs from what's persisted) and `pendingAction` (a switch-
// selection/delete the user tried to make while dirty, held until they
// confirm). Every other tab's state and API-call handlers still live in
// page.tsx exactly as before -- selecting/saving/adding/deleting a step is
// unaffected in success/failure; the only thing added here is a single gate
// in front of any action that would silently discard an unsaved draft.
export function MailCampaignStepsTab({
  steps,
  editable,
  busy,
  selection,
  onSelectEmail,
  onSelectWait,
  onStartAddStep,
  onMoveStep,
  onDeleteStep,
  onSaveEmail,
  onAddStep,
  onSaveWait,
  onCancelNewStep,
  savingSelection,
  selectionError,
}: {
  steps: MailSequenceStep[];
  editable: boolean;
  busy: boolean;
  selection: StepSelection;
  onSelectEmail: (step: MailSequenceStep) => void;
  onSelectWait: (step: MailSequenceStep) => void;
  onStartAddStep: () => void;
  onMoveStep: (index: number, direction: -1 | 1) => void;
  onDeleteStep: (stepId: string) => void;
  onSaveEmail: (stepId: string, subject: string, body: string) => void;
  onAddStep: (subject: string, body: string, delayDays: number) => void;
  onSaveWait: (stepId: string, delayDays: number) => void;
  onCancelNewStep: () => void;
  savingSelection: boolean;
  selectionError: string | null;
}) {
  const [isEditorDirty, setIsEditorDirty] = useState(false);
  // The switch/delete the user tried to make while the editor was dirty --
  // held here, not run, until they explicitly confirm via the dialog below.
  const [pendingAction, setPendingAction] = useState<(() => void) | null>(null);

  // Reordering (Move up/down) is NEVER gated here -- the editor's `key`
  // (below) is the selected step's own id, which a reorder never changes,
  // so the open draft survives a reorder untouched regardless of dirtiness.
  function attempt(action: () => void) {
    if (isEditorDirty) {
      setPendingAction(() => action);
    } else {
      action();
    }
  }

  const guardedSelectEmail = (step: MailSequenceStep) => attempt(() => onSelectEmail(step));
  const guardedSelectWait = (step: MailSequenceStep) => attempt(() => onSelectWait(step));
  const guardedStartAddStep = () => attempt(() => onStartAddStep());
  const guardedDeleteStep = (stepId: string) => attempt(() => onDeleteStep(stepId));

  const editorKey = selection === null ? "empty" : selection.type === "new-email" ? "new-email" : `${selection.type}-${selection.stepId}`;

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-[300px_1fr] lg:items-start">
      <MailCampaignStepsTimeline
        steps={steps}
        selection={selection}
        onSelectEmail={guardedSelectEmail}
        onSelectWait={guardedSelectWait}
        onStartAddStep={guardedStartAddStep}
        editable={editable}
      />
      <MailCampaignStepEditor
        key={editorKey}
        steps={steps}
        selection={selection}
        editable={editable}
        busy={busy}
        saving={savingSelection}
        error={selectionError}
        onSaveEmail={onSaveEmail}
        onAddStep={onAddStep}
        onSaveWait={onSaveWait}
        onCancelNewStep={onCancelNewStep}
        onMoveStep={onMoveStep}
        onDeleteStep={guardedDeleteStep}
        onDirtyChange={setIsEditorDirty}
      />

      <Dialog open={pendingAction !== null} onOpenChange={(open) => { if (!open) setPendingAction(null); }}>
        <DialogPopup className="max-w-sm">
          <DialogHeader>
            <DialogTitle>Discard unsaved changes?</DialogTitle>
            <DialogDescription>You have changes that haven&apos;t been saved.</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <DialogClose render={<Button type="button" variant="outline">Keep editing</Button>} />
            <Button
              type="button"
              variant="destructive"
              onClick={() => {
                pendingAction?.();
                setPendingAction(null);
              }}
            >
              Discard changes
            </Button>
          </DialogFooter>
        </DialogPopup>
      </Dialog>
    </div>
  );
}
