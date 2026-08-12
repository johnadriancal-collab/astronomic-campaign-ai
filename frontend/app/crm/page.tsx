"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { Download, Plus, Search } from "lucide-react";
import { AddToListPanel } from "@/components/add-to-list-panel";
import { buttonVariants } from "@/components/ui/button";
import { ContactResults } from "@/components/crm-contact-results";
import { Input } from "@/components/ui/input";
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
        <div className="flex shrink-0 flex-col items-end gap-2">
          <Link href="/crm/new" className={cn(buttonVariants({ size: "sm" }), "gap-1.5")}>
            <Plus className="h-4 w-4" />
            New contact
          </Link>
          <div className="flex gap-2">
            <AddToListPanel selectedIds={[...selected]} />
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

      <ContactResults
        contacts={contacts}
        total={total}
        page={page}
        pageSize={pageSize}
        error={error}
        hasActiveFilters={hasActiveFilters}
        onClearFilters={clearFilters}
        selected={selected}
        onToggleContact={toggleContact}
        selectAllCheckboxRef={selectAllCheckboxRef}
        allSelectedOnPage={allSelectedOnPage}
        onToggleSelectPage={toggleSelectPage}
        onSelectAllMatching={selectAllMatchingResults}
        onClearSelection={() => setSelected(clearSelection())}
        onGoToPage={goToPage}
        onChangePageSize={changePageSize}
        emptyStateAction={
          <>
            <Link href="/crm/new" className={cn(buttonVariants({ size: "sm" }))}>
              New contact
            </Link>
            <Link href="/crm/import" className={cn(buttonVariants({ size: "sm", variant: "outline" }))}>
              Import CSV
            </Link>
          </>
        }
      />
    </div>
  );
}
