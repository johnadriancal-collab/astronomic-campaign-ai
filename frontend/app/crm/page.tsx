"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { AlertTriangle, ChevronLeft, ChevronRight, Plus, Search, Users, X } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError, listCrmContacts, type CrmContact } from "@/lib/api";
import { cn } from "@/lib/utils";

const PAGE_SIZE_OPTIONS = [25, 50, 100];

interface Filters {
  q: string;
  city: string;
  investorMode: string;
}

export default function CrmContactsPage() {
  const [contacts, setContacts] = useState<CrmContact[] | null>(null);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [city, setCity] = useState("");
  const [investorMode, setInvestorMode] = useState("");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);

  async function load(filters: Filters, targetPage: number, targetPageSize: number) {
    try {
      const data = await listCrmContacts({
        q: filters.q || undefined,
        city: filters.city || undefined,
        investor_mode: filters.investorMode || undefined,
        page: targetPage,
        page_size: targetPageSize,
      });
      setContacts(data.items);
      setTotal(data.total);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? `Couldn't load contacts (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    }
  }

  useEffect(() => {
    load({ q: "", city: "", investorMode: "" }, 1, 50);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function runSearch() {
    setPage(1);
    load({ q, city, investorMode }, 1, pageSize);
  }

  function applyInvestorMode(value: string) {
    setInvestorMode(value);
    setPage(1);
    load({ q, city, investorMode: value }, 1, pageSize);
  }

  function clearFilters() {
    setQ("");
    setCity("");
    setInvestorMode("");
    setPage(1);
    load({ q: "", city: "", investorMode: "" }, 1, pageSize);
  }

  function goToPage(nextPage: number) {
    setPage(nextPage);
    load({ q, city, investorMode }, nextPage, pageSize);
  }

  function changePageSize(nextPageSize: number) {
    setPageSize(nextPageSize);
    setPage(1);
    load({ q, city, investorMode }, 1, nextPageSize);
  }

  const hasActiveFilters = Boolean(q || city || investorMode);
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const rangeStart = total === 0 ? 0 : (page - 1) * pageSize + 1;
  const rangeEnd = Math.min(page * pageSize, total);

  return (
    <div className="mx-auto max-w-5xl px-6 py-10">
      <div className="mb-8 flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight sm:text-3xl">CRM</h1>
          <p className="mt-2 max-w-2xl text-sm text-muted-foreground">
            Our own record of known prospects and relationships -- separate from Apollo, separate from Campaign Manager.
          </p>
        </div>
        <Link href="/crm/new" className={cn(buttonVariants({ size: "sm" }), "gap-1.5 shrink-0")}>
          <Plus className="h-4 w-4" />
          New contact
        </Link>
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
              <Link key={contact.crm_contact_id} href={`/crm/${contact.crm_contact_id}`}>
                <Card className="h-full transition-colors hover:bg-secondary/40">
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
