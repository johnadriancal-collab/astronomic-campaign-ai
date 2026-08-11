"use client";

import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";
import { Download } from "lucide-react";
import { buttonVariants } from "@/components/ui/button";
import { ContactResults } from "@/components/crm-contact-results";
import { FilterBuilder } from "@/components/crm-filter-builder";
import {
  ApiError,
  listCrmContactExportFields,
  listCrmCustomFields,
  listCrmFilterableFields,
  queryCrmContacts,
  type CrmContact,
  type CrmContactExportField,
  type CrmCustomFieldDefinition,
  type FilterCondition,
  type FilterFieldMeta,
  type FilterQuery,
  type FilterSort,
} from "@/lib/api";
import { clearSelection, isPageFullySelected, isPagePartiallySelected, selectAllMatching, toggleOne, toggleSelectAllOnPage } from "@/lib/contact-selection";
import { fetchAllMatchingContacts, resolveContactsForExport } from "@/lib/crm-bulk-selection";
import { buildCsv, buildExportColumns, downloadCsv, exportFilename } from "@/lib/csv-export";
import { allConditionsComplete } from "@/lib/filter-conditions";
import { decodeFilterQuery, encodeFilterQuery } from "@/lib/filter-query-url";
import { cn } from "@/lib/utils";

// The exact 7 sortable fields the user asked for, in the order they should
// appear in the "Sort by" dropdown -- labels are pulled from the filterable-
// fields registry (not hardcoded here) so they never drift from the backend.
const SORTABLE_FIELD_KEYS = ["first_name", "last_name", "company", "city", "state", "created_at", "updated_at"];
const DEFAULT_SORT: FilterSort = { field: "last_name", direction: "asc" };

export default function CrmFiltersPage() {
  return (
    <Suspense>
      <CrmFiltersPageInner />
    </Suspense>
  );
}

function CrmFiltersPageInner() {
  const router = useRouter();
  const searchParams = useSearchParams();

  const [fields, setFields] = useState<FilterFieldMeta[] | null>(null);
  const [exportFields, setExportFields] = useState<CrmContactExportField[] | null>(null);
  const [customFields, setCustomFields] = useState<CrmCustomFieldDefinition[] | null>(null);
  const [fieldsError, setFieldsError] = useState<string | null>(null);

  const initial = decodeFilterQuery(searchParams);
  const [conditions, setConditions] = useState<FilterCondition[]>(initial.filters);
  const [logic, setLogic] = useState<"AND" | "OR">(initial.logic);
  const [page, setPage] = useState(initial.page ?? 1);
  const [pageSize, setPageSize] = useState(initial.page_size ?? 50);
  const [sort, setSort] = useState<FilterSort>(initial.sort ?? DEFAULT_SORT);

  const [contacts, setContacts] = useState<CrmContact[] | null>(null);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [searching, setSearching] = useState(false);
  const [hasSearched, setHasSearched] = useState(conditions.length > 0);

  const [selected, setSelected] = useState<Set<string>>(new Set());
  // Every contact matching the current query, fetched only when the user
  // asks for "Select all N matching"/"Export all matching" -- normal
  // browsing stays server-paginated (`contacts` above holds just one page).
  // Cleared whenever a new/refreshed search runs, same as `selected`.
  const [matchingContacts, setMatchingContacts] = useState<CrmContact[] | null>(null);
  const [bulkBusy, setBulkBusy] = useState(false);
  const selectAllCheckboxRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    listCrmFilterableFields().then(setFields).catch(() => setFieldsError("Couldn't load the filter field list."));
    listCrmContactExportFields().then(setExportFields).catch(() => setExportFields([]));
    listCrmCustomFields(false).then(setCustomFields).catch(() => setCustomFields([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const runSearch = useCallback(
    async (nextConditions: FilterCondition[], nextLogic: "AND" | "OR", nextPage: number, nextPageSize: number, nextSort: FilterSort) => {
      setSearching(true);
      setHasSearched(true);
      try {
        const result = await queryCrmContacts({
          filters: nextConditions,
          logic: nextLogic,
          page: nextPage,
          page_size: nextPageSize,
          sort: nextSort,
        });
        setContacts(result.items);
        setTotal(result.total);
        setSelected(clearSelection());
        setMatchingContacts(null);
        setError(null);
      } catch (err) {
        setError(err instanceof ApiError ? `Couldn't run this query (${err.status}): ${err.message}` : "Couldn't reach the backend.");
      } finally {
        setSearching(false);
      }
    },
    []
  );

  // Runs once on mount if the URL already carried a query (a bookmarked/shared/
  // refreshed filtered link) -- otherwise the page starts empty, waiting for the
  // user to build a query and press Search.
  useEffect(() => {
    if (initial.filters.length > 0) {
      void runSearch(initial.filters, initial.logic, initial.page ?? 1, initial.page_size ?? 50, initial.sort ?? DEFAULT_SORT);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function syncUrl(nextConditions: FilterCondition[], nextLogic: "AND" | "OR", nextPage: number, nextPageSize: number, nextSort: FilterSort) {
    const params = encodeFilterQuery({ filters: nextConditions, logic: nextLogic, page: nextPage, page_size: nextPageSize, sort: nextSort });
    router.replace(params.toString() ? `/crm/filters?${params.toString()}` : "/crm/filters");
  }

  function handleSearch() {
    if (!allConditionsComplete(conditions)) return;
    setPage(1);
    syncUrl(conditions, logic, 1, pageSize, sort);
    void runSearch(conditions, logic, 1, pageSize, sort);
  }

  function handleClear() {
    setConditions([]);
    setLogic("AND");
    setPage(1);
    setSort(DEFAULT_SORT);
    setContacts(null);
    setTotal(0);
    setHasSearched(false);
    setSelected(clearSelection());
    setMatchingContacts(null);
    setError(null);
    router.replace("/crm/filters");
  }

  function goToPage(nextPage: number) {
    setPage(nextPage);
    syncUrl(conditions, logic, nextPage, pageSize, sort);
    void runSearch(conditions, logic, nextPage, pageSize, sort);
  }

  function changePageSize(nextPageSize: number) {
    setPageSize(nextPageSize);
    setPage(1);
    syncUrl(conditions, logic, 1, nextPageSize, sort);
    void runSearch(conditions, logic, 1, nextPageSize, sort);
  }

  // Changing sort before ever searching just updates the pending control --
  // there's nothing to re-sort yet, and the filter row(s) may still be
  // incomplete. Once a search has run, a sort change re-runs immediately
  // (same UX as changing the page size), rather than requiring another
  // explicit Search click.
  function changeSort(nextSort: FilterSort) {
    setSort(nextSort);
    if (!hasSearched) return;
    setPage(1);
    syncUrl(conditions, logic, 1, pageSize, nextSort);
    void runSearch(conditions, logic, 1, pageSize, nextSort);
  }

  const pageIds = contacts?.map((c) => c.crm_contact_id) ?? [];
  const allSelectedOnPage = isPageFullySelected(selected, pageIds);
  const somePartiallySelectedOnPage = isPagePartiallySelected(selected, pageIds);

  useEffect(() => {
    if (selectAllCheckboxRef.current) {
      selectAllCheckboxRef.current.indeterminate = somePartiallySelectedOnPage;
    }
  }, [somePartiallySelectedOnPage]);

  // The active query's filters/logic/sort, with no page/page_size -- the
  // shared helper always overrides those itself to fetch the COMPLETE
  // matching set, regardless of whatever page the user is currently on.
  function currentContentQuery(): FilterQuery {
    return { filters: conditions, logic, sort };
  }

  async function selectAllMatchingResults() {
    if (bulkBusy) return;
    setBulkBusy(true);
    try {
      const all = await fetchAllMatchingContacts(currentContentQuery(), queryCrmContacts);
      setMatchingContacts(all);
      setSelected(selectAllMatching(all.map((c) => c.crm_contact_id)));
    } catch (err) {
      setError(err instanceof ApiError ? `Couldn't select all matching contacts (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    } finally {
      setBulkBusy(false);
    }
  }

  // Selection can include ids beyond the current page (from "Select all N
  // matching", or a hand-picked selection carried across page changes) --
  // resolveContactsForExport fetches the full matching set on demand only
  // when that's actually the case, rather than silently exporting just
  // whatever happens to be on the current page.
  async function handleExportSelected() {
    if (!exportFields || !customFields || selected.size === 0 || bulkBusy) return;
    setBulkBusy(true);
    try {
      const knownContacts = matchingContacts ?? contacts ?? [];
      const selectedContacts = await resolveContactsForExport(selected, knownContacts, currentContentQuery(), queryCrmContacts);
      const columns = buildExportColumns(exportFields, customFields);
      downloadCsv(buildCsv(columns, selectedContacts), exportFilename(new Date()));
    } catch (err) {
      setError(err instanceof ApiError ? `Couldn't export contacts (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    } finally {
      setBulkBusy(false);
    }
  }

  // Exports every contact matching the current query, independent of
  // selection -- doesn't require "Select all matching" to be clicked first.
  async function handleExportAllMatching() {
    if (!exportFields || !customFields || total === 0 || bulkBusy) return;
    setBulkBusy(true);
    try {
      const all = matchingContacts ?? (await fetchAllMatchingContacts(currentContentQuery(), queryCrmContacts));
      const columns = buildExportColumns(exportFields, customFields);
      downloadCsv(buildCsv(columns, all), exportFilename(new Date()));
    } catch (err) {
      setError(err instanceof ApiError ? `Couldn't export contacts (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    } finally {
      setBulkBusy(false);
    }
  }

  return (
    <div className="mx-auto max-w-5xl px-6 py-10">
      <div className="mb-8 flex items-start justify-between gap-4">
        <div>
          <h1 className="font-serif text-2xl font-medium tracking-tight sm:text-3xl">More Filters</h1>
          <p className="mt-2 max-w-2xl text-sm text-muted-foreground">
            Build a query across every CRM field -- core, Investor Thesis, and custom -- to find contacts for prospecting and
            segmentation.
          </p>
        </div>
        <div className="flex shrink-0 gap-2">
          <button
            type="button"
            onClick={handleExportSelected}
            disabled={selected.size === 0 || bulkBusy}
            className={cn(buttonVariants({ size: "sm", variant: "outline" }), "gap-1.5 disabled:cursor-not-allowed disabled:opacity-40")}
          >
            <Download className="h-4 w-4" />
            Export selected{selected.size > 0 ? ` (${selected.size})` : ""}
          </button>
          <button
            type="button"
            onClick={handleExportAllMatching}
            disabled={total === 0 || bulkBusy}
            className={cn(buttonVariants({ size: "sm", variant: "outline" }), "gap-1.5 disabled:cursor-not-allowed disabled:opacity-40")}
          >
            <Download className="h-4 w-4" />
            Export all matching
          </button>
        </div>
      </div>

      {fieldsError && <p className="mb-4 text-sm text-destructive">{fieldsError}</p>}

      {fields && (
        <FilterBuilder
          fields={fields}
          conditions={conditions}
          logic={logic}
          onConditionsChange={setConditions}
          onLogicChange={setLogic}
          onSearch={handleSearch}
          onClear={handleClear}
          searching={searching}
          sortField={sort.field}
          sortDirection={sort.direction}
          sortFieldOptions={SORTABLE_FIELD_KEYS.map((key) => ({
            key,
            label: fields.find((f) => f.key === key)?.label ?? key,
          }))}
          onSortFieldChange={(field) => changeSort({ field, direction: sort.direction })}
          onSortDirectionChange={(direction) => changeSort({ field: sort.field, direction })}
        />
      )}

      {hasSearched && (
        <div className="mt-8">
          <ContactResults
            contacts={contacts}
            total={total}
            page={page}
            pageSize={pageSize}
            error={error}
            hasActiveFilters={conditions.length > 0}
            onClearFilters={handleClear}
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
            onGoToPage={goToPage}
            onChangePageSize={changePageSize}
            emptyStateAction={
              <Link href="/crm" className={cn(buttonVariants({ size: "sm", variant: "outline" }))}>
                Back to Contacts
              </Link>
            }
          />
        </div>
      )}
    </div>
  );
}
