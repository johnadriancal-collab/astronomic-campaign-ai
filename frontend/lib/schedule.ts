// Pure helper functions for the Schedule tab's visual timeline -- all real
// math lives here (unit-tested directly, see schedule.test.ts) so the
// interactive components (schedule-window-block.tsx/schedule-day-row.tsx)
// stay thin wrappers around pointer events calling into these functions.
//
// Windows are represented here as {start, end} in MINUTES SINCE MIDNIGHT
// (0-1439) -- NOT "HH:MM" strings -- for cheap arithmetic during drag/
// resize. Conversion to/from the "HH:MM" strings the backend actually
// speaks (see lib/api.ts's MailSendWindow/MailScheduleWindowInput) happens
// only at the edges (minutesFromTimeString/timeStringFromMinutes).
//
// End-of-day convention: this app has no way to represent "24:00" (backend
// uses Python's datetime.time, max 23:59) -- a full-day window is
// start=0, end=1439 (i.e. "00:00"-"23:59"), matching the exact sentinel
// the backend's legacy all_hours flag has always used. MAX_MINUTE below is
// that ceiling, not 1440.

export const MINUTES_PER_DAY = 1440;
export const MAX_MINUTE = 1439; // 23:59 -- see module docstring above
export const SNAP_MINUTES = 15;
export const MIN_WINDOW_DURATION_MINUTES = SNAP_MINUTES;

// A window not yet saved through PUT .../schedule (created client-side by
// "+ Send time") gets a local placeholder id in this exact shape -- see
// schedule-day-row.tsx's addWindow(). The Schedule tab's save handler uses
// this same check to decide which windows' ids are real server ids worth
// sending back (to preserve identity across the edit) versus which are
// local-only and must be omitted so the backend mints a real id for them.
// One shared prefix convention, not duplicated as a string literal in two
// places.
const LOCAL_WINDOW_ID_PREFIX = "new-";

export function newLocalWindowId(day: number, counter: number): string {
  return `${LOCAL_WINDOW_ID_PREFIX}${day}-${counter}`;
}

export function isLocalWindowId(id: string): boolean {
  return id.startsWith(LOCAL_WINDOW_ID_PREFIX);
}

// Removes exactly one window (by id) from a day's local, not-yet-saved
// window list -- the single implementation both remove entry points call
// (the timeline block's × and the manual-controls "Remove" link, see
// schedule-day-row.tsx), so there's only ever one place this behavior can
// drift. Purely local-state math: never calls the backend itself -- the
// removal only reaches the server on the next explicit "Save Schedule",
// same as every other edit in this tab. A no-op (returns an equal-length,
// unchanged-content array) if `id` isn't present, and never mutates the
// input array.
export function removeWindowById<T extends { id: string }>(windows: T[], id: string): T[] {
  return windows.filter((w) => w.id !== id);
}

// --- Timeline hour marks (gridlines + labels) -------------------------
//
// One mark per hour boundary, 0 through 24 inclusive (25 marks -> 24
// hourly gridline segments) -- matches the QuickMail reference's density.
// schedule-day-row.tsx renders a gridline at every INTERIOR mark
// (index 1..23 -- the track's own border already delineates 0 and 24) and
// a label at EVERY mark including both ends, positioned by exact
// `left: (h/24)*100%` rather than flexbox space-between: with unequal-width
// labels ("12AM"/"12PM" vs. a bare "1"-"11"), space-between would drift
// each interior label away from true alignment with its gridline, more
// visibly so at 25 marks than the coarser 3-hour version this replaced.

export const TIMELINE_HOUR_MARKS: number[] = Array.from({ length: 25 }, (_, i) => i);

/** "12AM"/"12PM" anchor the day/night halves; every other hour is a bare
 * number, no repeated AM/PM -- e.g. 12AM 1 2 ... 11 12PM 1 2 ... 11 12AM. */
export function formatHourMark(hour: number): string {
  if (hour === 0 || hour === 24) return "12AM";
  if (hour === 12) return "12PM";
  return hour > 12 ? `${hour - 12}` : `${hour}`;
}

export interface MinuteWindow {
  start: number;
  end: number;
}

/** Accepts "HH:MM" or "HH:MM:SS" (the backend sends the latter). */
export function minutesFromTimeString(value: string): number {
  const [h, m] = value.split(":");
  return Number(h) * 60 + Number(m);
}

/** Always emits "HH:MM" -- what PUT .../schedule and PUT .../campaigns expect. */
export function timeStringFromMinutes(minutes: number): string {
  const clamped = clampMinute(minutes);
  const h = Math.floor(clamped / 60);
  const m = clamped % 60;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
}

export function clampMinute(minutes: number): number {
  return Math.min(Math.max(minutes, 0), MAX_MINUTE);
}

export function snapToInterval(minutes: number, interval: number = SNAP_MINUTES): number {
  return Math.round(minutes / interval) * interval;
}

/** Human-readable "8:00 AM" / "12:00 PM" -- the 23:59 end-of-day sentinel
 * reads as "12:00 AM" (midnight), not the clunky-but-technically-correct
 * "11:59 PM", since it always means "through the end of the day." */
export function formatMinutesOfDay(minutes: number): string {
  if (minutes >= MAX_MINUTE) return "12:00 AM";
  const h24 = Math.floor(minutes / 60);
  const m = minutes % 60;
  const period = h24 < 12 ? "AM" : "PM";
  const h12 = h24 % 12 === 0 ? 12 : h24 % 12;
  return `${h12}:${String(m).padStart(2, "0")} ${period}`;
}

export function formatWindowRange(window: MinuteWindow): string {
  return `${formatMinutesOfDay(window.start)} – ${formatMinutesOfDay(window.end)}`;
}

/** Touching boundaries (a.end === b.start) are NOT an overlap -- matches
 * the backend's validate_send_windows() exactly. */
export function windowsOverlap(a: MinuteWindow, b: MinuteWindow): boolean {
  return a.start < b.end && b.start < a.end;
}

/** Every (i, j) index pair, i<j, whose windows overlap -- empty means the
 * day is entirely conflict-free. Windows are compared as given (not
 * pre-sorted), so callers can map results back to their original list. */
export function findOverlappingPairs(windows: MinuteWindow[]): Array<[number, number]> {
  const pairs: Array<[number, number]> = [];
  for (let i = 0; i < windows.length; i++) {
    for (let j = i + 1; j < windows.length; j++) {
      if (windowsOverlap(windows[i], windows[j])) pairs.push([i, j]);
    }
  }
  return pairs;
}

export function isValidWindow(window: MinuteWindow): boolean {
  return (
    Number.isFinite(window.start) &&
    Number.isFinite(window.end) &&
    window.start >= 0 &&
    window.end <= MAX_MINUTE + 1 && // end may land exactly on the 1440 "midnight" boundary pre-clamp
    window.start + MIN_WINDOW_DURATION_MINUTES <= window.end
  );
}

/**
 * Clamps a RESIZE of `window`'s start edge (dragging the left handle) to:
 * snap to `SNAP_MINUTES`, never cross 0, never cross the window's own end
 * minus the minimum duration, and never cross the end of the previous
 * window on the same day (`prevEnd`, or null if this is the day's first
 * window) -- so a drag can never itself PRODUCE an overlapping state.
 */
export function clampResizeStart(proposedStart: number, window: MinuteWindow, prevEnd: number | null): number {
  const snapped = snapToInterval(proposedStart);
  const lowerBound = prevEnd ?? 0;
  const upperBound = window.end - MIN_WINDOW_DURATION_MINUTES;
  return Math.min(Math.max(snapped, lowerBound), upperBound);
}

/** Symmetric to clampResizeStart() for the right handle -- `nextStart` is
 * the start of the next window on the same day, or null if this is the
 * day's last window. */
export function clampResizeEnd(proposedEnd: number, window: MinuteWindow, nextStart: number | null): number {
  const snapped = snapToInterval(proposedEnd);
  const upperBound = nextStart ?? MAX_MINUTE;
  const lowerBound = window.start + MIN_WINDOW_DURATION_MINUTES;
  return Math.max(Math.min(snapped, upperBound), lowerBound);
}

/**
 * Clamps a whole-block MOVE (dragging the body, preserving duration) to:
 * snap to `SNAP_MINUTES`, stay within [0, MAX_MINUTE], and never cross
 * into the previous/next window on the same day. Returns the new
 * {start, end} pair (both moved by the same amount).
 */
export function clampMoveWindow(
  window: MinuteWindow,
  deltaMinutes: number,
  prevEnd: number | null,
  nextStart: number | null
): MinuteWindow {
  const duration = window.end - window.start;
  const snappedDelta = snapToInterval(deltaMinutes);
  const lowerBound = prevEnd ?? 0;
  const upperBound = (nextStart ?? MAX_MINUTE + 1) - duration;
  const newStart = Math.min(Math.max(window.start + snappedDelta, lowerBound), upperBound);
  return { start: newStart, end: newStart + duration };
}

/** The previous window's end / next window's start on the same day,
 * relative to `index` in an ALREADY-SORTED-BY-START `dayWindows` array --
 * the neighbor bounds clampResizeStart/End/clampMoveWindow need. */
export function neighborBounds(
  dayWindows: MinuteWindow[],
  index: number
): { prevEnd: number | null; nextStart: number | null } {
  return {
    prevEnd: index > 0 ? dayWindows[index - 1].end : null,
    nextStart: index < dayWindows.length - 1 ? dayWindows[index + 1].start : null,
  };
}

/**
 * Sensible default window for "+ Send time":
 *   - Adding to a day that already has windows: starts 1 hour after that
 *     day's LAST window ends, 1 hour long, clamped to the day's end. Never
 *     overlaps (see clampResizeStart/End's same clamping philosophy).
 *   - Enabling an empty day (its first window): copies the FIRST window's
 *     hours found anywhere else in the campaign, if any exist -- otherwise
 *     falls back to a plain 9-to-5.
 * Returns null only when the day already has no room left at all (its
 * last window already reaches MAX_MINUTE) -- callers should disable the
 * "+ Send time" control in that case rather than call this.
 */
export function defaultNewWindow(dayWindows: MinuteWindow[], anyWindowInCampaign: MinuteWindow | null): MinuteWindow | null {
  if (dayWindows.length === 0) {
    if (anyWindowInCampaign) return { ...anyWindowInCampaign };
    return { start: 9 * 60, end: 17 * 60 };
  }
  const sorted = [...dayWindows].sort((a, b) => a.start - b.start);
  const lastEnd = sorted[sorted.length - 1].end;
  if (lastEnd >= MAX_MINUTE) return null;
  const start = lastEnd;
  const end = Math.min(start + 60, MAX_MINUTE);
  if (end - start < MIN_WINDOW_DURATION_MINUTES) return null;
  return { start, end };
}
