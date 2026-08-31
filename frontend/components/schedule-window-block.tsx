"use client";

import { useRef } from "react";
import {
  MinuteWindow,
  clampMoveWindow,
  clampResizeEnd,
  clampResizeStart,
  formatWindowRange,
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

  const leftPct = (value.start / 1440) * 100;
  const widthPct = ((value.end - value.start) / 1440) * 100;

  return (
    <div
      className={cn(
        "group absolute top-1 bottom-1 rounded-md border border-primary/50 bg-primary/15",
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
        {formatWindowRange(value)}
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
        <button
          type="button"
          onClick={onRemove}
          title="Remove this send window"
          className="absolute -top-2 -right-2 hidden h-4 w-4 items-center justify-center rounded-full bg-destructive text-[10px] leading-none text-white group-hover:flex"
        >
          ×
        </button>
      )}
    </div>
  );
}
