"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { AlertTriangle, ChevronLeft, ChevronRight, Download, Plus, Search, Users, X } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import {
  ApiError,
  listCrmContactExportFields,
  listCrmContacts,
  listCrmCustomFields,
  type CrmContact,
  type CrmContactExportField,
  type CrmCustomFieldDefinition,
} from "@/lib/api";
import {
  clearSelection,
  isPageFullySelected,
  isPagePartiallySelected,
  selectAllMatching,
  toggleOne,
  toggleSelectAllOnPage,
} from "@/lib/contact-selection";
import { buildCsv, buildExportColumns, downloadCsv, exportFilename } from "@/lib/csv-export";
import { compareContactsByName } from "@/lib/sort-contacts";
import { cn } from "@/lib/utils";

const PAGE_SIZE_OPTIONS = [25, 50, 100];

interface Filters {
  q: string;
  city: string;
  investorMode: string;
}

export default function CrmContactsPage() {
  // Holds EVERY contact matching the current search/filters, already
  // sorted A-Z -- not just one page. Pagination below is a pure client-side
  // slice of this array, which is what makes "page 1 is the true first
  // alphabetical contacts" possible: the backend has no sort of its own,
  // and it slices to one page before any sort could apply, so sorting has
  // to happen here, across the whole filtered set, before slicing.
  const [allContacts, setAllContacts] = useState<CrmContact[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [city, setCity] = useState("");
  const [investorMode, setInvestorMode] = useState("");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);

  // Selection is a plain Set<string> of crm_contact_id -- deliberately cleared
  // inside load() (below), never inside goToPage/changePageSize, so it survives
  // paging through the current result set but never carries over stale ids once
  // the search/filter criteria (and therefore the result set) actually changes.
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [exportFields, setExportFields] = useState<CrmContactExportField[] | null>(null);
  const [customFields, setCustomFields] = useState<CrmCustomFieldDefinition[] | null>(null);
  const selectAllCheckboxRef = useRef<HTMLInputElement>(null);

  async function load(filters: Filters) {
    try {
      const params = {
        q: filters.q || undefined,
        city: filters.city || undefined,
        investor_mode: filters.investorMode || undefined,
      };
      // Two-step fetch, no hard-coded ceiling: first ask for just the
      // count (page_size: 1 keeps that call cheap), then re-fetch with
      // page_size set to the EXACT total so every matching contact comes
      // back in one page, however large the CRM grows.
      const probe = await listCrmContacts({ ...params, page: 1, page_size: 1 });
      const data =
        probe.total > probe.items.length
          ? await listCrmContacts({ ...params, page: 1, page_size: probe.total })
          : probe;
      setAllContacts([...data.items].sort(compareContactsByName));
      setSelected(clearSelection());
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? `Couldn't load contacts (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    }
  }

  useEffect(() => {
    load({ q: "", city: "", investorMode: "" });
    listCrmContactExportFields().then(setExportFields).catch(() => setExportFields([]));
    listCrmCustomFields(false).then(setCustomFields).catch(() => setCustomFields([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function runSearch() {
    setPage(1);
    load({ q, city, investorMode });
  }

  function applyInvestorMode(value: string) {
    setInvestorMode(value);
    setPage(1);
    load({ q, city, investorMode: value });
  }

  function clearFilters() {
    setQ("");
    setCity("");
    setInvestorMode("");
    setPage(1);
    load({ q: "", city: "", investorMode: "" });
  }

  function goToPage(nextPage: number) {
    setPage(nextPage); // no network call -- just re-slicing the already-sorted array below
  }

  function changePageSize(nextPageSize: number) {
    setPageSize(nextPageSize);
    setPage(1);
  }

  const total = allContacts?.length ?? 0;
  const contacts = allContacts ? allContacts.slice((page - 1) * pageSize, (page - 1) * pageSize + pageSize) : null;
  const hasActiveFilters = Boolean(q || city || investorMode);
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const rangeStart = total === 0 ? 0 : (page - 1) * pageSize + 1;
  const rangeEnd = Math.min(page * pageSize, total);

  // "Matching results" = every id in allContacts -- already exactly the full filtered
  // set (or the full CRM, when no filters are active) thanks to the fetch-everything
  // load() above, so no separate backend call is needed to know what "all" means here.
  const pageIds = contacts?.map((c) => c.crm_contact_id) ?? [];
  const matchingIds = allContacts?.map((c) => c.crm_contact_id) ?? [];
  const allSelectedOnPage = isPageFullySelected(selected, pageIds);
  const somePartiallySelectedOnPage = isPagePartiallySelected(selected, pageIds);

  useEffect(() => {
    if (selectAllCheckboxRef.current) {
      selectAllCheckboxRef.current.indeterminate = somePartiallySelectedOnPage;
    }
  }, [somePartiallySelectedOnPage]);

  function toggleContact(id: string) {
    setSelected((prev) => toggleOne(prev, id));
  }

  function toggleSelectPage() {
    setSelected((prev) => toggleSelectAllOnPage(prev, pageIds));
  }

  function selectAllMatchingResults() {
    setSelected(selectAllMatching(matchingIds));
  }

  function handleExport() {
    if (!allContacts || !exportFields || !customFields || selected.size === 0) return;
    const columns = buildExportColumns(exportFields, customFields);
    const selectedContacts = allContacts.filter((c) => selected.has(c.crm_contact_id));
    const csv = buildCsv(columns, selectedContacts);
    downloadCsv(csv, exportFilename(new Date()));
  }

  return (
    <div className="mx-auto max-w-5xl px-6 py-10">
      <div className="mb-8 flex items-start justify-between gap-4">
        <div>
          <h1 className="font-serif text-2xl font-medium tracking-tight sm:text-3xl">CRM</h1>
          <p className="mt-2 max-w-2xl text-sm text-muted-foreground">
            Our own record of known prospects and relationships -- separate from Apollo, separate from Campaign Manager.
          </p>
        </div>
        <div className="flex shrink-0 flex-col gap-2">
          <Link href="/crm/new" className={cn(buttonVariants({ size: "sm" }), "gap-1.5")}>
            <Plus className="h-4 w-4" />
            New contact
          </Link>
          <button
            type="button"
            onClick={handleExport}
            disabled={selected.size === 0}
            className={cn(buttonVariants({ size: "sm", variant: "outline" }), "gap-1.5 disabled:cursor-not-allowed disabled:opacity-40")}
          >
            <Download className="h-4 w-4" />
            Export{selected.size > 0 ? ` (${selected.size})` : ""}
          </button>
        </div>
      </div>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          runSearch();
        }}
        className="mb-3 grid gap-2 sm:grid-cols-3"
      >
        <div className="relative sm:col-span-1">
          <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
          <Input
            placeholder="Search name, company, thesis..."
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && runSearch()}
            className="pl-8"
          />
        </div>
        <Input
          placeholder="City"
          value={city}
          onChange={(e) => setCity(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && runSearch()}
        />
        <select
          value={investorMode}
          onChange={(e) => applyInvestorMode(e.target.value)}
          className="h-9 rounded-md border border-input bg-transparent px-3 text-sm"
        >
          <option value="">Any investor mode</option>
          <option value="Privately">Privately</option>
          <option value="Institutionally">Institutionally</option>
          <option value="Both">Both</option>
        </select>
        <button type="submit" className="hidden" />
      </form>

      <div className="mb-6 flex items-center justify-between gap-4">
        <p className="text-sm text-muted-foreground">
          {total > 0 ? `Showing ${rangeStart}–${rangeEnd} of ${total} contact${total === 1 ? "" : "s"}` : null}
        </p>
        {hasActiveFilters && (
          <button
            type="button"
            onClick={clearFilters}
            className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
          >
            <X className="h-3.5 w-3.5" />
            Clear filters
          </button>
        )}
      </div>

      {!error && contacts !== null && contacts.length > 0 && (
        <div className="mb-4 flex flex-wrap items-center gap-4 rounded-lg border border-border/60 bg-secondary/20 px-3 py-2 text-sm">
          <label className="flex cursor-pointer items-center gap-2">
            <input
              ref={selectAllCheckboxRef}
              type="checkbox"
              checked={allSelectedOnPage}
              onChange={toggleSelectPage}
              className="h-4 w-4 cursor-pointer rounded border-input accent-primary"
            />
            <span>Select all on this page ({pageIds.length})</span>
          </label>

          {totalPages > 1 && (
            <button type="button" onClick={selectAllMatchingResults} className="text-primary hover:underline">
              Select all {total} matching contact{total === 1 ? "" : "s"}
            </button>
          )}

          {selected.size > 0 && (
            <>
              <span className="font-medium text-foreground">
                {selected.size} contact{selected.size === 1 ? "" : "s"} selected
              </span>
              <button
                type="button"
                onClick={() => setSelected(clearSelection())}
                className="inline-flex items-center gap-1 text-muted-foreground hover:text-foreground"
              >
                <X className="h-3.5 w-3.5" />
                Clear selection
              </button>
            </>
          )}
        </div>
      )}

      {error && (
        <Alert variant="destructive" className="mb-4">
          <AlertTriangle />
          <AlertTitle>Couldn&apos;t load contacts</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {!error && contacts === null && (
        <div className="grid gap-3 sm:grid-cols-2">
          {Array.from({ length: 6 }).map((_, i) => (
            <Skeleton key={i} className="h-28 rounded-xl" />
          ))}
        </div>
      )}

      {!error && contacts !== null && contacts.length === 0 && (
        <div className="flex flex-col items-center gap-4 rounded-2xl border border-dashed border-border/60 py-20 text-center">
          <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-secondary/60 text-muted-foreground">
            <Users className="h-5 w-5" />
          </div>
          <div>
            <p className="font-medium">{hasActiveFilters ? "No contacts match these filters" : "No contacts yet"}</p>
            <p className="mt-1 max-w-sm text-sm text-muted-foreground">
              {hasActiveFilters
                ? "Try clearing filters or searching for something else."
                : "Add someone manually or import a CSV of prospects you've already researched."}
            </p>
          </div>
          <div className="flex gap-2">
            {hasActiveFilters ? (
              <button type="button" onClick={clearFilters} className={cn(buttonVariants({ size: "sm", variant: "outline" }))}>
                Clear filters
              </button>
            ) : (
              <>
                <Link href="/crm/new" className={cn(buttonVariants({ size: "sm" }))}>
                  New contact
                </Link>
                <Link href="/crm/import" className={cn(buttonVariants({ size: "sm", variant: "outline" }))}>
                  Import CSV
                </Link>
              </>
            )}
          </div>
        </div>
      )}

      {!error && contacts !== null && contacts.length > 0 && (
        <div className="grid gap-3 sm:grid-cols-2">
          {contacts.map((contact) => {
            const name = [contact.first_name, contact.last_name].filter(Boolean).join(" ") || "Unnamed contact";
            const location = [contact.city, contact.state].filter(Boolean).join(", ");
            return (
              <div key={contact.crm_contact_id} className="relative">
                <input
                  type="checkbox"
                  checked={selected.has(contact.crm_contact_id)}
                  onChange={() => toggleContact(contact.crm_contact_id)}
                  aria-label={`Select ${name}`}
                  className="absolute left-3 top-3 z-10 h-4 w-4 cursor-pointer rounded border-input accent-primary"
                />
                <Link href={`/crm/${contact.crm_contact_id}`}>
                  <Card className="h-full pl-9 transition-colors hover:bg-secondary/40">
                    <CardHeader>
                      <div className="mb-1 flex items-start justify-between gap-2">
                        <CardTitle className="leading-snug">{name}</CardTitle>
                        {contact.thesis_investor_mode && (
                          <Badge variant="outline" className="rounded-full border-border/60 font-normal text-muted-foreground">
                            {contact.thesis_investor_mode}
                          </Badge>
                        )}
                      </div>
                      <p className="line-clamp-1 text-sm text-muted-foreground">
                        {[contact.title, contact.company].filter(Boolean).join(" @ ") || "No title/company on file"}
                      </p>
                    </CardHeader>
                    <CardContent className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
                      {location && <span>{location}</span>}
                      {contact.email && <span className="truncate">{contact.email}</span>}
                    </CardContent>
                  </Card>
                </Link>
              </div>
            );
          })}
        </div>
      )}

      {!error && contacts !== null && total > 0 && (
        <div className="mt-6 flex items-center justify-between gap-4">
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <span>Rows per page</span>
            <select
              value={pageSize}
              onChange={(e) => changePageSize(Number(e.target.value))}
              className="h-8 rounded-md border border-input bg-transparent px-2 text-sm"
            >
              {PAGE_SIZE_OPTIONS.map((size) => (
                <option key={size} value={size}>
                  {size}
                </option>
              ))}
            </select>
          </div>
          <div className="flex items-center gap-3">
            <span className="text-sm text-muted-foreground">
              Page {page} of {totalPages}
            </span>
            <button
              type="button"
              onClick={() => goToPage(page - 1)}
              disabled={page <= 1}
              className={cn(buttonVariants({ size: "sm", variant: "outline" }), "gap-1 disabled:opacity-40")}
            >
              <ChevronLeft className="h-4 w-4" />
              Previous
            </button>
            <button
              type="button"
              onClick={() => goToPage(page + 1)}
              disabled={page >= totalPages}
              className={cn(buttonVariants({ size: "sm", variant: "outline" }), "gap-1 disabled:opacity-40")}
            >
              Next
              <ChevronRight className="h-4 w-4" />
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
