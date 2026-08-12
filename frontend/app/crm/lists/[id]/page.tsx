"use client";

import { useEffect, useRef, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import { AlertTriangle, ArrowLeft, Download, ListChecks, Pencil, Trash2, X } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button, buttonVariants } from "@/components/ui/button";
import { ContactResults } from "@/components/crm-contact-results";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  ApiError,
  bulkRemoveFromCrmList,
  deleteCrmList,
  getCrmList,
  listCrmContactExportFields,
  listCrmCustomFields,
  listCrmListContacts,
  updateCrmList,
  type CrmContact,
  type CrmContactExportField,
  type CrmContactListSummary,
  type CrmCustomFieldDefinition,
} from "@/lib/api";
import { clearSelection, isPageFullySelected, isPagePartiallySelected, selectAllMatching, toggleOne, toggleSelectAllOnPage } from "@/lib/contact-selection";
import { fetchAllListContacts, resolveContactsForExport } from "@/lib/crm-bulk-selection";
import { buildCsv, buildExportColumns, downloadCsv, exportFilename } from "@/lib/csv-export";
import { cn } from "@/lib/utils";

export default function CrmListDetailPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const listId = params.id;

  const [list, setList] = useState<CrmContactListSummary | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [editName, setEditName] = useState("");
  const [editDescription, setEditDescription] = useState("");
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);

  const [contacts, setContacts] = useState<CrmContact[] | null>(null);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const [contactsError, setContactsError] = useState<string | null>(null);

  const [selected, setSelected] = useState<Set<string>>(new Set());
  // Every contact currently a member of this list, fetched only when the
  // user asks for "Select all N matching"/"Export all matching" -- normal
  // browsing stays server-paginated (`contacts` above holds just one page).
  const [matchingContacts, setMatchingContacts] = useState<CrmContact[] | null>(null);
  const [bulkBusy, setBulkBusy] = useState(false);
  const [exportFields, setExportFields] = useState<CrmContactExportField[] | null>(null);
  const [customFields, setCustomFields] = useState<CrmCustomFieldDefinition[] | null>(null);
  const selectAllCheckboxRef = useRef<HTMLInputElement>(null);

  async function loadList() {
    try {
      const summary = await getCrmList(listId);
      setList(summary);
      setListError(null);
    } catch (err) {
      setListError(err instanceof ApiError ? `Couldn't load this list (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    }
  }

  async function loadContacts(nextPage: number, nextPageSize: number) {
    try {
      const result = await listCrmListContacts(listId, { page: nextPage, page_size: nextPageSize });
      setContacts(result.items);
      setTotal(result.total);
      setContactsError(null);
    } catch (err) {
      setContactsError(err instanceof ApiError ? `Couldn't load contacts (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    }
  }

  useEffect(() => {
    loadList();
    loadContacts(1, 50);
    listCrmContactExportFields().then(setExportFields).catch(() => setExportFields([]));
    listCrmCustomFields(false).then(setCustomFields).catch(() => setCustomFields([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [listId]);

  function goToPage(nextPage: number) {
    setPage(nextPage);
    loadContacts(nextPage, pageSize);
  }

  function changePageSize(nextPageSize: number) {
    setPageSize(nextPageSize);
    setPage(1);
    loadContacts(1, nextPageSize);
  }

  const pageIds = contacts?.map((c) => c.crm_contact_id) ?? [];
  const allSelectedOnPage = isPageFullySelected(selected, pageIds);
  const somePartiallySelectedOnPage = isPagePartiallySelected(selected, pageIds);

  useEffect(() => {
    if (selectAllCheckboxRef.current) {
      selectAllCheckboxRef.current.indeterminate = somePartiallySelectedOnPage;
    }
  }, [somePartiallySelectedOnPage]);

  function startEditing() {
    if (!list) return;
    setEditName(list.name);
    setEditDescription(list.description ?? "");
    setEditing(true);
  }

  async function handleSaveEdit() {
    if (!list || !editName.trim()) return;
    setSaving(true);
    try {
      const updated = await updateCrmList(listId, { name: editName.trim(), description: editDescription.trim() || undefined });
      setList(updated);
      setEditing(false);
    } catch (err) {
      setListError(err instanceof ApiError ? `Couldn't save (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete() {
    if (!list) return;
    const confirmed = window.confirm(
      `Delete "${list.name}"? This will delete the list and its memberships. The ${list.contact_count} CRM ` +
        `contact${list.contact_count === 1 ? "" : "s"} will not be deleted.`
    );
    if (!confirmed) return;
    setDeleting(true);
    try {
      await deleteCrmList(listId);
      router.push("/crm/lists");
    } catch (err) {
      setListError(err instanceof ApiError ? `Couldn't delete this list (${err.status}): ${err.message}` : "Couldn't reach the backend.");
      setDeleting(false);
    }
  }

  async function selectAllInList() {
    if (bulkBusy) return;
    setBulkBusy(true);
    try {
      const all = await fetchAllListContacts(listId, listCrmListContacts);
      setMatchingContacts(all);
      setSelected(selectAllMatching(all.map((c) => c.crm_contact_id)));
    } catch (err) {
      setContactsError(err instanceof ApiError ? `Couldn't select all contacts (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    } finally {
      setBulkBusy(false);
    }
  }

  async function handleExportSelected() {
    if (!exportFields || !customFields || selected.size === 0 || bulkBusy) return;
    setBulkBusy(true);
    try {
      const knownContacts = matchingContacts ?? contacts ?? [];
      const selectedContacts = await resolveContactsForExport(selected, knownContacts, () => fetchAllListContacts(listId, listCrmListContacts));
      const columns = buildExportColumns(exportFields, customFields);
      downloadCsv(buildCsv(columns, selectedContacts), exportFilename(new Date()));
    } catch (err) {
      setContactsError(err instanceof ApiError ? `Couldn't export contacts (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    } finally {
      setBulkBusy(false);
    }
  }

  async function handleExportAllMatching() {
    if (!exportFields || !customFields || total === 0 || bulkBusy) return;
    setBulkBusy(true);
    try {
      const all = matchingContacts ?? (await fetchAllListContacts(listId, listCrmListContacts));
      const columns = buildExportColumns(exportFields, customFields);
      downloadCsv(buildCsv(columns, all), exportFilename(new Date()));
    } catch (err) {
      setContactsError(err instanceof ApiError ? `Couldn't export contacts (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    } finally {
      setBulkBusy(false);
    }
  }

  async function handleRemoveSelected() {
    if (selected.size === 0 || bulkBusy) return;
    setBulkBusy(true);
    try {
      await bulkRemoveFromCrmList(listId, [...selected]);
      setSelected(clearSelection());
      setMatchingContacts(null);
      await Promise.all([loadList(), loadContacts(page, pageSize)]);
    } catch (err) {
      setContactsError(err instanceof ApiError ? `Couldn't remove contacts (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    } finally {
      setBulkBusy(false);
    }
  }

  if (listError && !list) {
    return (
      <div className="mx-auto max-w-5xl px-6 py-10">
        <Alert variant="destructive">
          <AlertTriangle />
          <AlertTitle>Couldn&apos;t load this list</AlertTitle>
          <AlertDescription>{listError}</AlertDescription>
        </Alert>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-5xl px-6 py-10">
      <Link href="/crm/lists" className="mb-4 inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground">
        <ArrowLeft className="h-3.5 w-3.5" />
        Back to Lists
      </Link>

      <div className="mb-8 flex items-start justify-between gap-4">
        <div className="min-w-0">
          {!list ? (
            <p className="text-sm text-muted-foreground">Loading…</p>
          ) : !editing ? (
            <>
              <h1 className="flex items-center gap-2 font-serif text-2xl font-medium tracking-tight sm:text-3xl">
                <ListChecks className="h-5 w-5 text-muted-foreground" />
                {list.name}
              </h1>
              {list.description && <p className="mt-2 max-w-2xl text-sm text-muted-foreground">{list.description}</p>}
              <p className="mt-2 text-sm text-muted-foreground">{list.contact_count} contacts</p>
            </>
          ) : (
            <div className="flex flex-col gap-2 sm:max-w-md">
              <Input value={editName} onChange={(e) => setEditName(e.target.value)} placeholder="List name" />
              <Textarea value={editDescription} onChange={(e) => setEditDescription(e.target.value)} placeholder="Description (optional)" rows={2} />
              <div className="flex gap-2">
                <Button size="sm" onClick={handleSaveEdit} disabled={saving || !editName.trim()}>
                  {saving ? "Saving..." : "Save"}
                </Button>
                <Button size="sm" variant="outline" onClick={() => setEditing(false)} disabled={saving}>
                  Cancel
                </Button>
              </div>
            </div>
          )}
        </div>
        {list && !editing && (
          <div className="flex shrink-0 gap-2">
            <Button size="sm" variant="outline" className="gap-1.5" onClick={startEditing}>
              <Pencil className="h-3.5 w-3.5" />
              Edit
            </Button>
            <Button size="sm" variant="destructive" className="gap-1.5" onClick={handleDelete} disabled={deleting}>
              <Trash2 className="h-3.5 w-3.5" />
              {deleting ? "Deleting..." : "Delete list"}
            </Button>
          </div>
        )}
      </div>

      {listError && list && (
        <Alert variant="destructive" className="mb-4">
          <AlertDescription>{listError}</AlertDescription>
        </Alert>
      )}

      {list && (
        <>
          <div className="mb-3 flex flex-wrap items-center justify-end gap-2">
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={handleRemoveSelected}
              disabled={selected.size === 0 || bulkBusy}
              className="gap-1.5"
            >
              <X className="h-4 w-4" />
              Remove selected{selected.size > 0 ? ` (${selected.size})` : ""}
            </Button>
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={handleExportSelected}
              disabled={selected.size === 0 || bulkBusy}
              className="gap-1.5"
            >
              <Download className="h-4 w-4" />
              Export selected{selected.size > 0 ? ` (${selected.size})` : ""}
            </Button>
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={handleExportAllMatching}
              disabled={total === 0 || bulkBusy}
              className="gap-1.5"
            >
              <Download className="h-4 w-4" />
              Export entire list
            </Button>
          </div>

          <ContactResults
            contacts={contacts}
            total={total}
            page={page}
            pageSize={pageSize}
            error={contactsError}
            hasActiveFilters={false}
            onClearFilters={() => {}}
            selected={selected}
            onToggleContact={(id) => setSelected((prev) => toggleOne(prev, id))}
            selectAllCheckboxRef={selectAllCheckboxRef}
            allSelectedOnPage={allSelectedOnPage}
            onToggleSelectPage={() => setSelected((prev) => toggleSelectAllOnPage(prev, pageIds))}
            onSelectAllMatching={selectAllInList}
            onClearSelection={() => {
              setSelected(clearSelection());
              setMatchingContacts(null);
            }}
            onGoToPage={goToPage}
            onChangePageSize={changePageSize}
            emptyStateAction={
              <Link href="/crm" className={cn(buttonVariants({ size: "sm", variant: "outline" }))}>
                Go to Contacts
              </Link>
            }
          />
        </>
      )}
    </div>
  );
}
