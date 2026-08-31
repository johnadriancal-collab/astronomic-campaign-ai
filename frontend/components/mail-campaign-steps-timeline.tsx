import { Clock, Mail, Plus } from "lucide-react";
import { buildStepTimeline, stepBodyPreview, type StepSelection } from "@/lib/mail-campaign-steps";
import { formatDayCount } from "@/lib/mail";
import { cn } from "@/lib/utils";
import type { MailSequenceStep } from "@/lib/api";

// The left column of the Steps tab's two-column sequence builder -- a
// vertical, visually-connected list of Email/Wait nodes (see
// lib/mail-campaign-steps.ts's buildStepTimeline() for how the flat
// steps[] array becomes this alternating node list). Purely presentational:
// every node is a button: clicking one selects it, which is what drives
// what the editor panel (mail-campaign-step-editor.tsx) shows -- this
// component owns no state of its own.
export function MailCampaignStepsTimeline({
  steps,
  selection,
  onSelectEmail,
  onSelectWait,
  onStartAddStep,
  editable,
}: {
  steps: MailSequenceStep[];
  selection: StepSelection;
  onSelectEmail: (step: MailSequenceStep) => void;
  onSelectWait: (step: MailSequenceStep) => void;
  onStartAddStep: () => void;
  editable: boolean;
}) {
  const nodes = buildStepTimeline(steps);

  return (
    <div className="relative">
      {/* The connecting line -- a single continuous vertical rule behind
          every node's icon, matching QuickMail's threaded-timeline look.
          Positioned to run through the icon column (see each node's
          left-padding below); only rendered when there's more than one
          node to connect. */}
      {nodes.length > 1 && <div className="absolute top-4 bottom-4 left-[15px] w-px bg-border" aria-hidden="true" />}

      <div className="relative space-y-1.5">
        {nodes.map((node) => {
          if (node.kind === "email") {
            const stepNumber = node.step.step_number;
            const isSelected = selection?.type === "email" && selection.stepId === node.step.step_id;
            return (
              <button
                key={`email-${node.step.step_id}`}
                type="button"
                onClick={() => onSelectEmail(node.step)}
                className={cn(
                  "relative flex w-full items-start gap-2.5 rounded-md border p-2.5 text-left text-sm transition-colors",
                  isSelected ? "border-primary bg-primary/5" : "border-border bg-card hover:bg-muted/50"
                )}
              >
                <span
                  className={cn(
                    "flex h-6 w-6 shrink-0 items-center justify-center rounded-full border bg-background",
                    isSelected ? "border-primary text-primary" : "border-border text-muted-foreground"
                  )}
                >
                  <Mail className="h-3.5 w-3.5" />
                </span>
                <div className="min-w-0 flex-1">
                  <p className="text-xs font-medium text-muted-foreground">Step {stepNumber} — Email</p>
                  <p className="truncate font-medium">{node.step.subject || "Untitled email"}</p>
                  {node.step.body && (
                    <p className="mt-0.5 truncate text-xs text-muted-foreground">{stepBodyPreview(node.step.body)}</p>
                  )}
                </div>
              </button>
            );
          }

          const isSelected = selection?.type === "wait" && selection.stepId === node.step.step_id;
          return (
            <button
              key={`wait-${node.step.step_id}`}
              type="button"
              onClick={() => onSelectWait(node.step)}
              className={cn(
                "relative flex w-full items-center gap-2.5 rounded-md border border-dashed p-1.5 pl-2.5 text-left text-xs transition-colors",
                isSelected ? "border-primary bg-primary/5 text-foreground" : "border-transparent text-muted-foreground hover:bg-muted/50"
              )}
            >
              <span
                className={cn(
                  "flex h-6 w-6 shrink-0 items-center justify-center rounded-full border bg-background",
                  isSelected ? "border-primary text-primary" : "border-border text-muted-foreground"
                )}
              >
                <Clock className="h-3 w-3" />
              </span>
              <span>Wait — {formatDayCount(node.step.delay_days)}</span>
            </button>
          );
        })}

        {editable && (
          <button
            type="button"
            onClick={onStartAddStep}
            disabled={selection?.type === "new-email"}
            className="relative flex w-full items-center gap-2.5 rounded-md border border-dashed border-border p-2.5 text-left text-sm text-muted-foreground transition-colors hover:border-primary/50 hover:text-foreground disabled:cursor-not-allowed disabled:opacity-60"
          >
            <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full border border-dashed border-border">
              <Plus className="h-3.5 w-3.5" />
            </span>
            Add a new step
          </button>
        )}
      </div>
    </div>
  );
}
