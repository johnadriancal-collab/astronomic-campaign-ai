"use client";

import type { RefObject } from "react";
import Link from "next/link";
import { AlertTriangle, ChevronLeft, ChevronRight, Users, X } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import type { CrmContact } from "@/lib/api";
import {
  contactResultsMode,
  contactResultsSummaryText,
  formatContactLocation,
  formatContactName,
  formatContactTitleCompany,
} from "@/lib/contact-results-view";
import { cn } from "@/lib/utils";

const PAGE_SIZE_OPTIONS = [25, 50, 100];

/**
 * The contact card grid + selection bar + pagination controls -- shared by the
 * Contacts page (/crm) and More Filters (/crm/filters) so there is exactly one
 * results/pagination implementation, not two. Lifted verbatim out of crm/page.tsx
 * (same markup/classNames) rather than redesigned, so the Contacts page's visual
 * behavior is unchanged by this extraction.
 *
 * Selection/pagination STATE lives in the parent page (each page has its own
 * search/filter state to key it off of) -- this component is purely the view plus
 * the small amount of local derived values (name/location formatting) that don't
 * need to live in the parent.
 */
export function ContactResults({
  contacts,
  total,
  page,
  pageSize,
  error,
  hasActiveFilters,
  onClearFilters,
  selected,
  onToggleContact,
  selectAllCheckboxRef,
  allSelectedOnPage,
  onToggleSelectPage,
  onSelectAllMatching,
  onClearSelection,
  onGoToPage,
  onChangePageSize,
  emptyStateAction,
  showSelectAllMatching = true,
  hideSelection = false,
  hidePagination = false,
}: {
  contacts: CrmContact[] | null;
  total: number;
  page?: number;
  pageSize?: number;
  error: string | null;
  hasActiveFilters: boolean;
  onClearFilters: () => void;
  selected?: Set<string>;
  onToggleContact?: (id: string) => void;
  selectAllCheckboxRef?: RefObject<HTMLInputElement | null>;
  allSelectedOnPage?: boolean;
  onToggleSelectPage?: () => void;
  onSelectAllMatching?: () => void;
  onClearSelection?: () => void;
  onGoToPage?: (page: number) => void;
  onChangePageSize?: (pageSize: number) => void;
  emptyStateAction?: React.ReactNode;
  // "Select all N matching contacts" requires holding every matching id in memory,
  // which is exactly what server-side pagination (More Filters) deliberately avoids --
  // this stays true (unchanged) only for the Contacts page's existing fetch-the-full-
  // filtered-set behavior. More Filters passes false rather than show a button whose
  // label ("Select all N matching") would overpromise what it actually selects.
  showSelectAllMatching?: boolean;
  // Hides bulk-selection chrome (checkboxes, "Select all" bar) entirely.
  // Neither Contacts, More Filters, nor Astro Search pass this today -- all
  // three support bulk selection/export -- but it stays available as an
  // independent flag for any future read-only caller.
  hideSelection?: boolean;
  // Hides the rows-per-page + Previous/Next footer, independent of selection
  // chrome. Astro Search passes this: it only ever holds the first
  // page_size matches the backend returned and cannot page through more,
  // so showing paging controls would imply a capability that doesn't exist
  // -- but it DOES support bulk selection/export against its full matching
  // set, so selection chrome stays visible. See app/crm/astro/page.tsx.
  hidePagination?: boolean;
}) {
  const effectivePage = page ?? 1;
  const effectivePageSize = pageSize ?? Math.max(total, 1);
  const totalPages = Math.max(1, Math.ceil(total / effectivePageSize));
  const pageIds = contacts?.map((c) => c.crm_contact_id) ?? [];
  const mode = contactResultsMode({ hideSelection, hidePagination });
  const summaryText = contactResultsSummaryText({
    hidePagination,
    total,
    page: effectivePage,
    pageSize: effectivePageSize,
    renderedCount: contacts?.length ?? 0,
  });

  return (
    <>
      <div className="mb-6 flex items-center justify-between gap-4">
        <p className="text-sm text-muted-foreground">{summaryText}</p>
        {hasActiveFilters && (
          <button
            type="button"
            onClick={onClearFilters}
            className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
          >
            <X className="h-3.5 w-3.5" />
            Clear filters
          </button>
        )}
      </div>

      {mode.showSelectionChrome && !error && contacts !== null && contacts.length > 0 && (
        <div className="mb-4 flex flex-wrap items-center gap-4 rounded-lg border border-border/60 bg-secondary/20 px-3 py-2 text-sm">
          <label className="flex cursor-pointer items-center gap-2">
            <input
              ref={selectAllCheckboxRef}
              type="checkbox"
              checked={allSelectedOnPage}
              onChange={onToggleSelectPage}
              className="h-4 w-4 cursor-pointer rounded border-input accent-primary"
            />
            <span>Select all on this page ({pageIds.length})</span>
          </label>

          {showSelectAllMatching && totalPages > 1 && (
            <button type="button" onClick={onSelectAllMatching} className="text-primary hover:underline">
              Select all {total} matching contact{total === 1 ? "" : "s"}
            </button>
          )}

          {(selected?.size ?? 0) > 0 && (
            <>
              <span className="font-medium text-foreground">
                {selected?.size ?? 0} contact{(selected?.size ?? 0) === 1 ? "" : "s"} selected
              </span>
              <button
                type="button"
                onClick={onClearSelection}
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
              <button type="button" onClick={onClearFilters} className={cn(buttonVariants({ size: "sm", variant: "outline" }))}>
                Clear filters
              </button>
            ) : (
              emptyStateAction
            )}
          </div>
        </div>
      )}

      {!error && contacts !== null && contacts.length > 0 && (
        <div className="grid gap-3 sm:grid-cols-2">
          {contacts.map((contact) => {
            const name = formatContactName(contact);
            const location = formatContactLocation(contact);
            return (
              <div key={contact.crm_contact_id} className="relative">
                {mode.showSelectionChrome && (
                  <input
                    type="checkbox"
                    checked={selected?.has(contact.crm_contact_id) ?? false}
                    onChange={() => onToggleContact?.(contact.crm_contact_id)}
                    aria-label={`Select ${name}`}
                    className="absolute left-3 top-3 z-10 h-4 w-4 cursor-pointer rounded border-input accent-primary"
                  />
                )}
                <Link href={`/crm/${contact.crm_contact_id}`}>
                  <Card className={cn("h-full transition-colors hover:bg-secondary/40", mode.showSelectionChrome && "pl-9")}>
                    <CardHeader>
                      <div className="mb-1 flex items-start justify-between gap-2">
                        <CardTitle className="leading-snug">{name}</CardTitle>
                        {contact.thesis_investor_mode && (
                          <Badge variant="outline" className="rounded-full border-border/60 font-normal text-muted-foreground">
                            {contact.thesis_investor_mode}
                          </Badge>
                        )}
                      </div>
                      <p className="line-clamp-1 text-sm text-muted-foreground">{formatContactTitleCompany(contact)}</p>
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

      {mode.showPagination && !error && contacts !== null && total > 0 && (
        <div className="mt-6 flex items-center justify-between gap-4">
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <span>Rows per page</span>
            <select
              value={effectivePageSize}
              onChange={(e) => onChangePageSize?.(Number(e.target.value))}
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
              Page {effectivePage} of {totalPages}
            </span>
            <button
              type="button"
              onClick={() => onGoToPage?.(effectivePage - 1)}
              disabled={effectivePage <= 1}
              className={cn(buttonVariants({ size: "sm", variant: "outline" }), "gap-1 disabled:opacity-40")}
            >
              <ChevronLeft className="h-4 w-4" />
              Previous
            </button>
            <button
              type="button"
              onClick={() => onGoToPage?.(effectivePage + 1)}
              disabled={effectivePage >= totalPages}
              className={cn(buttonVariants({ size: "sm", variant: "outline" }), "gap-1 disabled:opacity-40")}
            >
              Next
              <ChevronRight className="h-4 w-4" />
            </button>
          </div>
        </div>
      )}
    </>
  );
}
