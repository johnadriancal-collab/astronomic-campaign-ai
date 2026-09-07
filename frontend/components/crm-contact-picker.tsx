"use client";

import { useState } from "react";
import { Search, X } from "lucide-react";
import { Input } from "@/components/ui/input";
import { ApiError, listCrmContacts, type CrmContact } from "@/lib/api";
import { formatContactName, formatContactTitleCompany } from "@/lib/contact-results-view";

// The first search-then-pick-one-from-rich-results component in this
// frontend (see Client CRM Stage 1D's own investigation -- no combobox/
// typeahead precedent existed before this). Deliberately Enter-to-search,
// matching this codebase's own established convention (no live-as-you-
// type debounce exists anywhere) rather than inventing a new interaction
// pattern for just this one picker.
export function CrmContactPicker({
  selected,
  onSelect,
  placeholder = "Search Contacts by name, email or company...",
}: {
  selected: CrmContact | null;
  onSelect: (contact: CrmContact | null) => void;
  placeholder?: string;
}) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<CrmContact[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function runSearch() {
    const q = query.trim();
    if (!q) {
      setResults(null);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const page = await listCrmContacts({ q, page_size: 8 });
      setResults(page.items);
    } catch (err) {
      setError(err instanceof ApiError ? `Couldn't search Contacts (${err.status}).` : "Couldn't reach the backend.");
    } finally {
      setLoading(false);
    }
  }

  if (selected) {
    return (
      <div className="flex items-center justify-between gap-2 rounded-md border border-input bg-secondary/30 px-3 py-2 text-sm">
        <div>
          <p className="font-medium">{formatContactName(selected)}</p>
          <p className="text-xs text-muted-foreground">{formatContactTitleCompany(selected)}</p>
        </div>
        <button
          type="button"
          onClick={() => onSelect(null)}
          className="shrink-0 rounded p-1 text-muted-foreground hover:bg-secondary hover:text-foreground"
          aria-label="Clear selected Contact"
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
        <p className="text-xs text-muted-foreground">No Contacts match &quot;{query.trim()}&quot;.</p>
      )}

      {!loading && results !== null && results.length > 0 && (
        <div className="max-h-48 space-y-1 overflow-y-auto rounded-md border border-border/60 p-1">
          {results.map((contact) => (
            <button
              key={contact.crm_contact_id}
              type="button"
              onClick={() => {
                onSelect(contact);
                setResults(null);
                setQuery("");
              }}
              className="w-full rounded px-2 py-1.5 text-left text-sm hover:bg-secondary/60"
            >
              <p className="font-medium">{formatContactName(contact)}</p>
              <p className="text-xs text-muted-foreground">{formatContactTitleCompany(contact)}</p>
              {contact.email && <p className="text-xs text-muted-foreground">{contact.email}</p>}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
