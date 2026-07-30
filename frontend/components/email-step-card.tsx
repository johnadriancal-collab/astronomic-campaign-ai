import { Mail } from "lucide-react";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import type { SequenceStep } from "@/lib/api";

export function EmailStepCard({ step, index }: { step: SequenceStep; index: number }) {
  return (
    <Card
      className="animate-in fade-in slide-in-from-bottom-3 gap-3 rounded-2xl border-border/60 bg-card/70 py-4 duration-500"
      style={{ animationDelay: `${index * 90}ms` }}
    >
      <CardHeader className="gap-1.5">
        <div className="flex items-center justify-between">
          <Badge
            variant="outline"
            className="rounded-full border-primary/30 bg-primary/10 text-primary"
          >
            Day {step.day}
          </Badge>
          <Mail className="h-3.5 w-3.5 text-muted-foreground" />
        </div>
        <h3 className="text-sm font-semibold leading-snug">{step.subject}</h3>
      </CardHeader>
      <CardContent>
        <p className="whitespace-pre-line text-sm leading-relaxed text-muted-foreground">
          {step.body}
        </p>
      </CardContent>
    </Card>
  );
}
