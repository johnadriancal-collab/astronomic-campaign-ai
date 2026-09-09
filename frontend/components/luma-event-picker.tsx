"use client";

import { useState } from "react";
import { Search, X } from "lucide-react";
import { Input } from "@/components/ui/input";
import { ApiError, listLumaEvents, type LumaEventSummary } from "@/lib/api";
import { formatEngagementDate } from "@/lib/client-crm";

// Client CRM Stage 1H-A. Same "search-then-pick-one-from-rich-results,
// Enter-to-search" convention as CrmContactPicker -- reads ONLY the
// already-stored LumaEvent records (GET /client-crm/luma-events), never
// calls Luma's own API. Shows event name + date (+ location, when known)
// so events with similar names are still distinguishable.
export function LumaEventPicker({
  selected,
  onSelect,
  placeholder = "Search Luma events by name...",
}: {
  selected: LumaEventSummary | null;
  onSelect: (event: LumaEventSummary | null) => void;
  placeholder?: string;
}) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<LumaEventSummary[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function runSearch() {
    // Unlike CrmContactPicker, an EMPTY query still searches (lists every
    // stored event, most-recent/upcoming-first) rather than no-op'ing --
    // the Luma event count is small enough that "just show me what's
    // there" is a reasonable first interaction, not just a name lookup.
    const q = query.trim();
    setLoading(true);
    setError(null);
    try {
      setResults(await listLumaEvents(q || undefined));
    } catch (err) {
      setError(err instanceof ApiError ? `Couldn't search Luma events (${err.status}).` : "Couldn't reach the backend.");
    } finally {
      setLoading(false);
    }
  }

  if (selected) {
    return (
      <div className="flex items-center justify-between gap-2 rounded-md border border-input bg-secondary/30 px-3 py-2 text-sm">
        <div>
          <p className="font-medium">{selected.name}</p>
          <p className="text-xs text-muted-foreground">
            {formatEngagementDate(selected.start_at)}
            {selected.location_summary ? ` · ${selected.location_summary}` : ""}
          </p>
        </div>
        <button
          type="button"
          onClick={() => onSelect(null)}
          className="shrink-0 rounded p-1 text-muted-foreground hover:bg-secondary hover:text-foreground"
          aria-label="Clear selected Luma event"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      </div>
    );
  }

  return (
    <div className="space-y-2">
      <div className="relative">
        <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
        <Input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && runSearch()}
          placeholder={placeholder}
          className="pl-8"
        />
      </div>

      {error && <p className="text-xs text-destructive">{error}</p>}
      {loading && <p className="text-xs text-muted-foreground">Searching...</p>}

      {!loading && results !== null && results.length === 0 && (
        <p className="text-xs text-muted-foreground">No stored Luma events match.</p>
      )}

      {!loading && results !== null && results.length > 0 && (
        <div className="max-h-48 space-y-1 overflow-y-auto rounded-md border border-border/60 p-1">
          {results.map((event) => (
            <button
              key={event.luma_event_id}
              type="button"
              onClick={() => {
                onSelect(event);
                setResults(null);
                setQuery("");
              }}
              className="w-full rounded px-2 py-1.5 text-left text-sm hover:bg-secondary/60"
            >
              <p className="font-medium">{event.name}</p>
              <p className="text-xs text-muted-foreground">
                {formatEngagementDate(event.start_at)}
                {event.location_summary ? ` · ${event.location_summary}` : ""}
              </p>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
