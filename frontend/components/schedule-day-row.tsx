"use client";

import { useRef } from "react";
import { Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ScheduleWindowBlock } from "@/components/schedule-window-block";
import {
  MinuteWindow,
  TIMELINE_HOUR_MARKS,
  defaultNewWindow,
  formatHourMark,
  minutesFromTimeString,
  neighborBounds,
  newLocalWindowId,
  removeWindowById,
  timeStringFromMinutes,
} from "@/lib/schedule";
import { WEEKDAY_LABELS } from "@/lib/mail";
import { cn } from "@/lib/utils";

export type EditableWindow = MinuteWindow & { id: string };

// One weekday's full row: the day label, the desktop-only visual 24h drag
// timeline (hidden below `md` -- see the module docstring in
// mail-campaign-schedule-tab.tsx for why), and the ALWAYS-visible accessible
// manual start/end inputs per window (the one thing keyboard/mobile users
// actually configure the schedule with -- dragging is a bonus, never the
// only way in). A day with zero windows renders visually inactive (label +
// empty track) rather than disappearing -- "enabled" is simply "has >=1 window".
export function ScheduleDayRow({
  day,
  windows,
  onWindowsChange,
  anyWindowInCampaign,
  readOnly,
}: {
  day: number;
  windows: EditableWindow[];
  onWindowsChange: (next: EditableWindow[]) => void;
  anyWindowInCampaign: MinuteWindow | null;
  readOnly: boolean;
}) {
  const trackRef = useRef<HTMLDivElement>(null);
  // A plain incrementing counter, not Date.now()/Math.random() -- both are
  // impure and React's purity rules (correctly) flag calling them from a
  // component-scoped closure. A per-row monotonic counter is deterministic
  // and unique-enough for its only job: keying new, not-yet-saved windows
  // until the next real save round-trips real window_ids back from the API.
  const nextNewWindowId = useRef(0);
  const sorted = [...windows].sort((a, b) => a.start - b.start);
  const isActive = sorted.length > 0;

  function updateWindow(id: string, next: MinuteWindow) {
    onWindowsChange(windows.map((w) => (w.id === id ? { ...w, ...next } : w)));
  }

  function removeWindow(id: string) {
    onWindowsChange(removeWindowById(windows, id));
  }

  function addWindow() {
    const next = defaultNewWindow(sorted, anyWindowInCampaign);
    if (!next) return;
    const id = newLocalWindowId(day, nextNewWindowId.current++);
    onWindowsChange([...windows, { ...next, id }]);
  }

  const canAdd = !readOnly && defaultNewWindow(sorted, anyWindowInCampaign) !== null;

  return (
    <div className={cn("flex items-start gap-3 border-b border-border/40 py-3 last:border-b-0", !isActive && "opacity-60")}>
      <div className="w-9 shrink-0 pt-1.5 text-xs font-semibold tracking-wide text-muted-foreground">
        {WEEKDAY_LABELS[day]}
      </div>

      <div className="min-w-0 flex-1 space-y-2">
        {/* Desktop visual timeline -- hidden below `lg` (1024px), not just
            `md` (768px): with all 24 hours labeled, "12AM"/"12PM" (4-5
            characters) no longer fit their ~1/24-of-track slot without
            crowding the adjacent bare-number hour once the track drops
            below ~600px wide (empirically confirmed clean at 1024px
            viewport, overlapping by a few px at 800-900px) -- see
            mail-campaign-schedule-tab.tsx for the broader mobile-fallback
            rationale this reuses rather than inventing a second, separate
            "medium-narrow" label density. */}
        <div className="hidden lg:block">
          <div ref={trackRef} className="relative h-10 overflow-hidden rounded-md border border-border/60 bg-secondary/20">
            {TIMELINE_HOUR_MARKS.slice(1, -1).map((h) => (
              <div
                key={h}
                className="pointer-events-none absolute inset-y-0 w-px bg-border/40"
                style={{ left: `${(h / 24) * 100}%` }}
              />
            ))}
            {sorted.map((w, i) => {
              const { prevEnd, nextStart } = neighborBounds(sorted, i);
              return (
                <ScheduleWindowBlock
                  key={w.id}
                  value={w}
                  prevEnd={prevEnd}
                  nextStart={nextStart}
                  trackRef={trackRef}
                  onChange={(next) => updateWindow(w.id, next)}
                  onRemove={() => removeWindow(w.id)}
                  readOnly={readOnly}
                />
              );
            })}
          </div>
          {/* Every label (including the two "12AM" endpoints) is centered
              on its own tick via translateX(-50%) -- uniformly, no special
              edge-anchoring. Centering the wide "12AM"/"12PM" labels the
              SAME way as the narrow "1"-"11" ones means each grows equally
              in both directions from its own tick instead of growing
              entirely toward its neighbor, which is what caused "12AM" to
              visually crowd "1" (and "11" to crowd the closing "12AM") at
              narrower desktop widths. The two endpoint labels spill a few
              px past the track's own left/right edge as a result -- this
              row has no overflow-hidden (only the track above it does), so
              nothing clips; that small overflow into the row's own
              surrounding whitespace is the standard, expected look for a
              ruler-style timeline's first/last tick. */}
          <div className="relative mt-0.5 h-3 text-[10px] whitespace-nowrap text-muted-foreground/60">
            {TIMELINE_HOUR_MARKS.map((h) => (
              <span key={h} className="absolute -translate-x-1/2" style={{ left: `${(h / 24) * 100}%` }}>
                {formatHourMark(h)}
              </span>
            ))}
          </div>
        </div>

        {/* Accessible manual controls -- the real, always-available way to
            configure every window's exact start/end, on every screen size. */}
        <div className="space-y-1.5">
          {sorted.length === 0 && <p className="text-xs text-muted-foreground">No send times.</p>}
          {sorted.map((w) => (
            <div key={w.id} className="flex items-center gap-1.5">
              <input
                type="time"
                aria-label={`${WEEKDAY_LABELS[day]} send window start time`}
                value={timeStringFromMinutes(w.start)}
                disabled={readOnly}
                onChange={(e) => updateWindow(w.id, { start: minutesFromTimeString(e.target.value), end: w.end })}
                className="h-8 rounded-md border border-input bg-transparent px-2 text-sm disabled:cursor-not-allowed disabled:opacity-60"
              />
              <span className="text-xs text-muted-foreground">to</span>
              <input
                type="time"
                aria-label={`${WEEKDAY_LABELS[day]} send window end time`}
                value={timeStringFromMinutes(w.end)}
                disabled={readOnly}
                onChange={(e) => updateWindow(w.id, { start: w.start, end: minutesFromTimeString(e.target.value) })}
                className="h-8 rounded-md border border-input bg-transparent px-2 text-sm disabled:cursor-not-allowed disabled:opacity-60"
              />
              {!readOnly && (
                <button
                  type="button"
                  onClick={() => removeWindow(w.id)}
                  aria-label={`Remove ${WEEKDAY_LABELS[day]} send window`}
                  className="text-xs text-muted-foreground hover:text-destructive"
                >
                  Remove
                </button>
              )}
            </div>
          ))}
        </div>
      </div>

      {!readOnly && (
        <Button
          type="button"
          variant="outline"
          size="icon-sm"
          onClick={addWindow}
          disabled={!canAdd}
          title={canAdd ? "Add a send time" : "No room left on this day"}
          className="mt-0.5 shrink-0"
        >
          <Plus className="h-3.5 w-3.5" />
        </Button>
      )}
    </div>
  );
}
