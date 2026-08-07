"use client";

import { useState } from "react";
import { Plus, Trash2 } from "lucide-react";
import { buttonVariants } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { FilterCondition, FilterFieldMeta } from "@/lib/api";
import { isConditionComplete } from "@/lib/filter-conditions";
import { NO_VALUE_OPERATORS, operatorLabel, ORDINAL_OPERATORS } from "@/lib/filter-operator-labels";
import { addTagValue, removeTagValue } from "@/lib/tag-multi-select";
import { cn } from "@/lib/utils";

function valuesOf(value: unknown): string[] {
  if (value === undefined || value === null) return [];
  return Array.isArray(value) ? value.map(String) : [String(value)];
}

function groupByCategory(fields: FilterFieldMeta[]): [string, FilterFieldMeta[]][] {
  const categories = new Map<string, FilterFieldMeta[]>();
  for (const f of fields) {
    const list = categories.get(f.category) ?? [];
    list.push(f);
    categories.set(f.category, list);
  }
  return Array.from(categories.entries());
}

function TagInput({ values, onChange }: { values: string[]; onChange: (values: string[]) => void }) {
  const [draft, setDraft] = useState("");

  function commitDraft() {
    if (!draft.trim()) return;
    const next = addTagValue(values, draft);
    if (next !== values) onChange(next);
    setDraft("");
  }

  return (
    <div className="flex flex-wrap items-center gap-1.5 rounded-md border border-input px-2 py-1.5">
      {values.map((v) => (
        <span key={v} className="inline-flex items-center gap-1 rounded-full bg-secondary px-2 py-0.5 text-xs">
          {v}
          <button type="button" onClick={() => onChange(removeTagValue(values, v))} aria-label={`Remove ${v}`}>
            ×
          </button>
        </span>
      ))}
      <input
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === ",") {
            e.preventDefault();
            commitDraft();
          }
        }}
        onBlur={commitDraft}
        placeholder={values.length === 0 ? "Type a value, press Enter…" : "Add another…"}
        className="min-w-[8rem] flex-1 bg-transparent text-sm outline-none"
      />
    </div>
  );
}

function ValueControl({
  field,
  value,
  onChange,
  isOrdinal,
}: {
  field: FilterFieldMeta;
  value: unknown;
  onChange: (value: unknown) => void;
  isOrdinal: boolean;
}) {
  // Ordinal (gt/gte/lt/lte) comparisons only ever operate against a field's
  // explicitly ordered_options -- never the full option list, and never a value
  // typed in free-hand. This is the frontend half of the same rule the backend
  // enforces server-side (validate_condition in crm_filter_service.py): "Other:",
  // "Retired", "Deceased" etc. simply never appear in this dropdown.
  if (isOrdinal) {
    return (
      <div>
        <select
          value={typeof value === "string" ? value : ""}
          onChange={(e) => onChange(e.target.value)}
          className="h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm"
        >
          <option value="" disabled>
            Select a value…
          </option>
          {field.ordered_options.map((opt) => (
            <option key={opt} value={opt}>
              {opt}
            </option>
          ))}
        </select>
        <p className="mt-1 text-xs text-muted-foreground">Listed low to high -- this comparison is against that order, not alphabetically.</p>
      </div>
    );
  }

  if (field.type === "number") {
    return (
      <Input type="number" value={typeof value === "string" || typeof value === "number" ? String(value) : ""} onChange={(e) => onChange(e.target.value)} />
    );
  }

  if (field.type === "date") {
    return <Input type="date" value={typeof value === "string" ? value : ""} onChange={(e) => onChange(e.target.value)} />;
  }

  if ((field.type === "single_select" || field.type === "multi_select") && field.options.length > 0) {
    // A checkbox list, not a plain <select> -- even a single_select field's `eq`
    // operator accepts multiple values (OR-matched), which is how "State is Texas
    // or California"-style multi-value filtering works without a separate
    // is_any_of operator name.
    const selected = valuesOf(value);
    return (
      <div>
        <div className="flex max-h-40 flex-col gap-1 overflow-y-auto rounded-md border border-input p-2 text-sm">
          {field.options.map((option) => (
            <label key={option} className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={selected.includes(option)}
                onChange={() => onChange(selected.includes(option) ? selected.filter((v) => v !== option) : [...selected, option])}
                className="h-4 w-4 rounded border-input accent-primary"
              />
              <span>{option}</span>
            </label>
          ))}
        </div>
        {field.type === "single_select" && (
          <p className="mt-1 text-xs text-muted-foreground">Pick more than one to match ANY of them (e.g. this OR that).</p>
        )}
      </div>
    );
  }

  // TEXT, and open-vocabulary (no declared options) single_select/multi_select
  // fields like Technologies/Investment Industry -- a tag input so a single value
  // and a multi-value OR both go through the same control.
  return <TagInput values={valuesOf(value)} onChange={onChange} />;
}

function FilterRow({
  condition,
  fields,
  onChange,
  onRemove,
}: {
  condition: FilterCondition;
  fields: FilterFieldMeta[];
  onChange: (next: FilterCondition) => void;
  onRemove: () => void;
}) {
  const field = fields.find((f) => f.key === condition.field) ?? null;
  const categories = groupByCategory(fields);
  const noValue = NO_VALUE_OPERATORS.has(condition.operator);
  const isOrdinal = ORDINAL_OPERATORS.has(condition.operator);

  function handleFieldChange(nextKey: string) {
    const nextField = fields.find((f) => f.key === nextKey);
    // A field change resets operator/value -- an operator valid for the previous
    // field's type may not even exist for the new one (e.g. gte only appears on
    // ordered fields).
    onChange({ field: nextKey, operator: nextField?.operators[0] ?? "", value: undefined });
  }

  function handleOperatorChange(nextOperator: string) {
    // Switching to/from an ordinal operator changes the value's valid domain
    // (ordered_options vs. the full option list) -- clear rather than carry over
    // a selection that may no longer be valid.
    onChange({ field: condition.field, operator: nextOperator, value: undefined });
  }

  return (
    <div className="grid grid-cols-1 items-start gap-2 sm:grid-cols-[1fr_1fr_1.4fr_auto]">
      <select
        value={condition.field}
        onChange={(e) => handleFieldChange(e.target.value)}
        className="h-9 rounded-md border border-input bg-transparent px-3 text-sm"
      >
        <option value="" disabled>
          Select a field…
        </option>
        {categories.map(([category, categoryFields]) => (
          <optgroup key={category} label={category}>
            {categoryFields.map((f) => (
              <option key={f.key} value={f.key}>
                {f.label}
              </option>
            ))}
          </optgroup>
        ))}
      </select>

      <select
        value={condition.operator}
        onChange={(e) => handleOperatorChange(e.target.value)}
        disabled={!field}
        className="h-9 rounded-md border border-input bg-transparent px-3 text-sm disabled:opacity-50"
      >
        {!field && <option value="">--</option>}
        {field?.operators.map((op) => (
          <option key={op} value={op}>
            {operatorLabel(op)}
          </option>
        ))}
      </select>

      <div>
        {field && !noValue && (
          <ValueControl
            field={field}
            value={condition.value}
            onChange={(value) => onChange({ ...condition, value })}
            isOrdinal={isOrdinal}
          />
        )}
      </div>

      <button type="button" onClick={onRemove} aria-label="Remove filter" className={cn(buttonVariants({ size: "sm", variant: "outline" }), "shrink-0")}>
        <Trash2 className="h-4 w-4" />
      </button>
    </div>
  );
}

/**
 * The whole More Filters query builder -- field/operator/value rows, Match ALL/ANY,
 * Add/Clear/Search. Deliberately flat (no nested AND/OR groups): OR-within-one-field
 * is expressed via a row's multi-value selection instead, per the approved V1 scope.
 * The Field dropdown and every operator/value choice come entirely from `fields`
 * (GET /crm/filterable-fields) -- nothing here hardcodes a field list or an
 * option list of its own.
 */
export function FilterBuilder({
  fields,
  conditions,
  logic,
  onConditionsChange,
  onLogicChange,
  onSearch,
  onClear,
  searching,
  sortField,
  sortDirection,
  sortFieldOptions,
  onSortFieldChange,
  onSortDirectionChange,
}: {
  fields: FilterFieldMeta[];
  conditions: FilterCondition[];
  logic: "AND" | "OR";
  onConditionsChange: (conditions: FilterCondition[]) => void;
  onLogicChange: (logic: "AND" | "OR") => void;
  onSearch: () => void;
  onClear: () => void;
  searching: boolean;
  sortField: string;
  sortDirection: "asc" | "desc";
  sortFieldOptions: { key: string; label: string }[];
  onSortFieldChange: (field: string) => void;
  onSortDirectionChange: (direction: "asc" | "desc") => void;
}) {
  function addCondition() {
    onConditionsChange([...conditions, { field: "", operator: "", value: undefined }]);
  }

  function updateCondition(index: number, next: FilterCondition) {
    onConditionsChange(conditions.map((c, i) => (i === index ? next : c)));
  }

  function removeCondition(index: number) {
    onConditionsChange(conditions.filter((_, i) => i !== index));
  }

  // An incomplete row (field/operator chosen but no value, or nothing chosen at
  // all yet) must never silently narrow or broaden the query -- Search stays
  // disabled until every row is either finished or removed.
  const hasIncompleteRow = conditions.some((c) => !isConditionComplete(c));

  return (
    <div className="space-y-3 rounded-xl border border-border/60 p-4">
      <div className="flex items-center gap-4 text-sm">
        <span className="font-medium">Match</span>
        <label className="flex items-center gap-1.5">
          <input type="radio" checked={logic === "AND"} onChange={() => onLogicChange("AND")} className="accent-primary" />
          ALL conditions
        </label>
        <label className="flex items-center gap-1.5">
          <input type="radio" checked={logic === "OR"} onChange={() => onLogicChange("OR")} className="accent-primary" />
          ANY condition
        </label>
      </div>

      <div className="flex flex-wrap items-center gap-3 text-sm">
        <label className="flex items-center gap-1.5">
          <span className="font-medium">Sort by</span>
          <select
            value={sortField}
            onChange={(e) => onSortFieldChange(e.target.value)}
            className="h-9 rounded-md border border-input bg-transparent px-3 text-sm"
          >
            {sortFieldOptions.map((opt) => (
              <option key={opt.key} value={opt.key}>
                {opt.label}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-1.5">
          <span className="font-medium">Direction</span>
          <select
            value={sortDirection}
            onChange={(e) => onSortDirectionChange(e.target.value as "asc" | "desc")}
            className="h-9 rounded-md border border-input bg-transparent px-3 text-sm"
          >
            <option value="asc">Ascending</option>
            <option value="desc">Descending</option>
          </select>
        </label>
      </div>

      {conditions.length === 0 && <p className="text-sm text-muted-foreground">No filters yet -- add one below.</p>}

      <div className="space-y-2">
        {conditions.map((condition, i) => (
          <FilterRow key={i} condition={condition} fields={fields} onChange={(next) => updateCondition(i, next)} onRemove={() => removeCondition(i)} />
        ))}
      </div>

      <div className="flex flex-wrap items-center justify-between gap-3 pt-1">
        <button type="button" onClick={addCondition} className={cn(buttonVariants({ size: "sm", variant: "outline" }), "gap-1.5")}>
          <Plus className="h-4 w-4" />
          Add filter
        </button>
        <div className="flex gap-2">
          <button type="button" onClick={onClear} className={cn(buttonVariants({ size: "sm", variant: "outline" }))}>
            Clear All
          </button>
          <button
            type="button"
            onClick={onSearch}
            disabled={searching || hasIncompleteRow}
            className={cn(buttonVariants({ size: "sm" }), "disabled:cursor-not-allowed disabled:opacity-40")}
          >
            Search Contacts
          </button>
        </div>
      </div>
      {hasIncompleteRow && <p className="text-xs text-muted-foreground">Finish or remove the incomplete filter row above before searching.</p>}
    </div>
  );
}
