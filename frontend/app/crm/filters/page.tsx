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
  type FilterSort,
} from "@/lib/api";
import { clearSelection, isPageFullySelected, isPagePartiallySelected, toggleOne, toggleSelectAllOnPage } from "@/lib/contact-selection";
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

  function handleExport() {
    if (!contacts || !exportFields || !customFields || selected.size === 0) return;
    const columns = buildExportColumns(exportFields, customFields);
    const selectedContacts = contacts.filter((c) => selected.has(c.crm_contact_id));
    const csv = buildCsv(columns, selectedContacts);
    downloadCsv(csv, exportFilename(new Date()));
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
        <button
          type="button"
          onClick={handleExport}
          disabled={selected.size === 0}
          className={cn(buttonVariants({ size: "sm", variant: "outline" }), "shrink-0 gap-1.5 disabled:cursor-not-allowed disabled:opacity-40")}
        >
          <Download className="h-4 w-4" />
          Export{selected.size > 0 ? ` (${selected.size})` : ""}
        </button>
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
            showSelectAllMatching={false}
            onClearSelection={() => setSelected(clearSelection())}
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
