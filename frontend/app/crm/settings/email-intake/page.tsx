"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { AlertTriangle, Mail, Paperclip, Search } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { buttonVariants } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ApiError, listEmailIntakeItems, type EmailIntakeItem, type EmailIntakeStatus } from "@/lib/api";
import { STATUS_OPTIONS, senderDisplayName, statusBadgeClass, statusLabel } from "@/lib/email-intake";
import { formatEventTimestamp } from "@/lib/activity";
import { cn } from "@/lib/utils";

const PAGE_SIZE = 25;

export default function EmailIntakePage() {
  const [items, setItems] = useState<EmailIntakeItem[] | null>(null);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const [q, setQ] = useState("");
  const [searchInput, setSearchInput] = useState("");
  const [status, setStatus] = useState<EmailIntakeStatus | "all">("all");

  async function load(nextPage: number) {
    setLoading(true);
    try {
      const result = await listEmailIntakeItems({
        status: status === "all" ? undefined : status,
        q: q || undefined,
        page: nextPage,
        page_size: PAGE_SIZE,
      });
      setItems(result.items);
      setTotal(result.total);
      setPage(result.page);
      setError(null);
    } catch (err) {
      setError(
        err instanceof ApiError ? `Couldn't load the Email Intake queue (${err.status}): ${err.message}` : "Couldn't reach the backend."
      );
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load(1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, q]);

  function runSearch() {
    setQ(searchInput.trim());
  }

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <div className="mx-auto max-w-3xl px-6 py-10">
      <div className="mb-6">
        <h1 className="flex items-center gap-2 font-serif text-2xl font-medium tracking-tight">
          <Mail className="h-5 w-5 text-muted-foreground" />
          Email Intake
        </h1>
        <p className="mt-2 text-sm text-muted-foreground">
          Emails proposing CRM changes, held here for review. Nothing here has ever updated a CRM contact --
          every field change requires an explicit Approve before it&apos;s applied.
        </p>
      </div>

      <div className="mb-4 flex flex-col gap-3 sm:flex-row sm:items-center">
        <div className="relative flex-1">
          <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
          <Input
            placeholder="Search sender, subject, contact..."
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && runSearch()}
            onBlur={runSearch}
            className="pl-8"
          />
        </div>
        <select
          value={status}
          onChange={(e) => setStatus(e.target.value as EmailIntakeStatus | "all")}
          className="h-9 rounded-md border border-input bg-transparent px-3 text-sm"
        >
          {STATUS_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
      </div>

      {error && (
        <Alert variant="destructive" className="mb-4">
          <AlertTriangle />
          <AlertTitle>Couldn&apos;t load the Email Intake queue</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {loading && !items && <p className="py-8 text-center text-sm text-muted-foreground">Loading…</p>}

      {!loading && !error && items && items.length === 0 && (
        <p className="py-8 text-center text-sm text-muted-foreground">
          No email intake items yet{q || status !== "all" ? " for this filter." : "."}
        </p>
      )}

      {items && items.length > 0 && (
        <div className="space-y-2">
          {items.map((item) => (
            <Link
              key={item.intake_id}
              href={`/crm/settings/email-intake/${item.intake_id}`}
              className="block rounded-lg border border-border bg-card p-3 transition-colors hover:bg-secondary/40"
            >
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex items-center gap-2 text-xs text-muted-foreground">
                    <span title={new Date(item.received_at).toLocaleString()}>{formatEventTimestamp(item.received_at)}</span>
                    <span>&middot;</span>
                    <span className="truncate">{senderDisplayName(item.sender)}</span>
                    {item.attachments.length > 0 && <Paperclip className="h-3 w-3 shrink-0" />}
                  </div>
                  <p className="mt-1 truncate text-sm font-medium">{item.subject || "(no subject)"}</p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    {item.matched_contact_name ? `Matched: ${item.matched_contact_name}` : "Needs Match"}
                    {item.status === "pending_review" &&
                      (item.proposal.length > 0
                        ? ` · ${item.proposal.length} proposed change${item.proposal.length === 1 ? "" : "s"}`
                        : " · No confidently extracted changes")}
                  </p>
                </div>
                <span
                  className={cn("shrink-0 rounded-full px-2 py-0.5 text-xs font-medium", statusBadgeClass(item.status))}
                >
                  {statusLabel(item.status)}
                </span>
              </div>
            </Link>
          ))}
        </div>
      )}

      {items && total > PAGE_SIZE && (
        <div className="mt-4 flex items-center justify-between text-sm text-muted-foreground">
          <span>
            Page {page} of {totalPages} &middot; {total} items
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
