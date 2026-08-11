"use client";

import { useEffect, useRef, useState } from "react";
import { Download, Loader2, SendHorizontal, Sparkles } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ContactResults } from "@/components/crm-contact-results";
import {
  ApiError,
  listCrmContactExportFields,
  listCrmCustomFields,
  queryCrmContacts,
  sendAstroCommand,
  type CrmContact,
  type CrmContactExportField,
  type CrmCustomFieldDefinition,
} from "@/lib/api";
import {
  applyAstroResponse,
  composeAstroMessage,
  contextForRequest,
  INITIAL_ASTRO_CONVERSATION_STATE,
  shouldResetSelectionOnAstroResponse,
  type AstroConversationState,
} from "@/lib/astro-conversation";
import {
  clearSelection,
  isPageFullySelected,
  isPagePartiallySelected,
  selectAllMatching,
  toggleOne,
  toggleSelectAllOnPage,
} from "@/lib/contact-selection";
import { fetchAllMatchingContacts, resolveContactsForExport } from "@/lib/crm-bulk-selection";
import { buildCsv, buildExportColumns, downloadCsv, exportFilename } from "@/lib/csv-export";
import { cn } from "@/lib/utils";

interface ConversationMessage {
  role: "user" | "astro" | "error";
  text: string;
}

const EXAMPLE_PROMPTS = ["Find family offices in Texas", "Show institutional investors", "Only Austin", "How many are left?"];

// Must match the backend's hardcoded page_size in app/api/astro.py's
// astro_command() -- Astro only ever renders this many contacts per turn,
// however many actually match. "Select all N matching"/"Export all
// matching" fetch the complete matching set on demand instead of relying
// on this page size (see fetchAllMatchingContacts).
const ASTRO_PAGE_SIZE = 50;

export default function AstroSearchPage() {
  const [state, setState] = useState<AstroConversationState>(INITIAL_ASTRO_CONVERSATION_STATE);
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);

  const [selected, setSelected] = useState<Set<string>>(new Set());
  // Every contact matching the CURRENT conversational query, fetched only
  // when the user asks for "Select all N matching"/"Export all matching" --
  // Astro itself only ever returns the first ASTRO_PAGE_SIZE matches per
  // turn. Reset (along with `selected`) whenever the result set changes --
  // see shouldResetSelectionOnAstroResponse.
  const [matchingContacts, setMatchingContacts] = useState<CrmContact[] | null>(null);
  const [bulkBusy, setBulkBusy] = useState(false);
  const [exportFields, setExportFields] = useState<CrmContactExportField[] | null>(null);
  const [customFields, setCustomFields] = useState<CrmCustomFieldDefinition[] | null>(null);
  const selectAllCheckboxRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    listCrmContactExportFields().then(setExportFields).catch(() => setExportFields([]));
    listCrmCustomFields(false).then(setCustomFields).catch(() => setCustomFields([]));
  }, []);

  async function runCommand(text: string) {
    if (!text.trim() || loading) return;
    setMessages((prev) => [...prev, { role: "user", text }]);
    setInput("");
    setLoading(true);
    try {
      const response = await sendAstroCommand(text, contextForRequest(state));
      setMessages((prev) => [...prev, { role: "astro", text: composeAstroMessage(response) }]);
      setState((prev) => applyAstroResponse(prev, response));
      // A resolved search resets selection (the result set changed);
      // count_contacts and unresolved turns leave it exactly as it was.
      if (shouldResetSelectionOnAstroResponse(response)) {
        setSelected(clearSelection());
        setMatchingContacts(null);
      }
    } catch (err) {
      const errorText =
        err instanceof ApiError ? `Couldn't reach Astro (${err.status}): ${err.message}` : "Couldn't reach the backend. Is it running?";
      setMessages((prev) => [...prev, { role: "error", text: errorText }]);
    } finally {
      setLoading(false);
    }
  }

  function handleSubmit() {
    runCommand(input);
  }

  const contacts = state.contacts;
  const currentQuery = state.context?.query ?? null;
  const hasActiveFilters = Boolean(currentQuery?.filters.length);
  const total = state.total ?? 0;

  const pageIds = contacts?.map((c) => c.crm_contact_id) ?? [];
  const allSelectedOnPage = isPageFullySelected(selected, pageIds);
  const somePartiallySelectedOnPage = isPagePartiallySelected(selected, pageIds);

  useEffect(() => {
    if (selectAllCheckboxRef.current) {
      selectAllCheckboxRef.current.indeterminate = somePartiallySelectedOnPage;
    }
  }, [somePartiallySelectedOnPage]);

  async function selectAllMatchingResults() {
    if (!currentQuery || bulkBusy) return;
    setBulkBusy(true);
    try {
      const all = await fetchAllMatchingContacts(currentQuery, queryCrmContacts);
      setMatchingContacts(all);
      setSelected(selectAllMatching(all.map((c) => c.crm_contact_id)));
    } catch (err) {
      setMessages((prev) => [
        ...prev,
        {
          role: "error",
          text:
            err instanceof ApiError
              ? `Couldn't select all matching contacts (${err.status}): ${err.message}`
              : "Couldn't reach the backend.",
        },
      ]);
    } finally {
      setBulkBusy(false);
    }
  }

  // Selection can include ids beyond the rendered ASTRO_PAGE_SIZE (from
  // "Select all N matching") -- resolveContactsForExport fetches the full
  // matching set on demand only when that's actually the case.
  async function handleExportSelected() {
    if (!currentQuery || !exportFields || !customFields || selected.size === 0 || bulkBusy) return;
    setBulkBusy(true);
    try {
      const knownContacts = matchingContacts ?? contacts ?? [];
      const selectedContacts = await resolveContactsForExport(selected, knownContacts, currentQuery, queryCrmContacts);
      const columns = buildExportColumns(exportFields, customFields);
      downloadCsv(buildCsv(columns, selectedContacts), exportFilename(new Date()));
    } catch (err) {
      setMessages((prev) => [
        ...prev,
        { role: "error", text: err instanceof ApiError ? `Couldn't export contacts (${err.status}): ${err.message}` : "Couldn't reach the backend." },
      ]);
    } finally {
      setBulkBusy(false);
    }
  }

  // Exports every contact matching the current query, independent of
  // selection -- doesn't require "Select all matching" to be clicked first.
  async function handleExportAllMatching() {
    if (!currentQuery || !exportFields || !customFields || total === 0 || bulkBusy) return;
    setBulkBusy(true);
    try {
      const all = matchingContacts ?? (await fetchAllMatchingContacts(currentQuery, queryCrmContacts));
      const columns = buildExportColumns(exportFields, customFields);
      downloadCsv(buildCsv(columns, all), exportFilename(new Date()));
    } catch (err) {
      setMessages((prev) => [
        ...prev,
        { role: "error", text: err instanceof ApiError ? `Couldn't export contacts (${err.status}): ${err.message}` : "Couldn't reach the backend." },
      ]);
    } finally {
      setBulkBusy(false);
    }
  }

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-6 px-6 py-10">
      <div>
        <span className="mb-2 inline-flex items-center gap-1.5 rounded-full border border-border/60 bg-secondary/60 px-3 py-1 text-xs text-muted-foreground">
          <Sparkles className="h-3 w-3 text-primary" />
          DETERMINISTIC -- NO CLAUDE
        </span>
        <h1 className="font-serif text-2xl font-medium tracking-tight sm:text-3xl">Astro Search</h1>
        <p className="mt-2 max-w-2xl text-sm text-muted-foreground">
          Search and count your CRM contacts in plain English, then refine turn by turn. This is the CRM assistant, not the
          Apollo Campaign Builder -- read-only, no lists, no CRM writes.
        </p>
      </div>

      <div className="rounded-lg border border-border bg-card p-3 shadow-sm">
        <div className="flex flex-col gap-3">
          {messages.length === 0 && (
            <p className="px-1 py-2 text-sm text-muted-foreground">
              Try one of these, or ask your own question about your CRM contacts.
            </p>
          )}
          {messages.map((m, i) => (
            <div key={i} className={cn("flex", m.role === "user" ? "justify-end" : "justify-start")}>
              <div
                className={cn(
                  "max-w-[85%] rounded-lg px-3 py-2 text-sm",
                  m.role === "user" && "bg-primary text-primary-foreground",
                  m.role === "astro" && "bg-secondary/60 text-foreground",
                  m.role === "error" && "bg-destructive/10 text-destructive"
                )}
              >
                {m.text}
              </div>
            </div>
          ))}
          {loading && (
            <div className="flex items-center gap-2 self-start rounded-lg bg-secondary/60 px-3 py-2 text-sm text-muted-foreground">
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              Astro is thinking…
            </div>
          )}
        </div>

        <form
          onSubmit={(e) => {
            e.preventDefault();
            handleSubmit();
          }}
          className="mt-3 flex items-center gap-2"
        >
          <Input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key !== "Enter") return;
              e.preventDefault();
              handleSubmit();
            }}
            placeholder="Find family offices in Texas..."
            disabled={loading}
          />
          <Button type="submit" size="sm" disabled={!input.trim() || loading} className="gap-1.5">
            <SendHorizontal className="h-4 w-4" />
            Send
          </Button>
        </form>

        {messages.length === 0 && (
          <div className="mt-3 flex flex-wrap gap-2">
            {EXAMPLE_PROMPTS.map((example) => (
              <button
                key={example}
                type="button"
                onClick={() => setInput(example)}
                className="rounded-full border border-border/60 bg-secondary/40 px-3 py-1.5 text-xs text-muted-foreground transition-colors hover:border-primary/40 hover:text-foreground"
              >
                {example}
              </button>
            ))}
          </div>
        )}
      </div>

      {state.total !== null && (
        <div>
          <div className="mb-3 flex flex-wrap items-center justify-end gap-2">
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={handleExportSelected}
              disabled={selected.size === 0 || bulkBusy}
              className="gap-1.5"
            >
              <Download className="h-4 w-4" />
              Export selected{selected.size > 0 ? ` (${selected.size})` : ""}
            </Button>
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={handleExportAllMatching}
              disabled={total === 0 || bulkBusy}
              className="gap-1.5"
            >
              <Download className="h-4 w-4" />
              Export all matching
            </Button>
          </div>
          <ContactResults
            contacts={contacts}
            total={total}
            page={1}
            pageSize={ASTRO_PAGE_SIZE}
            error={null}
            hasActiveFilters={hasActiveFilters}
            onClearFilters={() => runCommand("Start over")}
            selected={selected}
            onToggleContact={(id) => setSelected((prev) => toggleOne(prev, id))}
            selectAllCheckboxRef={selectAllCheckboxRef}
            allSelectedOnPage={allSelectedOnPage}
            onToggleSelectPage={() => setSelected((prev) => toggleSelectAllOnPage(prev, pageIds))}
            onSelectAllMatching={selectAllMatchingResults}
            onClearSelection={() => {
              setSelected(clearSelection());
              setMatchingContacts(null);
            }}
            hidePagination
          />
        </div>
      )}
    </div>
  );
}
