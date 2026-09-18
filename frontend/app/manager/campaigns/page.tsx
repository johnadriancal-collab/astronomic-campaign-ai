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
import { mailCampaignStatusBadgeClass, mailCampaignStatusLabel, OPEN_RATE_TOOLTIP } from "@/lib/mail";
import { MAIL_CAMPAIGN_DETAIL_CONTAINER_CLASS } from "@/lib/mail-campaign-layout";
import { cn } from "@/lib/utils";

// Campaign Manager Campaigns V1 (2026-09-17), lead-start progress
// redefinition (2026-09-18), sequence-completion progress redefinition
// (2026-09-18b) -- one row per campaign, wide table -- see
// MailCampaignListService's backend docstring for exactly what each row
// aggregates and MailCampaignListItem's own docstring for the
// available_leads/finished_leads/progress_percent/reply_rate_percent
// definitions. Every count here is real workload/step data already
// tracked elsewhere; this view shows nothing that isn't genuinely
// measured anywhere in this system -- Open rate is the one exception,
// which is why it's a static "not tracked" cell rather than a field on
// the model at all.
//
// Available (lead-start) and Progress (sequence-completion) are
// deliberately SEPARATE concepts, never conflated: Available answers
// "has outreach started," Progress answers "has the whole sequence
// finished" -- see ProgressCell's own comment for exactly what counts
// as finished and why.
//
// Mailbox and Sent were dropped from this table (2026-09-18): Mailbox is
// a Channels-tab concept now, and Sent is superseded by Available/
// Progress.
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
// single-line at the tighter row height below. The full exact timestamp
// is still available as a title tooltip on the cell that renders this.
function formatDateTime(iso: string): string {
  return new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

// Campaign created is a DATE, not a date+time -- deliberately distinct
// from Last Updated's compact date+time format above. Includes the year
// since a campaign's creation date is worth being unambiguous about
// longer than its last-updated time is.
function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

function exactTimestamp(iso: string): string {
  return new Date(iso).toLocaleString();
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

// Progress (2026-09-18b redefinition) -- colored = finished the
// sequence (finished_leads), neutral = still in progress
// (in_progress_leads), i.e. total_leads - finished_leads. No percentage
// text next to the bar any more -- the exact counts live in a compact
// native-tooltip hover instead (QuickMail-style "N leads in progress").
// A long bar (roughly double the original width) carries the visual
// weight on its own.
function ProgressCell({ finished, inProgress, total }: { finished: number; inProgress: number; total: number }) {
  const percent = total > 0 ? (finished / total) * 100 : 0;
  const tooltip = `${inProgress} leads in progress\n${finished} completed of ${total} total`;
  return (
    <div className="h-2 w-32 shrink-0 overflow-hidden rounded-full bg-secondary/60" title={tooltip}>
      <div className="h-full rounded-full bg-primary" style={{ width: `${Math.min(100, Math.max(0, percent))}%` }} />
    </div>
  );
}

// Narrow/pinned columns, same density standard established on the
// Emails/Leads/Inbox lists -- keyed by header label so header and body
// width stay in lockstep without duplicating the column list itself.
// Campaign is deliberately absent here -- it keeps its own wider,
// truncating w-[240px] treatment below, same as before. Progress is
// roughly double its original w-[150px] to give the longer bar real
// room (2026-09-18b).
const COLUMN_WIDTH_CLASS: Record<string, string> = {
  Status: "w-[90px]",
  Available: "w-[80px]",
  Total: "w-[70px]",
  Progress: "w-[300px]",
  "Open rate": "w-[80px]",
  "Reply rate": "w-[80px]",
  Replied: "w-[70px]",
  Suppressed: "w-[90px]",
  Failed: "w-[70px]",
  Steps: "w-[60px]",
  "Campaign created": "w-[120px]",
  "Last updated": "w-[130px]",
};

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
        <h2 className="text-sm font-medium text-muted-foreground">Total campaigns: {total}</h2>
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
              <table className="w-full min-w-[1550px] text-sm">
                <thead className="border-b border-border bg-secondary/30 text-xs">
                  <tr>
                    <th className={cn("px-3 py-2 text-left", COLUMN_WIDTH_CLASS.Status)}>
                      <SortHeader label="Status" column="status" sortBy={sortBy} sortDir={sortDir} onSort={handleSort} />
                    </th>
                    <th className="w-[240px] px-3 py-2 text-left">
                      <SortHeader label="Campaign" column="name" sortBy={sortBy} sortDir={sortDir} onSort={handleSort} />
                    </th>
                    <th className={cn("px-3 py-2 text-right", COLUMN_WIDTH_CLASS.Available)}>
                      <SortHeader
                        label="Available"
                        column="available"
                        sortBy={sortBy}
                        sortDir={sortDir}
                        onSort={handleSort}
                        align="right"
                      />
                    </th>
                    <th className={cn("px-3 py-2 text-right", COLUMN_WIDTH_CLASS.Total)}>
                      <SortHeader
                        label="Total"
                        column="total_leads"
                        sortBy={sortBy}
                        sortDir={sortDir}
                        onSort={handleSort}
                        align="right"
                      />
                    </th>
                    <th className={cn("px-3 py-2 text-left", COLUMN_WIDTH_CLASS.Progress)}>
                      <SortHeader label="Progress" column="progress" sortBy={sortBy} sortDir={sortDir} onSort={handleSort} />
                    </th>
                    <th className={cn("px-3 py-2 text-right font-medium text-muted-foreground", COLUMN_WIDTH_CLASS["Open rate"])}>
                      Open rate
                    </th>
                    <th className={cn("px-3 py-2 text-right", COLUMN_WIDTH_CLASS["Reply rate"])}>
                      <SortHeader
                        label="Reply rate"
                        column="reply_rate"
                        sortBy={sortBy}
                        sortDir={sortDir}
                        onSort={handleSort}
                        align="right"
                      />
                    </th>
                    <th className={cn("px-3 py-2 text-right", COLUMN_WIDTH_CLASS.Replied)}>
                      <SortHeader
                        label="Replied"
                        column="replied"
                        sortBy={sortBy}
                        sortDir={sortDir}
                        onSort={handleSort}
                        align="right"
                      />
                    </th>
                    <th className={cn("px-3 py-2 text-right font-medium text-muted-foreground", COLUMN_WIDTH_CLASS.Suppressed)}>
                      Suppressed
                    </th>
                    <th className={cn("px-3 py-2 text-right font-medium text-muted-foreground", COLUMN_WIDTH_CLASS.Failed)}>
                      Failed
                    </th>
                    <th className={cn("px-3 py-2 text-right font-medium text-muted-foreground", COLUMN_WIDTH_CLASS.Steps)}>
                      Steps
                    </th>
                    <th className={cn("px-3 py-2 text-right", COLUMN_WIDTH_CLASS["Campaign created"])}>
                      <SortHeader
                        label="Campaign created"
                        column="created_at"
                        sortBy={sortBy}
                        sortDir={sortDir}
                        onSort={handleSort}
                        align="right"
                      />
                    </th>
                    <th className={cn("px-3 py-2 text-right", COLUMN_WIDTH_CLASS["Last updated"])}>
                      <SortHeader
                        label="Last updated"
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
                      <td className={cn("whitespace-nowrap px-3 py-2", COLUMN_WIDTH_CLASS.Status)}>
                        <span
                          className={cn(
                            "rounded-full px-2 py-0.5 text-xs font-medium",
                            mailCampaignStatusBadgeClass(campaign.status)
                          )}
                        >
                          {mailCampaignStatusLabel(campaign.status)}
                        </span>
                      </td>
                      <td className="w-[240px] max-w-[240px] p-0">
                        <Link
                          href={`/manager/campaigns/mail/${campaign.mail_campaign_id}`}
                          title={campaign.name}
                          className="block truncate whitespace-nowrap px-3 py-2 font-medium hover:underline"
                        >
                          {campaign.name}
                        </Link>
                      </td>
                      <td className={cn("px-3 py-2 text-right tabular-nums text-muted-foreground", COLUMN_WIDTH_CLASS.Available)}>
                        {campaign.available_leads}
                      </td>
                      <td className={cn("px-3 py-2 text-right tabular-nums text-muted-foreground", COLUMN_WIDTH_CLASS.Total)}>
                        {campaign.total_leads}
                      </td>
                      <td className={cn("px-3 py-2", COLUMN_WIDTH_CLASS.Progress)}>
                        <ProgressCell
                          finished={campaign.finished_leads}
                          inProgress={campaign.in_progress_leads}
                          total={campaign.total_leads}
                        />
                      </td>
                      <td
                        className={cn("whitespace-nowrap px-3 py-2 text-right text-muted-foreground", COLUMN_WIDTH_CLASS["Open rate"])}
                        title={OPEN_RATE_TOOLTIP}
                      >
                        —
                      </td>
                      <td className={cn("px-3 py-2 text-right tabular-nums text-muted-foreground", COLUMN_WIDTH_CLASS["Reply rate"])}>
                        {campaign.reply_rate_percent}%
                      </td>
                      <td className={cn("px-3 py-2 text-right tabular-nums text-muted-foreground", COLUMN_WIDTH_CLASS.Replied)}>
                        {campaign.replied}
                      </td>
                      <td className={cn("px-3 py-2 text-right tabular-nums text-muted-foreground", COLUMN_WIDTH_CLASS.Suppressed)}>
                        {campaign.suppressed}
                      </td>
                      <td className={cn("px-3 py-2 text-right tabular-nums text-muted-foreground", COLUMN_WIDTH_CLASS.Failed)}>
                        {campaign.failed}
                      </td>
                      <td className={cn("px-3 py-2 text-right tabular-nums text-muted-foreground", COLUMN_WIDTH_CLASS.Steps)}>
                        {campaign.step_count}
                      </td>
                      <td
                        className={cn("whitespace-nowrap px-3 py-2 text-right text-muted-foreground", COLUMN_WIDTH_CLASS["Campaign created"])}
                        title={exactTimestamp(campaign.created_at)}
                      >
                        {formatDate(campaign.created_at)}
                      </td>
                      <td
                        className={cn("whitespace-nowrap px-3 py-2 text-right text-muted-foreground", COLUMN_WIDTH_CLASS["Last updated"])}
                        title={exactTimestamp(campaign.updated_at)}
                      >
                        {formatDateTime(campaign.updated_at)}
                      </td>
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
