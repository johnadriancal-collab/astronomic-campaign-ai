// Human-readable labels for the fixed, backend-defined operator vocabulary. This is
// NOT a second source of truth for which operators a field supports -- that always
// comes from FilterFieldMeta.operators (the registry). This module only translates a
// known operator key into display text, the same way a label is just display text for
// a field key.

export const OPERATOR_LABELS: Record<string, string> = {
  eq: "is",
  neq: "is not",
  contains: "contains",
  not_contains: "does not contain",
  is_empty: "is empty",
  is_not_empty: "is not empty",
  gt: "greater than",
  gte: "greater than or equal to",
  lt: "less than",
  lte: "less than or equal to",
  contains_any: "contains any of",
  contains_all: "contains all of",
  is_true: "is true",
  is_false: "is false",
  before: "before",
  after: "after",
  on_or_before: "on or before",
  on_or_after: "on or after",
};

export function operatorLabel(operator: string): string {
  return OPERATOR_LABELS[operator] ?? operator;
}

// NO_VALUE_OPERATORS lives in filter-conditions.ts (it's a completeness-check
// concern, not a labeling one) -- re-exported here too since both are commonly
// needed together when rendering a filter row's value control.
export { NO_VALUE_OPERATORS } from "./filter-conditions";

export const ORDINAL_OPERATORS = new Set(["gt", "gte", "lt", "lte"]);
