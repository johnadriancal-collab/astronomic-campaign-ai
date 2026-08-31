import assert from "node:assert/strict";
import { test } from "node:test";
import {
  MAX_MINUTE,
  TIMELINE_HOUR_MARKS,
  clampMinute,
  clampMoveWindow,
  clampResizeEnd,
  clampResizeStart,
  defaultNewWindow,
  findOverlappingPairs,
  formatHourMark,
  formatMinutesOfDay,
  formatWindowRange,
  isLocalWindowId,
  isValidWindow,
  minutesFromTimeString,
  neighborBounds,
  newLocalWindowId,
  snapToInterval,
  timeStringFromMinutes,
  windowsOverlap,
} from "./schedule.ts";

// --- time <-> minutes conversion -------------------------------------------

test("minutesFromTimeString parses HH:MM", () => {
  assert.equal(minutesFromTimeString("00:00"), 0);
  assert.equal(minutesFromTimeString("08:30"), 510);
  assert.equal(minutesFromTimeString("23:59"), 1439);
});

test("minutesFromTimeString parses HH:MM:SS (the backend's actual format)", () => {
  assert.equal(minutesFromTimeString("09:15:00"), 555);
});

test("timeStringFromMinutes formats back to zero-padded HH:MM", () => {
  assert.equal(timeStringFromMinutes(0), "00:00");
  assert.equal(timeStringFromMinutes(510), "08:30");
  assert.equal(timeStringFromMinutes(1439), "23:59");
});

test("timeStringFromMinutes and minutesFromTimeString round-trip", () => {
  for (const m of [0, 15, 60, 540, 1000, 1439]) {
    assert.equal(minutesFromTimeString(timeStringFromMinutes(m)), m);
  }
});

test("clampMinute clamps to [0, MAX_MINUTE] -- there is no 1440/24:00", () => {
  assert.equal(clampMinute(-5), 0);
  assert.equal(clampMinute(1500), MAX_MINUTE);
  assert.equal(clampMinute(700), 700);
});

// --- snapping ---------------------------------------------------------------

test("snapToInterval rounds to the nearest 15 minutes by default", () => {
  assert.equal(snapToInterval(7), 0);
  assert.equal(snapToInterval(8), 15);
  assert.equal(snapToInterval(22), 15);
  assert.equal(snapToInterval(23), 30);
});

test("snapToInterval supports a custom interval", () => {
  assert.equal(snapToInterval(40, 30), 30);
  assert.equal(snapToInterval(44, 30), 30);
  assert.equal(snapToInterval(46, 30), 60);
});

// --- display formatting -----------------------------------------------------

test("formatMinutesOfDay renders 12-hour clock labels", () => {
  assert.equal(formatMinutesOfDay(0), "12:00 AM");
  assert.equal(formatMinutesOfDay(510), "8:30 AM");
  assert.equal(formatMinutesOfDay(720), "12:00 PM");
  assert.equal(formatMinutesOfDay(13 * 60), "1:00 PM");
});

test("formatMinutesOfDay renders the 23:59 end-of-day sentinel as midnight, not 11:59 PM", () => {
  assert.equal(formatMinutesOfDay(MAX_MINUTE), "12:00 AM");
});

test("formatWindowRange joins start and end with an en dash", () => {
  assert.equal(formatWindowRange({ start: 480, end: 720 }), "8:00 AM – 12:00 PM");
});

// --- overlap detection --------------------------------------------------

test("windowsOverlap is true for genuinely overlapping ranges", () => {
  assert.equal(windowsOverlap({ start: 480, end: 780 }, { start: 720, end: 1080 }), true);
});

test("windowsOverlap is false for back-to-back touching windows", () => {
  assert.equal(windowsOverlap({ start: 480, end: 720 }, { start: 720, end: 1080 }), false);
});

test("windowsOverlap is false for windows with a gap", () => {
  assert.equal(windowsOverlap({ start: 480, end: 600 }, { start: 700, end: 800 }), false);
});

test("windowsOverlap is symmetric", () => {
  const a = { start: 480, end: 780 };
  const b = { start: 720, end: 1080 };
  assert.equal(windowsOverlap(a, b), windowsOverlap(b, a));
});

test("findOverlappingPairs finds every conflicting index pair, not just the first", () => {
  const windows = [
    { start: 480, end: 600 }, // 0: 8-10
    { start: 540, end: 660 }, // 1: 9-11, overlaps 0
    { start: 900, end: 960 }, // 2: 15-16, no conflict
    { start: 930, end: 990 }, // 3: 15:30-16:30, overlaps 2
  ];
  const pairs = findOverlappingPairs(windows);
  assert.deepEqual(pairs, [
    [0, 1],
    [2, 3],
  ]);
});

test("findOverlappingPairs is empty for a fully non-overlapping day", () => {
  const windows = [
    { start: 480, end: 720 },
    { start: 720, end: 1080 },
  ];
  assert.deepEqual(findOverlappingPairs(windows), []);
});

// --- structural validity -----------------------------------------------

test("isValidWindow rejects zero-duration and negative-duration windows", () => {
  assert.equal(isValidWindow({ start: 540, end: 540 }), false);
  assert.equal(isValidWindow({ start: 600, end: 540 }), false);
});

test("isValidWindow accepts a window at the minimum duration", () => {
  assert.equal(isValidWindow({ start: 540, end: 555 }), true);
});

test("isValidWindow accepts the full-day boundary window", () => {
  assert.equal(isValidWindow({ start: 0, end: MAX_MINUTE }), true);
});

// --- resize clamping (drag left/right edge) ---------------------------

test("clampResizeStart snaps to 15 minutes", () => {
  const w = { start: 480, end: 720 };
  assert.equal(clampResizeStart(487, w, null), 480);
  assert.equal(clampResizeStart(493, w, null), 495);
});

test("clampResizeStart never crosses below 0", () => {
  assert.equal(clampResizeStart(-100, { start: 60, end: 200 }, null), 0);
});

test("clampResizeStart never crosses the window's own end minus minimum duration", () => {
  const w = { start: 480, end: 500 };
  assert.equal(clampResizeStart(495, w, null), 485); // 500 - 15
});

test("clampResizeStart never crosses the previous window's end (no overlap producible)", () => {
  const w = { start: 600, end: 720 };
  assert.equal(clampResizeStart(500, w, 540), 540);
});

test("clampResizeEnd never crosses the next window's start (no overlap producible)", () => {
  const w = { start: 480, end: 600 };
  assert.equal(clampResizeEnd(700, w, 660), 660);
});

test("clampResizeEnd never crosses past MAX_MINUTE when there's no next window", () => {
  assert.equal(clampResizeEnd(1500, { start: 480, end: 600 }, null), MAX_MINUTE);
});

test("clampResizeEnd never crosses below the window's own start plus minimum duration", () => {
  const w = { start: 480, end: 500 };
  assert.equal(clampResizeEnd(485, w, null), 495); // 480 + 15
});

// --- whole-block move clamping -----------------------------------------

test("clampMoveWindow preserves duration while shifting both edges", () => {
  const result = clampMoveWindow({ start: 480, end: 600 }, 60, null, null);
  assert.equal(result.end - result.start, 120);
  assert.equal(result.start, 540);
  assert.equal(result.end, 660);
});

test("clampMoveWindow snaps the delta to 15 minutes", () => {
  const result = clampMoveWindow({ start: 480, end: 600 }, 22, null, null);
  assert.equal(result.start, 480 + 15);
});

test("clampMoveWindow never drags a window before the previous window's end", () => {
  const result = clampMoveWindow({ start: 600, end: 720 }, -200, 540, null);
  assert.equal(result.start, 540);
  assert.equal(result.end, 660);
});

test("clampMoveWindow never drags a window past the next window's start", () => {
  const result = clampMoveWindow({ start: 480, end: 600 }, 500, null, 720);
  assert.equal(result.end, 720);
  assert.equal(result.start, 600);
});

test("clampMoveWindow never drags a window past MAX_MINUTE with no next window", () => {
  const result = clampMoveWindow({ start: 1380, end: 1439 }, 200, null, null);
  assert.equal(result.end, MAX_MINUTE + 1); // duration preserved against the ceiling
});

// --- neighbor bounds -----------------------------------------------------

test("neighborBounds returns null prevEnd for the first window and null nextStart for the last", () => {
  const dayWindows = [
    { start: 480, end: 600 },
    { start: 660, end: 780 },
    { start: 900, end: 960 },
  ];
  assert.deepEqual(neighborBounds(dayWindows, 0), { prevEnd: null, nextStart: 660 });
  assert.deepEqual(neighborBounds(dayWindows, 1), { prevEnd: 600, nextStart: 900 });
  assert.deepEqual(neighborBounds(dayWindows, 2), { prevEnd: 780, nextStart: null });
});

// --- default window for "+ Send time" -----------------------------------

test("defaultNewWindow on an empty day with no other windows anywhere falls back to 9-to-5", () => {
  assert.deepEqual(defaultNewWindow([], null), { start: 540, end: 1020 });
});

test("defaultNewWindow on an empty day copies another day's existing hours", () => {
  const existing = { start: 480, end: 720 };
  assert.deepEqual(defaultNewWindow([], existing), existing);
});

test("defaultNewWindow on a day with an existing window starts right after it ends", () => {
  const dayWindows = [{ start: 480, end: 600 }];
  assert.deepEqual(defaultNewWindow(dayWindows, null), { start: 600, end: 660 });
});

test("defaultNewWindow clamps the new window's end to MAX_MINUTE near the end of the day", () => {
  const dayWindows = [{ start: 1350, end: 1400 }];
  const result = defaultNewWindow(dayWindows, null);
  assert.ok(result);
  assert.equal(result!.start, 1400);
  assert.equal(result!.end, MAX_MINUTE);
});

test("defaultNewWindow returns null when the day has no room left at all", () => {
  const dayWindows = [{ start: 0, end: MAX_MINUTE }];
  assert.equal(defaultNewWindow(dayWindows, null), null);
});

// --- local vs. server window id ---------------------------------------

test("newLocalWindowId always produces an id isLocalWindowId recognizes", () => {
  assert.equal(isLocalWindowId(newLocalWindowId(0, 0)), true);
  assert.equal(isLocalWindowId(newLocalWindowId(6, 42)), true);
});

test("isLocalWindowId is false for a real server-issued id (a UUID)", () => {
  assert.equal(isLocalWindowId("3f6b8f2a-1c2d-4e5f-9a8b-7c6d5e4f3a2b"), false);
});

test("newLocalWindowId produces distinct ids for distinct counters", () => {
  assert.notEqual(newLocalWindowId(0, 0), newLocalWindowId(0, 1));
});

// --- timeline hour marks (hourly gridlines/labels) -----------------------

test("TIMELINE_HOUR_MARKS covers every hour boundary, midnight through the closing midnight", () => {
  assert.deepEqual(TIMELINE_HOUR_MARKS, Array.from({ length: 25 }, (_, i) => i));
  assert.equal(TIMELINE_HOUR_MARKS.length, 25); // 24 hourly gridline segments, 25 boundary marks
  assert.equal(TIMELINE_HOUR_MARKS[0], 0);
  assert.equal(TIMELINE_HOUR_MARKS[TIMELINE_HOUR_MARKS.length - 1], 24);
});

test("formatHourMark labels midnight and noon as 12AM/12PM", () => {
  assert.equal(formatHourMark(0), "12AM");
  assert.equal(formatHourMark(12), "12PM");
});

test("formatHourMark labels the closing boundary (hour 24) as 12AM too", () => {
  assert.equal(formatHourMark(24), "12AM");
});

test("formatHourMark renders morning hours as a bare number, no AM suffix", () => {
  assert.equal(formatHourMark(1), "1");
  assert.equal(formatHourMark(9), "9");
  assert.equal(formatHourMark(11), "11");
});

test("formatHourMark renders afternoon/evening hours as a bare 1-11, no PM suffix or 24h number", () => {
  assert.equal(formatHourMark(13), "1");
  assert.equal(formatHourMark(17), "5");
  assert.equal(formatHourMark(23), "11");
});

test("formatHourMark never repeats AM/PM on interior hours -- every mark from TIMELINE_HOUR_MARKS is a clean, short label", () => {
  const labels = TIMELINE_HOUR_MARKS.map(formatHourMark);
  assert.deepEqual(labels, [
    "12AM", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11",
    "12PM", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11",
    "12AM",
  ]);
});
