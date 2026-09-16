"use client";

import { useEffect, useId, useRef, useState } from "react";
import {
  clampHighlightIndex,
  filterOptions,
  nextHighlightIndex,
  previousHighlightIndex,
  removeValue,
  selectOption,
} from "@/lib/searchable-multi-select";

// A searchable, approved-options-only multi-select -- the taxonomy-enforcing
// counterpart to lib/tag-multi-select.ts's free-text TagMultiSelect (see
// app/crm/[id]/page.tsx). Deliberately generic: takes `options` from the
// caller rather than embedding any specific taxonomy, so it can be reused
// anywhere a "pick many, from a fixed list, nothing invented" field is
// needed -- Investment Industry is its first caller, not its only intended
// one. Selected chips render EVERY value in `values`, including a value
// that isn't in `options` (a legacy/pre-existing value) -- see
// lib/searchable-multi-select.ts's selectOption/removeValue for why such a
// value can be removed like any other but never re-added through search.
export function SearchableMultiSelect({
  values,
  options,
  onChange,
  label,
  placeholder = "Search...",
  noMatchesLabel = "No matching options",
}: {
  values: string[];
  options: readonly string[];
  onChange: (values: string[]) => void;
  label?: string;
  placeholder?: string;
  noMatchesLabel?: string;
}) {
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [highlightIndex, setHighlightIndex] = useState(-1);
  const rootRef = useRef<HTMLDivElement>(null);
  const baseId = useId();
  const listboxId = `${baseId}-listbox`;

  const filtered = filterOptions(options, query, values);

  // Outside click closes the dropdown -- the setState calls here happen
  // inside the (asynchronous, browser-driven) mousedown callback, not
  // synchronously in the effect body itself, so this doesn't hit the same
  // "setState in effect" concern as resetting highlightIndex would.
  useEffect(() => {
    if (!open) return;
    function handlePointerDown(event: MouseEvent) {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) {
        closeDropdown();
      }
    }
    document.addEventListener("mousedown", handlePointerDown);
    return () => document.removeEventListener("mousedown", handlePointerDown);
  }, [open]);

  // Every place the visible option list can change shape (typing, opening,
  // or a selection removing an option from it) recomputes highlightIndex
  // synchronously, right there in the same handler -- deliberately NOT done
  // via a useEffect keyed on the filtered list, which would call setState
  // during render's commit phase for no benefit here (every trigger is
  // already a discrete user event, not an external system to synchronize
  // with).
  function openDropdown(nextQuery: string) {
    setOpen(true);
    setHighlightIndex(clampHighlightIndex(0, filterOptions(options, nextQuery, values).length));
  }

  function closeDropdown() {
    setOpen(false);
    setQuery("");
    setHighlightIndex(-1);
  }

  function commitSelection(option: string) {
    const nextValues = selectOption(values, option, options);
    onChange(nextValues);
    setQuery("");
    setHighlightIndex(clampHighlightIndex(0, filterOptions(options, "", nextValues).length));
  }

  function commitHighlighted() {
    if (highlightIndex < 0 || highlightIndex >= filtered.length) return;
    commitSelection(filtered[highlightIndex]);
  }

  function handleRemove(value: string) {
    onChange(removeValue(values, value));
  }

  return (
    <div ref={rootRef} className="space-y-1.5">
      {label && <p className="text-xs font-medium text-muted-foreground">{label}</p>}

      {values.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {values.map((value) => (
            <span
              key={value}
              className="inline-flex items-center gap-1.5 rounded-full border border-input bg-muted px-2.5 py-0.5 text-xs"
            >
              {value}
              <button
                type="button"
                aria-label={`Remove ${value}`}
                className="text-muted-foreground hover:text-foreground"
                onClick={() => handleRemove(value)}
              >
                ×
              </button>
            </span>
          ))}
        </div>
      )}

      <div className="relative">
        <input
          role="combobox"
          type="text"
          aria-expanded={open}
          aria-controls={listboxId}
          aria-autocomplete="list"
          aria-activedescendant={open && highlightIndex >= 0 ? `${listboxId}-option-${highlightIndex}` : undefined}
          value={query}
          onFocus={() => openDropdown(query)}
          onClick={() => openDropdown(query)}
          onChange={(e) => {
            const next = e.target.value;
            setQuery(next);
            openDropdown(next);
          }}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") {
              e.preventDefault();
              setOpen(true);
              setHighlightIndex((i) => nextHighlightIndex(i, filtered.length));
            } else if (e.key === "ArrowUp") {
              e.preventDefault();
              setOpen(true);
              setHighlightIndex((i) => previousHighlightIndex(i, filtered.length));
            } else if (e.key === "Enter") {
              e.preventDefault();
              commitHighlighted();
            } else if (e.key === "Escape") {
              e.preventDefault();
              closeDropdown();
            }
          }}
          placeholder={placeholder}
          className="h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm"
        />

        {open && (
          <ul
            id={listboxId}
            role="listbox"
            className="absolute z-10 mt-1 max-h-56 w-full overflow-y-auto rounded-md border border-border/60 bg-popover p-1 shadow-md"
          >
            {filtered.length === 0 ? (
              <li className="px-2 py-1.5 text-sm text-muted-foreground">{noMatchesLabel}</li>
            ) : (
              filtered.map((option, index) => (
                <li
                  key={option}
                  id={`${listboxId}-option-${index}`}
                  role="option"
                  aria-selected={index === highlightIndex}
                  onMouseEnter={() => setHighlightIndex(index)}
                  onMouseDown={(e) => {
                    // Prevent the input from blurring (which would close the
                    // dropdown via the outside-click handler) before the click
                    // is processed.
                    e.preventDefault();
                  }}
                  onClick={() => commitSelection(option)}
                  className={`cursor-pointer rounded px-2 py-1.5 text-sm ${
                    index === highlightIndex ? "bg-secondary/60 text-foreground" : ""
                  }`}
                >
                  {option}
                </li>
              ))
            )}
          </ul>
        )}
      </div>
    </div>
  );
}
