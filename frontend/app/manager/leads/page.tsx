"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { AlertTriangle, ArrowDown, ArrowUp, ArrowUpDown, MessageSquare, Users } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  ApiError,
  listMailCampaigns,
  listMailLeads,
  type MailCampaign,
  type MailEnrollmentStatus,
  type MailLeadListItem,
  type MailLeadSortBy,
} from "@/lib/api";
import { mailEnrollmentStatusBadgeClass, mailEnrollmentStatusLabel } from "@/lib/mail";
import { MAIL_CAMPAIGN_DETAIL_CONTAINER_CLASS } from "@/lib/mail-campaign-layout";
import { cn } from "@/lib/utils";

// Campaign Manager Leads V1 (2026-09-17). A "Lead" is a CRM contact
// with at least one real MailEnrollment -- never every CrmContact,
// never a search/prospecting result (see MailLeadsService's backend
// docstring). One row per crm_contact_id, aggregated across every
// campaign that contact has ever been enrolled in. Replaces this
// page's earlier Apollo-based content (that system's own data
// functions in lib/api.ts are left fully untouched) -- same route,
// real Campaign Manager data.
//
// Genuinely server-paginated (unlike the Inbox list or the CRM
// contacts page, which fetch everything and slice client-side) --
// every filter/sort/page change re-fetches from GET /mail/leads.

const STATUS_OPTIONS: { value: MailEnrollmentStatus; label: string }[] = [
  { value: "pending", label: "Pending" },
  { value: "active", label: "Active" },
  { value: "paused", label: "Paused" },
  { value: "completed", label: "Completed" },
  { value: "suppressed", label: "Suppressed" },
  { value: "failed", label: "Failed" },
  { value: "replied", label: "Replied" },
];

const PAGE_SIZE_OPTIONS = [25, 50];

function formatDateTime(iso: string): string {
  return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
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
  column: MailLeadSortBy;
  sortBy: MailLeadSortBy;
  sortDir: "asc" | "desc";
  onSort: (column: MailLeadSortBy) => void;
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

export default function LeadsPage() {
  const [items, setItems] = useState<MailLeadListItem[] | null>(null);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState<string | null>(null);

  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState<MailEnrollmentStatus | "all">("all");
  const [campaignFilter, setCampaignFilter] = useState<string>("all");
  const [repliedFilter, setRepliedFilter] = useState<"all" | "replied" | "not_replied">("all");
  const [sortBy, setSortBy] = useState<MailLeadSortBy>("last_activity");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);

  const [campaigns, setCampaigns] = useState<MailCampaign[]>([]);

  useEffect(() => {
    listMailCampaigns()
      .then(setCampaigns)
      .catch(() => {
        // The campaign filter is a convenience -- a failure here doesn't block the Leads list itself.
      });
  }, []);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    listMailLeads({
      q: search.trim() || undefined,
      status: statusFilter === "all" ? undefined : statusFilter,
      campaignId: campaignFilter === "all" ? undefined : campaignFilter,
      replied: repliedFilter === "all" ? undefined : repliedFilter === "replied",
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
          setError(err instanceof ApiError ? `Couldn't load Leads (${err.status}): ${err.message}` : "Couldn't reach the backend.");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [search, statusFilter, campaignFilter, repliedFilter, sortBy, sortDir, page, pageSize]);

  // Any filter/search/sort/page-size change resets to page 1 -- otherwise
  // a user could land on an empty page 4 of a now-3-page result.
  function updateFilter<T>(setter: (value: T) => void) {
    return (value: T) => {
      setter(value);
      setPage(1);
    };
  }

  function handleSort(column: MailLeadSortBy) {
    if (sortBy === column) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortBy(column);
      setSortDir(column === "name" || column === "last_campaign" ? "asc" : "desc");
    }
    setPage(1);
  }

  const totalPages = Math.max(1, Math.ceil(total / pageSize));

  return (
    <div className={MAIL_CAMPAIGN_DETAIL_CONTAINER_CLASS}>
      <div className="mb-6">
        <h1 className="font-serif text-2xl font-medium tracking-tight sm:text-3xl">Leads</h1>
        <p className="mt-2 max-w-2xl text-sm text-muted-foreground">
          Contacts who have actually been enrolled in an Astronomic Mail campaign, across every campaign they&apos;ve
          belonged to.
        </p>
      </div>

      {error && (
        <Alert variant="destructive" className="mb-6">
          <AlertTriangle />
          <AlertTitle>Couldn&apos;t load Leads</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      <div className="mb-4 flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <h2 className="text-sm font-medium text-muted-foreground">Leads ({total})</h2>
        <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-center">
          <Input
            placeholder="Search name, email, company, or campaign…"
            value={search}
            onChange={(e) => updateFilter(setSearch)(e.target.value)}
            className="w-full min-w-0 sm:w-64"
          />
          <select
            value={statusFilter}
            onChange={(e) => updateFilter(setStatusFilter)(e.target.value as MailEnrollmentStatus | "all")}
            className="w-full min-w-0 rounded-md border border-input bg-background px-3 py-2 text-sm sm:w-40"
          >
            <option value="all">All statuses</option>
            {STATUS_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
          {campaigns.length > 0 && (
            <select
              value={campaignFilter}
              onChange={(e) => updateFilter(setCampaignFilter)(e.target.value)}
              className="w-full min-w-0 rounded-md border border-input bg-background px-3 py-2 text-sm sm:w-56"
            >
              <option value="all">All campaigns</option>
              {campaigns.map((c) => (
                <option key={c.mail_campaign_id} value={c.mail_campaign_id}>
                  {c.name}
                </option>
              ))}
            </select>
          )}
          <select
            value={repliedFilter}
            onChange={(e) => updateFilter(setRepliedFilter)(e.target.value as "all" | "replied" | "not_replied")}
            className="w-full min-w-0 rounded-md border border-input bg-background px-3 py-2 text-sm sm:w-40"
          >
            <option value="all">Replied or not</option>
            <option value="replied">Replied</option>
            <option value="not_replied">Not replied</option>
          </select>
        </div>
      </div>

      {!error && items === null && <div className="text-sm text-muted-foreground">Loading…</div>}

      {!error && items !== null && items.length === 0 && (
        <Card>
          <CardContent className="py-14 text-center">
            <div className="mx-auto mb-4 flex h-11 w-11 items-center justify-center rounded-2xl bg-secondary/60 text-muted-foreground">
              <Users className="h-5 w-5" />
            </div>
            <p className="text-sm text-muted-foreground">
              {search || statusFilter !== "all" || campaignFilter !== "all" || repliedFilter !== "all"
                ? "No leads match these filters."
                : "No leads yet. A contact becomes a Lead once a campaign enrolls them."}
            </p>
          </CardContent>
        </Card>
      )}

      {!error && items !== null && items.length > 0 && (
        <>
          <Card>
            <CardContent className="overflow-x-auto p-0">
              <table className="w-full min-w-[900px] text-sm">
                <thead className="border-b border-border bg-secondary/30 text-xs">
                  <tr>
                    <th className="px-4 py-2.5 text-left">
                      <SortHeader label="Name" column="name" sortBy={sortBy} sortDir={sortDir} onSort={handleSort} />
                    </th>
                    <th className="px-4 py-2.5 text-left font-medium text-muted-foreground">Email</th>
                    <th className="px-4 py-2.5 text-left font-medium text-muted-foreground">Company</th>
                    <th className="px-4 py-2.5 text-left font-medium text-muted-foreground">Title</th>
                    <th className="px-4 py-2.5 text-left font-medium text-muted-foreground">Status</th>
                    <th className="px-4 py-2.5 text-left">
                      <SortHeader
                        label="Last Campaign"
                        column="last_campaign"
                        sortBy={sortBy}
                        sortDir={sortDir}
                        onSort={handleSort}
                      />
                    </th>
                    <th className="px-4 py-2.5 text-right font-medium text-muted-foreground">Campaigns</th>
                    <th className="px-4 py-2.5 text-right">
                      <SortHeader
                        label="Last Activity"
                        column="last_activity"
                        sortBy={sortBy}
                        sortDir={sortDir}
                        onSort={handleSort}
                        align="right"
                      />
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {items.map((lead) => (
                    <tr key={lead.crm_contact_id} className="hover:bg-secondary/20">
                      <td className="p-0">
                        <Link
                          href={`/manager/leads/${lead.crm_contact_id}`}
                          className="flex items-center gap-1.5 px-4 py-2.5 font-medium hover:underline"
                        >
                          {lead.name ?? lead.email ?? "(unknown)"}
                          {lead.replied && (
                            <MessageSquare className="h-3.5 w-3.5 shrink-0 text-emerald-700" aria-label="Has replied" />
                          )}
                        </Link>
                      </td>
                      <td className="px-4 py-2.5 text-muted-foreground">{lead.email ?? "—"}</td>
                      <td className="px-4 py-2.5 text-muted-foreground">{lead.company ?? "—"}</td>
                      <td className="px-4 py-2.5 text-muted-foreground">{lead.title ?? "—"}</td>
                      <td className="px-4 py-2.5">
                        <span
                          className={cn(
                            "rounded-full px-2 py-0.5 text-xs font-medium",
                            mailEnrollmentStatusBadgeClass(lead.status)
                          )}
                        >
                          {mailEnrollmentStatusLabel(lead.status)}
                        </span>
                      </td>
                      <td className="px-4 py-2.5">
                        <Link href={`/manager/campaigns/mail/${lead.last_campaign_id}`} className="hover:underline">
                          {lead.last_campaign_name}
                        </Link>
                      </td>
                      <td className="px-4 py-2.5 text-right text-muted-foreground">{lead.campaigns_count}</td>
                      <td className="px-4 py-2.5 text-right text-muted-foreground">
                        {formatDateTime(lead.last_activity_at)}
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
