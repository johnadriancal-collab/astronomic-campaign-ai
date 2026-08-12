"use client";

import { useState } from "react";
import { ListChecks, Loader2, Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { describeBulkAddResult } from "@/lib/add-to-list";
import { ApiError, bulkAddToCrmList, createCrmList, listCrmLists, type CrmContactListSummary } from "@/lib/api";

/**
 * Self-contained "Add to List" trigger + picker -- dropped into Contacts,
 * More Filters, and Astro Search next to their existing Export buttons with
 * nothing but `selectedIds`. Owns its own open/loading/feedback state and
 * every API call (fetch lists, create a list, bulk-add) so none of the three
 * pages need to know anything about Lists beyond "here are the ids currently
 * selected" -- reusing the exact selection state those pages already built
 * for Export, never touching or clearing it itself.
 */
export function AddToListPanel({ selectedIds }: { selectedIds: string[] }) {
  const [open, setOpen] = useState(false);
  const [lists, setLists] = useState<CrmContactListSummary[] | null>(null);
  const [selectedListId, setSelectedListId] = useState("");
  const [creatingNew, setCreatingNew] = useState(false);
  const [newName, setNewName] = useState("");
  const [newDescription, setNewDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState<string | null>(null);

  function openPanel() {
    setOpen(true);
    setFeedback(null);
    listCrmLists().then(setLists).catch(() => setLists([]));
  }

  function closePanel() {
    setOpen(false);
    setFeedback(null);
    setCreatingNew(false);
    setNewName("");
    setNewDescription("");
    setSelectedListId("");
  }

  async function handleSubmit() {
    if (selectedIds.length === 0 || busy) return;
    setBusy(true);
    setFeedback(null);
    try {
      let listId = selectedListId;
      let listName = lists?.find((l) => l.list_id === selectedListId)?.name ?? "";

      if (creatingNew) {
        if (!newName.trim()) {
          setFeedback("Give the new list a name first.");
          setBusy(false);
          return;
        }
        const created = await createCrmList({ name: newName.trim(), description: newDescription.trim() || undefined });
        listId = created.list_id;
        listName = created.name;
      }

      if (!listId) {
        setFeedback("Choose a list, or create a new one.");
        setBusy(false);
        return;
      }

      const result = await bulkAddToCrmList(listId, selectedIds);
      setFeedback(describeBulkAddResult(result.added, result.already_member, listName));
      setCreatingNew(false);
      setNewName("");
      setNewDescription("");
      listCrmLists().then(setLists).catch(() => {});
    } catch (err) {
      setFeedback(err instanceof ApiError ? `Couldn't add to list (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="relative">
      <Button
        type="button"
        size="sm"
        variant="outline"
        onClick={() => (open ? closePanel() : openPanel())}
        disabled={selectedIds.length === 0}
        className="gap-1.5"
      >
        <ListChecks className="h-4 w-4" />
        Add to List{selectedIds.length > 0 ? ` (${selectedIds.length})` : ""}
      </Button>

      {open && (
        <div className="absolute right-0 z-20 mt-2 w-80 rounded-lg border border-border bg-card p-3 shadow-lg">
          {lists === null ? (
            <p className="text-sm text-muted-foreground">Loading lists…</p>
          ) : (
            <div className="flex flex-col gap-3">
              {!creatingNew ? (
                <div className="flex flex-col gap-1.5">
                  <span className="text-xs font-medium text-muted-foreground">Add to an existing list</span>
                  {lists.length === 0 ? (
                    <p className="text-sm text-muted-foreground">No lists yet -- create one below.</p>
                  ) : (
                    <select
                      value={selectedListId}
                      onChange={(e) => setSelectedListId(e.target.value)}
                      className="h-9 rounded-md border border-input bg-transparent px-2 text-sm"
                    >
                      <option value="">Select a list…</option>
                      {lists.map((l) => (
                        <option key={l.list_id} value={l.list_id}>
                          {l.name} ({l.contact_count})
                        </option>
                      ))}
                    </select>
                  )}
                  <button type="button" onClick={() => setCreatingNew(true)} className="self-start text-sm text-primary hover:underline">
                    + Create a new list instead
                  </button>
                </div>
              ) : (
                <div className="flex flex-col gap-2">
                  <span className="text-xs font-medium text-muted-foreground">New list</span>
                  <Input placeholder="List name" value={newName} onChange={(e) => setNewName(e.target.value)} />
                  <Textarea
                    placeholder="Description (optional)"
                    value={newDescription}
                    onChange={(e) => setNewDescription(e.target.value)}
                    className="min-h-9"
                  />
                  <button
                    type="button"
                    onClick={() => setCreatingNew(false)}
                    className="self-start text-sm text-muted-foreground hover:text-foreground"
                  >
                    Choose an existing list instead
                  </button>
                </div>
              )}

              {feedback && <p className="text-sm text-foreground">{feedback}</p>}

              <div className="flex justify-end gap-2">
                <Button type="button" size="sm" variant="ghost" onClick={closePanel}>
                  Close
                </Button>
                <Button type="button" size="sm" onClick={handleSubmit} disabled={busy} className="gap-1.5">
                  {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />}
                  Add
                </Button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
