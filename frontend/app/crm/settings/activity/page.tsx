"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { AlertTriangle, ChevronDown, ChevronUp, ClipboardList, Search } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { buttonVariants } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  ApiError,
  listActivityEvents,
  type ActivityCategory,
  type ActivityEvent,
} from "@/lib/api";
import { CATEGORY_OPTIONS, detailLines, entityLink, eventTitle, formatEventTimestamp } from "@/lib/activity";
import { cn } from "@/lib/utils";

const PAGE_SIZE = 25;

export default function ActivityLogPage() {
  const [events, setEvents] = useState<ActivityEvent[] | null>(null);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const [q, setQ] = useState("");
  const [searchInput, setSearchInput] = useState("");
  const [category, setCategory] = useState<ActivityCategory | "all">("all");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");

  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  async function load(nextPage: number) {
    setLoading(true);
    try {
      const result = await listActivityEvents({
        category: category === "all" ? undefined : category,
        q: q || undefined,
        date_from: dateFrom ? new Date(dateFrom).toISOString() : undefined,
        date_to: dateTo ? new Date(dateTo).toISOString() : undefined,
        page: nextPage,
        page_size: PAGE_SIZE,
      });
      setEvents(result.items);
      setTotal(result.total);
      setPage(result.page);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? `Couldn't load the activity log (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load(1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [category, q, dateFrom, dateTo]);

  function runSearch() {
    setQ(searchInput.trim());
  }

  function toggleExpanded(eventId: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(eventId)) next.delete(eventId);
      else next.add(eventId);
      return next;
    });
  }

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <div className="mx-auto max-w-3xl px-6 py-10">
      <div className="mb-6">
        <h1 className="flex items-center gap-2 font-serif text-2xl font-medium tracking-tight">
          <ClipboardList className="h-5 w-5 text-muted-foreground" />
          Activity Log
        </h1>
        <p className="mt-2 text-sm text-muted-foreground">
          A persistent record of meaningful CRM and operational actions -- ITF submissions, contact and list changes,
          CSV imports, exports, and campaign lifecycle events. Ordinary searches and views are never recorded here.
        </p>
      </div>

      <div className="mb-4 flex flex-col gap-3 sm:flex-row sm:items-center">
        <div className="relative flex-1">
          <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
          <Input
            placeholder="Search summaries, entity names..."
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && runSearch()}
            onBlur={runSearch}
            className="pl-8"
          />
        </div>
        <select
          value={category}
          onChange={(e) => setCategory(e.target.value as ActivityCategory | "all")}
          className="h-9 rounded-md border border-input bg-transparent px-3 text-sm"
        >
          {CATEGORY_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
        <Input type="date" value={dateFrom} onChange={(e) => setDateFrom(e.target.value)} className="sm:w-36" aria-label="From date" />
        <Input type="date" value={dateTo} onChange={(e) => setDateTo(e.target.value)} className="sm:w-36" aria-label="To date" />
      </div>

      {error && (
        <Alert variant="destructive" className="mb-4">
          <AlertTriangle />
          <AlertTitle>Couldn&apos;t load the activity log</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {loading && !events && <p className="py-8 text-center text-sm text-muted-foreground">Loading…</p>}

      {!loading && !error && events && events.length === 0 && (
        <p className="py-8 text-center text-sm text-muted-foreground">
          No activity recorded yet{q || category !== "all" || dateFrom || dateTo ? " for this filter." : "."}
        </p>
      )}

      {events && events.length > 0 && (
        <div className="space-y-2">
          {events.map((event) => {
            const link = entityLink(event);
            const lines = detailLines(event);
            const isExpanded = expanded.has(event.event_id);
            return (
              <div key={event.event_id} className="rounded-lg border border-border bg-card p-3">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2 text-xs text-muted-foreground">
                      <span title={new Date(event.created_at).toLocaleString()}>{formatEventTimestamp(event.created_at)}</span>
                      <span>&middot;</span>
                      <span>{eventTitle(event)}</span>
                    </div>
                    <p className="mt-1 text-sm">{event.summary}</p>
                    {link && (
                      <Link href={link.href} className="mt-1 inline-block text-xs text-primary hover:underline">
                        {link.label}
                      </Link>
                    )}
                  </div>
                  {lines.length > 0 && (
                    <button
                      type="button"
                      onClick={() => toggleExpanded(event.event_id)}
                      className="shrink-0 rounded-md p-1 text-muted-foreground hover:bg-secondary/60"
                      aria-label={isExpanded ? "Hide details" : "Show details"}
                    >
                      {isExpanded ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
                    </button>
                  )}
                </div>
                {isExpanded && lines.length > 0 && (
                  <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 border-t border-border pt-3 text-xs">
                    {lines.map((line) => (
                      <div key={line.label} className="contents">
                        <dt className="text-muted-foreground">{line.label}</dt>
                        <dd className="break-all">{line.value}</dd>
                      </div>
                    ))}
                  </dl>
                )}
              </div>
            );
          })}
        </div>
      )}

      {events && total > PAGE_SIZE && (
        <div className="mt-4 flex items-center justify-between text-sm text-muted-foreground">
          <span>
            Page {page} of {totalPages} &middot; {total} events
          </span>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={() => load(page - 1)}
              disabled={page <= 1}
              className={cn(buttonVariants({ size: "sm", variant: "outline" }), "disabled:cursor-not-allowed disabled:opacity-40")}
            >
              Previous
            </button>
            <button
              type="button"
              onClick={() => load(page + 1)}
              disabled={page >= totalPages}
              className={cn(buttonVariants({ size: "sm", variant: "outline" }), "disabled:cursor-not-allowed disabled:opacity-40")}
            >
              Next
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
