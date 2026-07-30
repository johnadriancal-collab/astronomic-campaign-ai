import { CheckCircle2, Circle, Loader2, XCircle } from "lucide-react";
import { cn } from "@/lib/utils";

export type StageStatus = "pending" | "active" | "done" | "error";

export interface Stage {
  key: string;
  label: string;
  detail?: string;
  status: StageStatus;
}

function StageIcon({ status }: { status: StageStatus }) {
  if (status === "done") return <CheckCircle2 className="h-4 w-4 text-emerald-400" />;
  if (status === "active") return <Loader2 className="h-4 w-4 animate-spin text-primary" />;
  if (status === "error") return <XCircle className="h-4 w-4 text-destructive" />;
  return <Circle className="h-4 w-4 text-muted-foreground/40" />;
}

export function StageList({ stages }: { stages: Stage[] }) {
  return (
    <ul className="space-y-3">
      {stages.map((stage, i) => (
        <li
          key={stage.key}
          className={cn(
            "flex items-start gap-3 animate-in fade-in slide-in-from-left-2 duration-500",
            stage.status === "pending" && "opacity-50"
          )}
          style={{ animationDelay: `${i * 60}ms` }}
        >
          <div className="mt-0.5 shrink-0">
            <StageIcon status={stage.status} />
          </div>
          <div className="min-w-0">
            <p
              className={cn(
                "text-sm font-medium leading-none",
                stage.status === "error" ? "text-destructive" : "text-foreground"
              )}
            >
              {stage.label}
            </p>
            {stage.detail && (
              <p className="mt-1 text-xs text-muted-foreground break-words">{stage.detail}</p>
            )}
          </div>
        </li>
      ))}
    </ul>
  );
}
