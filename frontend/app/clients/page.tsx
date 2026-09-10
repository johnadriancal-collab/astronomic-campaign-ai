"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { AlertTriangle, Plus, Search } from "lucide-react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { ClientFormModal } from "@/components/client-form-modal";
import {
  ApiError,
  listClients,
  type Client,
  type ClientPage,
  type ClientRelationshipClassification,
  type ClientStatus,
} from "@/lib/api";
import {
  CLIENT_RELATIONSHIP_CLASSIFICATION_OPTIONS,
  CLIENT_STATUS_OPTIONS,
  buildClientListQueryParams,
  clientRelationshipClassificationBadgeClass,
  clientRelationshipClassificationLabel,
  clientStatusBadgeClass,
  clientStatusLabel,
  defaultClientListFilters,
  formatClientDate,
  type ClientListFilters,
} from "@/lib/client-crm";

export default function ClientsPage() {
  const router = useRouter();
  const [filters, setFilters] = useState<ClientListFilters>(defaultClientListFilters());
  const [searchInput, setSearchInput] = useState("");
  const [ownerInput, setOwnerInput] = useState("");
  const [page, setPage] = useState<ClientPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);

  async function load(next: ClientListFilters) {
    setPage(null); // loading state -- distinct from "loaded, zero results"
    try {
      const result = await listClients(buildClientListQueryParams(next));
      setPage(result);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? `Couldn't load Clients (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    }
  }

  useEffect(() => {
    load(filters);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filters]);

  // Search/owner are Enter-to-search (or the Search button), matching the
  // existing Contacts page's own convention (/crm) -- no live-as-you-type
  // debounce exists anywhere in this codebase to match instead (see this
  // stage's own investigation).
  function runSearch() {
    setFilters((prev) => ({ ...prev, q: searchInput, owner: ownerInput, page: 1 }));
  }

  function applyStatus(value: string) {
    setFilters((prev) => ({ ...prev, status: value as ClientStatus | "", page: 1 }));
  }

  function applyRelationship(value: string) {
    setFilters((prev) => ({ ...prev, relationshipClassification: value as ClientRelationshipClassification | "", page: 1 }));
  }

  function toggleIncludeArchived(next: boolean) {
    setFilters((prev) => ({ ...prev, includeArchived: next, page: 1 }));
  }

  function goToPage(nextPage: number) {
    setFilters((prev) => ({ ...prev, page: nextPage }));
  }

  function handleCreated(client: Client) {
    // Existing app precedent (Campaign creation) navigates straight to the
    // new record's own detail page rather than just refreshing the list.
    router.push(`/clients/${client.client_id}`);
  }

  const hasActiveFilters = Boolean(filters.q || filters.status || filters.relationshipClassification || filters.owner);
  const totalPages = page ? Math.max(1, Math.ceil(page.total / page.page_size)) : 1;

  return (
    <div className="mx-auto max-w-5xl px-6 py-10">
      <div className="mb-8 flex items-start justify-between gap-4">
        <div>
          <h1 className="font-serif text-2xl font-medium tracking-tight sm:text-3xl">Client CRM</h1>
          <p className="mt-2 max-w-2xl text-sm text-muted-foreground">
            Astronomic&apos;s relationship and sales record for organizations -- separate from Contacts, Astronomic&apos;s
            database of people.
          </p>
        </div>
        <Button size="sm" className="shrink-0 gap-1.5" onClick={() => setCreateOpen(true)}>
          <Plus className="h-4 w-4" />
          New Client
        </Button>
      </div>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          runSearch();
        }}
        className="mb-3 grid gap-2 sm:grid-cols-4"
      >
        <div className="relative sm:col-span-1">
          <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
          <Input
            placeholder="Search Clients..."
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && runSearch()}
            className="pl-8"
          />
        </div>
        <select
          value={filters.status}
          onChange={(e) => applyStatus(e.target.value)}
          className="h-9 rounded-md border border-input bg-transparent px-3 text-sm"
        >
          <option value="">Any status</option>
          {CLIENT_STATUS_OPTIONS.map((s) => (
            <option key={s.value} value={s.value}>
              {s.label}
            </option>
          ))}
        </select>
        <select
          value={filters.relationshipClassification}
          onChange={(e) => applyRelationship(e.target.value)}
          className="h-9 rounded-md border border-input bg-transparent px-3 text-sm"
        >
          <option value="">Any relationship</option>
          {CLIENT_RELATIONSHIP_CLASSIFICATION_OPTIONS.map((c) => (
            <option key={c.value} value={c.value}>
              {c.label}
            </option>
          ))}
        </select>
        <Input
          placeholder="Owner"
          value={ownerInput}
          onChange={(e) => setOwnerInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && runSearch()}
        />
        <button type="submit" className="hidden" />
      </form>

      <label className="mb-4 flex items-center gap-2 text-xs text-muted-foreground">
        <input
          type="checkbox"
          checked={filters.includeArchived}
          onChange={(e) => toggleIncludeArchived(e.target.checked)}
          className="h-3.5 w-3.5 rounded border-input"
        />
        Show archived Clients
      </label>

      {error && (
        <Alert variant="destructive" className="mb-4">
          <AlertTriangle />
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {!error && page === null && (
        <div className="space-y-2">
          {Array.from({ length: 5 }).map((_, i) => (
            <Skeleton key={i} className="h-12 rounded-lg" />
          ))}
        </div>
      )}

      {!error && page !== null && page.items.length === 0 && (
        <div className="flex flex-col items-center gap-3 rounded-xl border border-dashed border-border/60 py-16 text-center">
          <p className="font-medium">{hasActiveFilters ? "No Clients match these filters" : "No Clients yet"}</p>
          <p className="max-w-sm text-sm text-muted-foreground">
            {hasActiveFilters
              ? "Try a different search or clear your filters."
              : "Add your first organization to start tracking the relationship."}
          </p>
          {!hasActiveFilters && (
            <Button size="sm" className="mt-1 gap-1.5" onClick={() => setCreateOpen(true)}>
              <Plus className="h-4 w-4" />
              New Client
            </Button>
          )}
        </div>
      )}

      {!error && page !== null && page.items.length > 0 && (
        <>
          <div className="overflow-x-auto rounded-xl border border-border/60">
            <table className="w-full text-sm">
              <thead className="bg-secondary/40 text-xs text-muted-foreground">
                <tr>
                  <th className="px-3 py-2 text-left font-medium">Client</th>
                  <th className="px-3 py-2 text-left font-medium">Status</th>
                  <th className="px-3 py-2 text-left font-medium">Relationship</th>
                  <th className="px-3 py-2 text-left font-medium">Owner</th>
                  <th className="px-3 py-2 text-left font-medium">Next Dinner</th>
                  <th className="px-3 py-2 text-left font-medium">Last Contacted</th>
                  <th className="px-3 py-2 text-left font-medium">Next Action</th>
                  <th className="px-3 py-2 text-left font-medium">Next Action Due</th>
                  <th className="px-3 py-2 text-left font-medium">Updated</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border/60">
                {page.items.map((client) => (
                  <tr key={client.client_id} className="hover:bg-secondary/30">
                    <td className="px-3 py-2.5 font-medium">
                      <Link href={`/clients/${client.client_id}`} className="hover:underline">
                        {client.name}
                      </Link>
                      {client.archived && (
                        <Badge variant="secondary" className="ml-2 bg-secondary text-muted-foreground">
                          Archived
                        </Badge>
                      )}
                    </td>
                    <td className="px-3 py-2.5">
                      <Badge variant="secondary" className={clientStatusBadgeClass(client.status)}>
                        {clientStatusLabel(client.status)}
                      </Badge>
                    </td>
                    <td className="px-3 py-2.5">
                      {client.relationship_classification ? (
                        <Badge variant="secondary" className={clientRelationshipClassificationBadgeClass(client.relationship_classification)}>
                          {clientRelationshipClassificationLabel(client.relationship_classification)}
                        </Badge>
                      ) : (
                        <span className="text-muted-foreground">—</span>
                      )}
                    </td>
                    <td className="px-3 py-2.5 text-muted-foreground">{client.owner || "—"}</td>
                    <td className="px-3 py-2.5 text-muted-foreground">{formatClientDate(client.next_dinner)}</td>
                    <td className="px-3 py-2.5 text-muted-foreground">{formatClientDate(client.last_contacted)}</td>
                    <td className="px-3 py-2.5 text-muted-foreground">{client.next_action || "—"}</td>
                    <td className="px-3 py-2.5 text-muted-foreground">{formatClientDate(client.next_action_due)}</td>
                    <td className="px-3 py-2.5 text-muted-foreground">{formatClientDate(client.updated_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="mt-4 flex items-center justify-between text-sm">
            <p className="text-muted-foreground">
              {page.total} Client{page.total === 1 ? "" : "s"}
            </p>
            {totalPages > 1 && (
              <div className="flex items-center gap-3">
                <span className="text-muted-foreground">
                  Page {page.page} of {totalPages}
                </span>
                <Button size="sm" variant="outline" onClick={() => goToPage(page.page - 1)} disabled={page.page <= 1}>
                  Previous
                </Button>
                <Button size="sm" variant="outline" onClick={() => goToPage(page.page + 1)} disabled={page.page >= totalPages}>
                  Next
                </Button>
              </div>
            )}
          </div>
        </>
      )}

      <ClientFormModal open={createOpen} onOpenChange={setCreateOpen} existingClient={null} onSaved={handleCreated} />
    </div>
  );
}
