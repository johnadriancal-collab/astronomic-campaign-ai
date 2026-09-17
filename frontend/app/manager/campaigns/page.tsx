"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { AlertTriangle, ArrowDown, ArrowUp, ArrowUpDown, Megaphone, Plus } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { buttonVariants } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  ApiError,
  listMailCampaignList,
  type MailCampaignListItem,
  type MailCampaignListSortBy,
  type MailCampaignStatus,
} from "@/lib/api";
import { mailCampaignStatusBadgeClass, mailCampaignStatusLabel } from "@/lib/mail";
import { MAIL_CAMPAIGN_DETAIL_CONTAINER_CLASS } from "@/lib/mail-campaign-layout";
import { cn } from "@/lib/utils";

// Campaign Manager Campaigns V1 (2026-09-17). One row per campaign, wide
// table (not the earlier side-by-side card grid) -- see
// MailCampaignListService's backend docstring for exactly what each row
// aggregates. Every count here is real workload/step data already tracked
// elsewhere; this view shows nothing that isn't genuinely measured
// anywhere in this system.
//
// Genuinely server-paginated, same stance as the Leads list page --
// every filter/sort/page change re-fetches from GET /mail/campaign-list.

const STATUS_OPTIONS: { value: MailCampaignStatus; label: string }[] = [
  { value: "draft", label: "Draft" },
  { value: "ready", label: "Ready" },
  { value: "active", label: "Active" },
  { value: "paused", label: "Paused" },
  { value: "completed", label: "Completed" },
  { value: "archived", label: "Archived" },
];

const PAGE_SIZE_OPTIONS = [25, 50];

// Compact -- omits the year (every campaign here is recent enough that
// it's rarely ambiguous) so the Last Updated column stays narrow and
// single-line at the tighter row height below.
function formatDateTime(iso: string): string {
  return new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function SortHeader({
  label,
  column,
  sortBy,
  sortDir,
  onSort,
  align = "left",
}: {
  label: string;
  column: MailCampaignListSortBy;
  sortBy: MailCampaignListSortBy;
  sortDir: "asc" | "desc";
  onSort: (column: MailCampaignListSortBy) => void;
  align?: "left" | "right";
}) {
  const active = sortBy === column;
  const Icon = !active ? ArrowUpDown : sortDir === "asc" ? ArrowUp : ArrowDown;
  return (
    <button
      type="button"
      onClick={() => onSort(column)}
      className={cn(
        "inline-flex items-center gap-1 font-medium hover:text-foreground",
        active ? "text-foreground" : "text-muted-foreground",
        align === "right" && "flex-row-reverse"
      )}
    >
      {label}
      <Icon className="h-3 w-3" />
    </button>
  );
}

function ProgressCell({ percent }: { percent: number }) {
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 w-16 shrink-0 overflow-hidden rounded-full bg-secondary/60">
        <div className="h-full rounded-full bg-primary" style={{ width: `${Math.min(100, Math.max(0, percent))}%` }} />
      </div>
      <span className="tabular-nums text-muted-foreground">{percent}%</span>
    </div>
  );
}

export default function CampaignsPage() {
  const [items, setItems] = useState<MailCampaignListItem[] | null>(null);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState<string | null>(null);

  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState<MailCampaignStatus | "all">("all");
  const [sortBy, setSortBy] = useState<MailCampaignListSortBy>("updated_at");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    listMailCampaignList({
      q: search.trim() || undefined,
      status: statusFilter === "all" ? undefined : statusFilter,
      sortBy,
      sortDir,
      page,
      pageSize,
    })
      .then((result) => {
        if (!cancelled) {
          setItems(result.items);
          setTotal(result.total);
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setError(
            err instanceof ApiError ? `Couldn't load campaigns (${err.status}): ${err.message}` : "Couldn't reach the backend."
          );
        }
      });
    return () => {
      cancelled = true;
    };
  }, [search, statusFilter, sortBy, sortDir, page, pageSize]);

  // Any filter/search/sort/page-size change resets to page 1 -- otherwise
  // a user could land on an empty page 4 of a now-3-page result.
  function updateFilter<T>(setter: (value: T) => void) {
    return (value: T) => {
      setter(value);
      setPage(1);
    };
  }

  function handleSort(column: MailCampaignListSortBy) {
    if (sortBy === column) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortBy(column);
      setSortDir(column === "name" ? "asc" : "desc");
    }
    setPage(1);
  }

  const totalPages = Math.max(1, Math.ceil(total / pageSize));

  return (
    <div className={MAIL_CAMPAIGN_DETAIL_CONTAINER_CLASS}>
      <div className="mb-6 flex items-start justify-between gap-4">
        <div>
          <h1 className="font-serif text-2xl font-medium tracking-tight sm:text-3xl">Campaigns</h1>
          <p className="mt-2 max-w-2xl text-sm text-muted-foreground">
            Create and manage your Astronomic Mail campaigns.
          </p>
        </div>
        <Link href="/manager/campaigns/new" className={cn(buttonVariants({ size: "sm" }), "shrink-0")}>
          <Plus className="h-4 w-4" />
          Create Campaign
        </Link>
      </div>

      {error && (
        <Alert variant="destructive" className="mb-6">
          <AlertTriangle />
          <AlertTitle>Couldn&apos;t load campaigns</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      <div className="mb-4 flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <h2 className="text-sm font-medium text-muted-foreground">Campaigns ({total})</h2>
        <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-center">
          <Input
            placeholder="Search campaign name…"
            value={search}
            onChange={(e) => updateFilter(setSearch)(e.target.value)}
            className="w-full min-w-0 sm:w-64"
          />
          <select
            value={statusFilter}
            onChange={(e) => updateFilter(setStatusFilter)(e.target.value as MailCampaignStatus | "all")}
            className="w-full min-w-0 rounded-md border border-input bg-background px-3 py-2 text-sm sm:w-40"
          >
            <option value="all">All statuses</option>
            {STATUS_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </div>
      </div>

      {!error && items === null && <div className="text-sm text-muted-foreground">Loading…</div>}

      {!error && items !== null && items.length === 0 && (
        <Card>
          <CardContent className="py-14 text-center">
            <div className="mx-auto mb-4 flex h-11 w-11 items-center justify-center rounded-2xl bg-secondary/60 text-muted-foreground">
              <Megaphone className="h-5 w-5" />
            </div>
            <p className="text-sm text-muted-foreground">
              {search || statusFilter !== "all" ? "No campaigns match these filters." : "No campaigns yet."}
            </p>
            {!search && statusFilter === "all" && (
              <Link href="/manager/campaigns/new" className={cn(buttonVariants({ size: "sm" }), "mt-4")}>
                Create a campaign
              </Link>
            )}
          </CardContent>
        </Card>
      )}

      {!error && items !== null && items.length > 0 && (
        <>
          <Card>
            <CardContent className="overflow-x-auto p-0">
              <table className="w-full min-w-[1000px] text-sm">
                <thead className="border-b border-border bg-secondary/30 text-xs">
                  <tr>
                    <th className="w-[240px] px-3 py-2 text-left">
                      <SortHeader label="Campaign" column="name" sortBy={sortBy} sortDir={sortDir} onSort={handleSort} />
                    </th>
                    <th className="px-3 py-2 text-left font-medium text-muted-foreground">Status</th>
                    <th className="px-3 py-2 text-left font-medium text-muted-foreground">Mailbox</th>
                    <th className="px-3 py-2 text-right">
                      <SortHeader
                        label="Leads"
                        column="total_leads"
                        sortBy={sortBy}
                        sortDir={sortDir}
                        onSort={handleSort}
                        align="right"
                      />
                    </th>
                    <th className="px-3 py-2 text-right font-medium text-muted-foreground">Sent</th>
                    <th className="px-3 py-2 text-right">
                      <SortHeader
                        label="Replied"
                        column="replied"
                        sortBy={sortBy}
                        sortDir={sortDir}
                        onSort={handleSort}
                        align="right"
                      />
                    </th>
                    <th className="px-3 py-2 text-right font-medium text-muted-foreground">Suppressed</th>
                    <th className="px-3 py-2 text-right font-medium text-muted-foreground">Failed</th>
                    <th className="px-3 py-2 text-left">
                      <SortHeader
                        label="Progress"
                        column="progress"
                        sortBy={sortBy}
                        sortDir={sortDir}
                        onSort={handleSort}
                      />
                    </th>
                    <th className="px-3 py-2 text-right font-medium text-muted-foreground">Steps</th>
                    <th className="px-3 py-2 text-right">
                      <SortHeader
                        label="Last Updated"
                        column="updated_at"
                        sortBy={sortBy}
                        sortDir={sortDir}
                        onSort={handleSort}
                        align="right"
                      />
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {items.map((campaign) => (
                    <tr key={campaign.mail_campaign_id} className="hover:bg-secondary/20">
                      <td className="w-[240px] max-w-[240px] p-0">
                        <Link
                          href={`/manager/campaigns/mail/${campaign.mail_campaign_id}`}
                          title={campaign.name}
                          className="block truncate whitespace-nowrap px-3 py-2 font-medium hover:underline"
                        >
                          {campaign.name}
                        </Link>
                      </td>
                      <td className="whitespace-nowrap px-3 py-2">
                        <span
                          className={cn(
                            "rounded-full px-2 py-0.5 text-xs font-medium",
                            mailCampaignStatusBadgeClass(campaign.status)
                          )}
                        >
                          {mailCampaignStatusLabel(campaign.status)}
                        </span>
                      </td>
                      <td className="whitespace-nowrap px-3 py-2 text-muted-foreground">
                        {campaign.mailbox_email ?? "—"}
                        {campaign.mailbox_count > 1 && (
                          <span className="ml-1 text-xs text-muted-foreground/70">+{campaign.mailbox_count - 1}</span>
                        )}
                      </td>
                      <td className="px-3 py-2 text-right tabular-nums text-muted-foreground">{campaign.total_leads}</td>
                      <td className="px-3 py-2 text-right tabular-nums text-muted-foreground">{campaign.sent}</td>
                      <td className="px-3 py-2 text-right tabular-nums text-muted-foreground">{campaign.replied}</td>
                      <td className="px-3 py-2 text-right tabular-nums text-muted-foreground">{campaign.suppressed}</td>
                      <td className="px-3 py-2 text-right tabular-nums text-muted-foreground">{campaign.failed}</td>
                      <td className="px-3 py-2">
                        <ProgressCell percent={campaign.progress_percent} />
                      </td>
                      <td className="px-3 py-2 text-right tabular-nums text-muted-foreground">{campaign.step_count}</td>
                      <td className="whitespace-nowrap px-3 py-2 text-right text-muted-foreground">{formatDateTime(campaign.updated_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </CardContent>
          </Card>

          <div className="mt-4 flex flex-col items-center justify-between gap-3 sm:flex-row">
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <span>
                Page {page} of {totalPages} · {total} total
              </span>
              <select
                value={pageSize}
                onChange={(e) => {
                  setPageSize(Number(e.target.value));
                  setPage(1);
                }}
                className="rounded-md border border-input bg-background px-2 py-1 text-xs"
              >
                {PAGE_SIZE_OPTIONS.map((size) => (
                  <option key={size} value={size}>
                    {size} / page
                  </option>
                ))}
              </select>
            </div>
            <div className="flex items-center gap-2">
              <button
                type="button"
                disabled={page <= 1}
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                className="rounded-md border border-input px-3 py-1.5 text-xs font-medium disabled:opacity-40"
              >
                Previous
              </button>
              <button
                type="button"
                disabled={page >= totalPages}
                onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                className="rounded-md border border-input px-3 py-1.5 text-xs font-medium disabled:opacity-40"
              >
                Next
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
