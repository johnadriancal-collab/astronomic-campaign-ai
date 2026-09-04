"use client";

import { useRef } from "react";
import {
  MinuteWindow,
  clampMoveWindow,
  clampResizeEnd,
  clampResizeStart,
  formatWindowRange,
  minutesToTimelinePercent,
} from "@/lib/schedule";
import { cn } from "@/lib/utils";

// The visual, direct-manipulation half of one send window -- purely a
// positioned block inside its day's timeline track (see
// schedule-day-row.tsx for the accessible/keyboard-operable manual time
// inputs every window ALSO gets, always visible alongside this, never
// hidden behind dragging). Pointer capture (setPointerCapture on the
// element that received pointerdown) means this needs no document-level
// listener wiring -- the browser keeps routing pointermove/pointerup to
// whichever handle/body started the drag, even once the cursor leaves it.
//
// `prop name is "value" not "window"` -- avoids shadowing the global
// `window` object throughout this file.
export function ScheduleWindowBlock({
  value,
  prevEnd,
  nextStart,
  trackRef,
  onChange,
  onRemove,
  readOnly,
}: {
  value: MinuteWindow;
  prevEnd: number | null;
  nextStart: number | null;
  trackRef: React.RefObject<HTMLDivElement | null>;
  onChange: (next: MinuteWindow) => void;
  onRemove: () => void;
  readOnly: boolean;
}) {
  const label = formatWindowRange(value);
  const dragRef = useRef<{
    mode: "move" | "resize-start" | "resize-end";
    startClientX: number;
    startValue: MinuteWindow;
  } | null>(null);

  function trackWidth(): number {
    return trackRef.current?.getBoundingClientRect().width ?? 1;
  }

  function beginDrag(e: React.PointerEvent, mode: "move" | "resize-start" | "resize-end") {
    if (readOnly) return;
    (e.currentTarget as Element).setPointerCapture(e.pointerId);
    dragRef.current = { mode, startClientX: e.clientX, startValue: value };
    e.stopPropagation();
  }

  function continueDrag(e: React.PointerEvent) {
    const drag = dragRef.current;
    if (!drag) return;
    const deltaMinutes = ((e.clientX - drag.startClientX) / trackWidth()) * 1440;

    if (drag.mode === "move") {
      onChange(clampMoveWindow(drag.startValue, deltaMinutes, prevEnd, nextStart));
    } else if (drag.mode === "resize-start") {
      onChange({
        start: clampResizeStart(drag.startValue.start + deltaMinutes, drag.startValue, prevEnd),
        end: drag.startValue.end,
      });
    } else {
      onChange({
        start: drag.startValue.start,
        end: clampResizeEnd(drag.startValue.end + deltaMinutes, drag.startValue, nextStart),
      });
    }
  }

  function endDrag() {
    dragRef.current = null;
  }

  const leftPct = minutesToTimelinePercent(value.start);
  const widthPct = minutesToTimelinePercent(value.end - value.start);

  return (
    <div
      className={cn(
        // hover:z-10/focus-within:z-10 -- back-to-back windows (e.g.
        // Monday 8-12 immediately followed by 12-6) are an expected,
        // common configuration, and the x button below deliberately
        // overflows slightly past this block's own right edge (see that
        // button's own comment). Without a z-index bump, that overflowing
        // sliver sits UNDER the next window's block in paint order (later
        // sibling wins hit-testing by default), so part of ITS resize
        // handle would win clicks meant for THIS block's x -- confirmed by
        // directly sampling elementFromPoint() across the button's hit
        // box. Bumping stacking only while this block is actually being
        // interacted with (hover or keyboard focus within it) fixes that
        // precisely, without permanently reordering anything.
        "group absolute top-1 bottom-1 rounded-md border border-primary/50 bg-primary/15 hover:z-10 focus-within:z-10",
        !readOnly && "cursor-grab touch-none active:cursor-grabbing"
      )}
      style={{ left: `${leftPct}%`, width: `${widthPct}%` }}
      onPointerDown={(e) => beginDrag(e, "move")}
      onPointerMove={continueDrag}
      onPointerUp={endDrag}
      onPointerCancel={endDrag}
    >
      {!readOnly && (
        <div
          className="absolute inset-y-0 left-0 w-2 cursor-ew-resize touch-none"
          onPointerDown={(e) => beginDrag(e, "resize-start")}
          onPointerMove={continueDrag}
          onPointerUp={endDrag}
          onPointerCancel={endDrag}
        />
      )}

      <div className="pointer-events-none flex h-full items-center justify-center overflow-hidden px-2 text-[11px] font-medium whitespace-nowrap text-primary">
        {label}
      </div>

      {!readOnly && (
        <div
          className="absolute inset-y-0 right-0 w-2 cursor-ew-resize touch-none"
          onPointerDown={(e) => beginDrag(e, "resize-end")}
          onPointerMove={continueDrag}
          onPointerUp={endDrag}
          onPointerCancel={endDrag}
        />
      )}

      {!readOnly && (
        // A real, always-focusable button (not display:none -- see the
        // opacity-based reveal below) so keyboard users can Tab to it even
        // though it's only visually hinted at on hover/focus, matching the
        // "Remove" link under the timeline's own always-visible standard.
        // The critical bit is onPointerDown's own stopPropagation: without
        // it, a pointerdown here bubbles to the BLOCK's onPointerDown
        // (mode="move"), which calls setPointerCapture on the block itself
        // -- pointer capture then retargets the follow-up pointerup/click
        // away from this button entirely, so onClick silently never fired.
        // Stopping propagation here (before the block's own handler runs)
        // is the actual fix; the onClick-level stopPropagation is defense
        // in depth, not the mechanism that made clicks fail.
        <button
          type="button"
          onPointerDown={(e) => e.stopPropagation()}
          onClick={(e) => {
            e.stopPropagation();
            onRemove();
          }}
          aria-label={`Remove ${label} send time`}
          title={`Remove ${label} send time`}
          className="absolute -top-2 -right-2 flex h-5 w-5 items-center justify-center rounded-full bg-destructive text-[10px] leading-none text-white opacity-0 transition-opacity group-hover:opacity-100 focus-visible:opacity-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-destructive"
        >
          <span aria-hidden="true">×</span>
        </button>
      )}
    </div>
  );
}
