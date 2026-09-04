"use client";

import { Zap } from "lucide-react";
import { Tooltip, TooltipPopup, TooltipTrigger } from "@/components/ui/tooltip";
import type { TriggerMarker } from "@/lib/mail-trigger";

// Stage 5F.1 -- a pure VISUALIZATION of an already-configured, already-
// enabled lead-start trigger, positioned on the SAME desktop timeline
// track schedule-window-block.tsx's send-window bars use (see this
// component's own caller, schedule-day-row.tsx, for why it renders in an
// UNCLIPPED sibling overlay rather than inside the track's own
// `overflow-hidden` box). Never draggable, never opens an edit flow by
// itself -- Trigger editing stays exclusively in the management table
// below (mail-campaign-triggers-card.tsx). Deliberately independent of
// send-window positions/bounds -- a trigger firing outside the
// configured sending hours (a real, valid, backend-supported
// configuration) still renders here at its own true time; this component
// has no awareness of windows at all.
export function ScheduleTriggerMarker({ marker }: { marker: TriggerMarker }) {
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <button
            type="button"
            aria-label={`Lead start trigger: ${marker.detail}`}
            className="pointer-events-auto absolute top-0 z-20 flex h-3.5 w-3.5 -translate-x-1/2 -translate-y-1/2 cursor-default items-center justify-center rounded-full bg-primary text-primary-foreground shadow-sm ring-2 ring-background focus-visible:outline-none focus-visible:ring-ring"
            style={{ left: `${marker.leftPct}%` }}
          >
            <Zap className="h-2 w-2" fill="currentColor" strokeWidth={0} />
          </button>
        }
      />
      <TooltipPopup>
        <p className="font-medium text-foreground">Lead start trigger</p>
        <p className="text-muted-foreground">{marker.detail}</p>
      </TooltipPopup>
    </Tooltip>
  );
}
