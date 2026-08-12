"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { AlertTriangle, ListChecks, Plus } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { ApiError, createCrmList, listCrmLists, type CrmContactListSummary } from "@/lib/api";

function formatDate(iso: string): string {
  try {
    return new Date(iso).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  } catch {
    return iso;
  }
}

export default function CrmListsPage() {
  const [lists, setLists] = useState<CrmContactListSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  async function load() {
    try {
      setLists(await listCrmLists());
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? `Couldn't load lists (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    }
  }

  useEffect(() => {
    load();
  }, []);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;
    setCreating(true);
    setCreateError(null);
    try {
      await createCrmList({ name: name.trim(), description: description.trim() || undefined });
      setName("");
      setDescription("");
      await load();
    } catch (err) {
      setCreateError(err instanceof ApiError ? `Couldn't create list (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    } finally {
      setCreating(false);
    }
  }

  return (
    <div className="mx-auto max-w-3xl px-6 py-10">
      <div className="mb-6">
        <h1 className="mb-2 font-serif text-2xl font-medium tracking-tight">Lists</h1>
        <p className="text-sm text-muted-foreground">
          Named, persistent groupings of existing CRM contacts -- e.g. "Austin Family Offices" or "AI Investors". A
          contact can belong to any number of lists; adding or removing it from a list never edits the contact itself.
        </p>
      </div>

      {error && (
        <Alert variant="destructive" className="mb-4">
          <AlertTriangle />
          <AlertTitle>Couldn&apos;t load lists</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      <Card className="mb-6">
        <CardHeader>
          <CardTitle className="text-sm">New list</CardTitle>
        </CardHeader>
        <CardContent>
          {createError && (
            <Alert variant="destructive" className="mb-3">
              <AlertDescription>{createError}</AlertDescription>
            </Alert>
          )}
          <form onSubmit={handleCreate} className="grid gap-3">
            <div className="space-y-1">
              <label className="text-xs font-medium text-muted-foreground">Name</label>
              <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="Austin Family Offices" required />
            </div>
            <div className="space-y-1">
              <label className="text-xs font-medium text-muted-foreground">Description (optional)</label>
              <Textarea value={description} onChange={(e) => setDescription(e.target.value)} rows={2} />
            </div>
            <div>
              <Button type="submit" disabled={creating || !name.trim()} className="gap-1.5">
                <Plus className="h-4 w-4" />
                {creating ? "Creating..." : "Create list"}
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>

      {lists && (
        <div className="space-y-2">
          {lists.map((list) => (
            <Link key={list.list_id} href={`/crm/lists/${list.list_id}`}>
              <Card className="transition-colors hover:bg-secondary/40">
                <CardContent className="py-3">
                  <div className="flex items-center justify-between gap-3">
                    <div>
                      <p className="flex items-center gap-1.5 text-sm font-medium">
                        <ListChecks className="h-3.5 w-3.5 text-muted-foreground" />
                        {list.name}
                      </p>
                      {list.description && <p className="mt-1 text-xs text-muted-foreground">{list.description}</p>}
                      <p className="mt-1 text-xs text-muted-foreground">
                        created {formatDate(list.created_at)} -- updated {formatDate(list.updated_at)}
                      </p>
                    </div>
                    <div className="shrink-0 text-sm text-muted-foreground">
                      {list.contact_count} contact{list.contact_count === 1 ? "" : "s"}
                    </div>
                  </div>
                </CardContent>
              </Card>
            </Link>
          ))}
          {lists.length === 0 && <p className="text-sm text-muted-foreground">No lists yet -- create one above.</p>}
        </div>
      )}
    </div>
  );
}
