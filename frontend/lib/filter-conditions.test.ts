import assert from "node:assert/strict";
import { test } from "node:test";
import { allConditionsComplete, isConditionComplete } from "./filter-conditions.ts";

test("a row with field, operator, and value is complete", () => {
  assert.equal(isConditionComplete({ field: "state", operator: "eq", value: "Texas" }), true);
});

test("a row missing a field is incomplete", () => {
  assert.equal(isConditionComplete({ field: "", operator: "eq", value: "Texas" }), false);
});

test("a row missing an operator is incomplete", () => {
  assert.equal(isConditionComplete({ field: "state", operator: "", value: "Texas" }), false);
});

test("a row missing a value is incomplete for a value-requiring operator", () => {
  assert.equal(isConditionComplete({ field: "state", operator: "eq", value: undefined }), false);
  assert.equal(isConditionComplete({ field: "state", operator: "eq", value: "" }), false);
  assert.equal(isConditionComplete({ field: "state", operator: "eq", value: [] }), false);
});

test("is_empty/is_not_empty/is_true/is_false are complete with no value at all", () => {
  assert.equal(isConditionComplete({ field: "city", operator: "is_empty" }), true);
  assert.equal(isConditionComplete({ field: "city", operator: "is_not_empty" }), true);
  assert.equal(isConditionComplete({ field: "do_not_call", operator: "is_true" }), true);
  assert.equal(isConditionComplete({ field: "do_not_call", operator: "is_false" }), true);
});

test("a non-empty array or number value is complete", () => {
  assert.equal(isConditionComplete({ field: "state", operator: "eq", value: ["Texas"] }), true);
  assert.equal(isConditionComplete({ field: "custom:total_funding", operator: "gt", value: 0 }), true);
});

test("allConditionsComplete is true only when every row is complete", () => {
  const complete = { field: "state", operator: "eq", value: "Texas" };
  const incomplete = { field: "city", operator: "", value: "" };
  assert.equal(allConditionsComplete([complete]), true);
  assert.equal(allConditionsComplete([complete, incomplete]), false);
  assert.equal(allConditionsComplete([]), true); // no rows at all -- vacuously fine, "search everything"
});
