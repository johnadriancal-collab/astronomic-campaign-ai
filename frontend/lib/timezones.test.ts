import { test } from "node:test";
import assert from "node:assert/strict";
import { DEFAULT_TIMEZONE, TIMEZONE_OPTIONS, timezoneLabel, timezoneOptionsIncluding } from "./timezones.ts";

test("DEFAULT_TIMEZONE is a real entry in the curated list", () => {
  assert.ok(TIMEZONE_OPTIONS.some((tz) => tz.value === DEFAULT_TIMEZONE));
});

test("the curated list is not limited to US zones", () => {
  const values = TIMEZONE_OPTIONS.map((tz) => tz.value);
  assert.ok(values.includes("Asia/Manila"));
  assert.ok(values.includes("Africa/Lagos"));
  assert.ok(values.some((v) => v.startsWith("America/")));
});

test("timezoneLabel returns the human-readable label for a known zone", () => {
  assert.equal(timezoneLabel("America/Chicago"), "Central Time (US & Canada)");
});

test("timezoneLabel falls back to the raw value for an unknown zone", () => {
  assert.equal(timezoneLabel("Antarctica/McMurdo"), "Antarctica/McMurdo");
});

test("timezoneOptionsIncluding returns the curated list unchanged for a known value", () => {
  const options = timezoneOptionsIncluding("America/Chicago");
  assert.equal(options.length, TIMEZONE_OPTIONS.length);
});

test("timezoneOptionsIncluding returns the curated list unchanged for null", () => {
  const options = timezoneOptionsIncluding(null);
  assert.equal(options.length, TIMEZONE_OPTIONS.length);
});

test("timezoneOptionsIncluding prepends an unknown stored value so it always renders", () => {
  const options = timezoneOptionsIncluding("Antarctica/McMurdo");
  assert.equal(options.length, TIMEZONE_OPTIONS.length + 1);
  assert.deepEqual(options[0], { value: "Antarctica/McMurdo", label: "Antarctica/McMurdo" });
});
