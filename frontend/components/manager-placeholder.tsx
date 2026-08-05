import type { LucideIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";

interface ManagerPlaceholderProps {
  icon: LucideIcon;
  title: string;
  description: string;
  /** Real, non-fabricated detail (e.g. a route param) -- never simulated data. */
  detail?: string;
}

/**
 * Shared "not built yet" state for every Campaign Manager section. Always
 * says so honestly -- no fake counts, rows, or sample data that could be
 * mistaken for something real.
 */
export function ManagerPlaceholder({ icon: Icon, title, description, detail }: ManagerPlaceholderProps) {
  return (
    <div className="mx-auto max-w-2xl px-6 py-20 text-center">
      <div className="mx-auto mb-5 flex h-12 w-12 items-center justify-center rounded-2xl bg-secondary/60 text-muted-foreground">
        <Icon className="h-5 w-5" />
      </div>
      <div className="mb-3 flex items-center justify-center gap-2">
        <h1 className="font-serif text-xl font-medium tracking-tight">{title}</h1>
        <Badge variant="outline" className="rounded-full font-normal text-muted-foreground">
          Coming soon
        </Badge>
      </div>
      <p className="text-sm text-muted-foreground">{description}</p>
      {detail && <p className="mt-4 text-xs text-muted-foreground/70">{detail}</p>}
    </div>
  );
}
