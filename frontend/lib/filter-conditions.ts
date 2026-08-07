// Pure helpers for the More Filters condition-row UI -- kept separate from the page
// component so "is this row complete enough to submit" is unit-testable without
// rendering React. An incomplete row must never silently narrow/broaden the query it
// wasn't meant to be part of -- the page disables Search entirely until every row is
// either complete or removed, rather than quietly omitting it.

import type { FilterCondition } from "./api";

// Operators that take no value at all -- a row using one of these is complete as
// soon as a field and operator are chosen, regardless of any value fields is holding.
export const NO_VALUE_OPERATORS = new Set(["is_empty", "is_not_empty", "is_true", "is_false"]);

export function isConditionComplete(condition: FilterCondition): boolean {
  if (!condition.field || !condition.operator) return false;
  if (NO_VALUE_OPERATORS.has(condition.operator)) return true;
  return hasValue(condition.value);
}

export function allConditionsComplete(conditions: FilterCondition[]): boolean {
  return conditions.every(isConditionComplete);
}

function hasValue(value: unknown): boolean {
  if (value === undefined || value === null) return false;
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === "string") return value.trim().length > 0;
  return true;
}
