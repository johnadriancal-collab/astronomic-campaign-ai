"use client";

import { useEffect, useState } from "react";
import { AlertTriangle, Download, Pencil, Plus, X } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  ApiError,
  createCrmCustomField,
  exportCrmBackup,
  listCrmCustomFields,
  updateCrmCustomField,
  type CrmCustomFieldDefinition,
  type CustomFieldType,
} from "@/lib/api";

const FIELD_TYPES: CustomFieldType[] = ["text", "long_text", "number", "date", "boolean", "single_select", "multi_select"];

function formatCreatedAt(iso: string): string {
  try {
    return new Date(iso).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  } catch {
    return iso;
  }
}

function EditFieldForm({
  field,
  onCancel,
  onSaved,
}: {
  field: CrmCustomFieldDefinition;
  onCancel: () => void;
  onSaved: (updated: CrmCustomFieldDefinition) => void;
}) {
  const [label, setLabel] = useState(field.label);
  const [description, setDescription] = useState(field.description ?? "");
  const [optionsText, setOptionsText] = useState(field.options.join(", "));
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const needsOptions = field.field_type === "single_select" || field.field_type === "multi_select";

  async function handleSave() {
    const newOptions = needsOptions
      ? optionsText.split(",").map((v) => v.trim()).filter(Boolean)
      : field.options;
    const removed = field.options.filter((o) => !newOptions.includes(o));
    if (removed.length > 0) {
      const confirmed = window.confirm(
        `Removing option(s) ${removed.map((o) => `"${o}"`).join(", ")} from "${field.label}".\n\n` +
          "Contacts that already have this value stored will keep it, but it will no longer appear as a pickable " +
          "choice. Continue?"
      );
      if (!confirmed) return;
    }
    setSaving(true);
    setSaveError(null);
    try {
      const updated = await updateCrmCustomField(field.crm_custom_field_id, {
        label: label.trim(),
        description: description.trim() || null,
        options: newOptions,
      });
      onSaved(updated);
    } catch (err) {
      setSaveError(err instanceof ApiError ? `Couldn't save (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="space-y-3 border-t border-border/60 pt-3">
      {saveError && (
        <Alert variant="destructive">
          <AlertDescription>{saveError}</AlertDescription>
        </Alert>
      )}
      <div className="space-y-1">
        <label className="text-xs font-medium text-muted-foreground">Label</label>
        <Input value={label} onChange={(e) => setLabel(e.target.value)} />
      </div>
      <div className="space-y-1">
        <label className="text-xs font-medium text-muted-foreground">Description</label>
        <Textarea value={description} onChange={(e) => setDescription(e.target.value)} rows={2} />
      </div>
      {needsOptions && (
        <div className="space-y-1">
          <label className="text-xs font-medium text-muted-foreground">Options (comma-separated)</label>
          <Textarea value={optionsText} onChange={(e) => setOptionsText(e.target.value)} rows={2} />
        </div>
      )}
      <div className="flex gap-2">
        <Button size="sm" onClick={handleSave} disabled={saving}>
          {saving ? "Saving..." : "Save changes"}
        </Button>
        <Button size="sm" variant="outline" onClick={onCancel} disabled={saving}>
          Cancel
        </Button>
      </div>
    </div>
  );
}

export default function CrmCustomFieldsPage() {
  const [fields, setFields] = useState<CrmCustomFieldDefinition[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  const [fieldKey, setFieldKey] = useState("");
  const [label, setLabel] = useState("");
  const [description, setDescription] = useState("");
  const [fieldType, setFieldType] = useState<CustomFieldType>("text");
  const [optionsText, setOptionsText] = useState("");
  const [required, setRequired] = useState(false);
  const [editingFieldId, setEditingFieldId] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);

  async function handleExportBackup() {
    setExporting(true);
    setExportError(null);
    try {
      const backup = await exportCrmBackup();
      const blob = new Blob([JSON.stringify(backup, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      const stamp = new Date().toISOString().replace(/[:.]/g, "-");
      a.href = url;
      a.download = `crm_backup_${stamp}.json`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      setExportError(err instanceof ApiError ? `Couldn't export backup (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    } finally {
      setExporting(false);
    }
  }

  async function load() {
    try {
      setFields(await listCrmCustomFields(true));
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? `Couldn't load custom fields (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    }
  }

  useEffect(() => {
    load();
  }, []);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    setCreating(true);
    setCreateError(null);
    try {
      await createCrmCustomField({
        field_key: fieldKey.trim(),
        label: label.trim(),
        field_type: fieldType,
        description: description.trim() || undefined,
        options: optionsText ? optionsText.split(",").map((v) => v.trim()).filter(Boolean) : [],
        required,
      });
      setFieldKey("");
      setLabel("");
      setDescription("");
      setOptionsText("");
      setRequired(false);
      setFieldType("text");
      await load();
    } catch (err) {
      setCreateError(err instanceof ApiError ? `Couldn't create field (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    } finally {
      setCreating(false);
    }
  }

  async function toggleActive(field: CrmCustomFieldDefinition) {
    const updated = await updateCrmCustomField(field.crm_custom_field_id, { active: !field.active });
    setFields((prev) => (prev ? prev.map((f) => (f.crm_custom_field_id === field.crm_custom_field_id ? updated : f)) : prev));
  }

  const needsOptions = fieldType === "single_select" || fieldType === "multi_select";

  return (
    <div className="mx-auto max-w-3xl px-6 py-10">
      <div className="mb-6 flex items-start justify-between gap-4">
        <div>
          <h1 className="mb-2 font-serif text-2xl font-medium tracking-tight">Custom fields</h1>
          <p className="text-sm text-muted-foreground">
            Add fields to track on CRM contacts without any code changes -- dietary preference, favorite sports team, dinner
            attendance, referral source, whatever you need. Deactivating a field hides it without deleting data already
            stored under it.
          </p>
        </div>
        <Button size="sm" variant="outline" className="shrink-0 gap-1.5" onClick={handleExportBackup} disabled={exporting}>
          <Download className="h-3.5 w-3.5" />
          {exporting ? "Exporting..." : "Export CRM Backup"}
        </Button>
      </div>

      {exportError && (
        <Alert variant="destructive" className="mb-4">
          <AlertTriangle />
          <AlertTitle>Backup export failed</AlertTitle>
          <AlertDescription>{exportError}</AlertDescription>
        </Alert>
      )}

      {error && (
        <Alert variant="destructive" className="mb-4">
          <AlertTriangle />
          <AlertTitle>Couldn&apos;t load custom fields</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      <Card className="mb-6">
        <CardHeader>
          <CardTitle className="text-sm">New custom field</CardTitle>
        </CardHeader>
        <CardContent>
          {createError && (
            <Alert variant="destructive" className="mb-3">
              <AlertDescription>{createError}</AlertDescription>
            </Alert>
          )}
          <form onSubmit={handleCreate} className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1">
              <label className="text-xs font-medium text-muted-foreground">Field key (unique, no spaces)</label>
              <Input value={fieldKey} onChange={(e) => setFieldKey(e.target.value)} placeholder="dietary_preference" required />
            </div>
            <div className="space-y-1">
              <label className="text-xs font-medium text-muted-foreground">Label</label>
              <Input value={label} onChange={(e) => setLabel(e.target.value)} placeholder="Dietary Preference" required />
            </div>
            <div className="space-y-1">
              <label className="text-xs font-medium text-muted-foreground">Type</label>
              <select
                value={fieldType}
                onChange={(e) => setFieldType(e.target.value as CustomFieldType)}
                className="h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm"
              >
                {FIELD_TYPES.map((t) => (
                  <option key={t} value={t}>{t.replace("_", " ")}</option>
                ))}
              </select>
            </div>
            <div className="space-y-1">
              <label className="text-xs font-medium text-muted-foreground">Description (optional)</label>
              <Input value={description} onChange={(e) => setDescription(e.target.value)} />
            </div>
            {needsOptions && (
              <div className="sm:col-span-2 space-y-1">
                <label className="text-xs font-medium text-muted-foreground">Options (comma-separated)</label>
                <Input value={optionsText} onChange={(e) => setOptionsText(e.target.value)} placeholder="Vegetarian, Vegan, No restrictions" />
              </div>
            )}
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={required} onChange={(e) => setRequired(e.target.checked)} />
              Required
            </label>
            <div className="sm:col-span-2">
              <Button type="submit" disabled={creating} className="gap-1.5">
                <Plus className="h-4 w-4" />
                {creating ? "Creating..." : "Create field"}
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>

      {fields && (
        <div className="space-y-2">
          {fields.map((field) => (
            <Card key={field.crm_custom_field_id}>
              <CardContent className="py-3">
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <p className="text-sm font-medium">
                      {field.label} <span className="text-xs text-muted-foreground">({field.field_type})</span>
                    </p>
                    <p className="text-xs text-muted-foreground">
                      key: {field.field_key}
                      {field.description && ` -- ${field.description}`}
                    </p>
                    {field.options.length > 0 && (
                      <p className="mt-1 text-xs text-muted-foreground">options: {field.options.join(", ")}</p>
                    )}
                    <p className="mt-1 text-xs text-muted-foreground">created {formatCreatedAt(field.created_at)}</p>
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    <Badge variant={field.active ? "outline" : "secondary"}>{field.active ? "Active" : "Inactive"}</Badge>
                    <Button
                      size="sm"
                      variant="outline"
                      className="gap-1"
                      onClick={() => setEditingFieldId(editingFieldId === field.crm_custom_field_id ? null : field.crm_custom_field_id)}
                    >
                      {editingFieldId === field.crm_custom_field_id ? (
                        <>
                          <X className="h-3.5 w-3.5" />
                          Close
                        </>
                      ) : (
                        <>
                          <Pencil className="h-3.5 w-3.5" />
                          Edit
                        </>
                      )}
                    </Button>
                    <Button size="sm" variant="outline" onClick={() => toggleActive(field)}>
                      {field.active ? "Deactivate" : "Activate"}
                    </Button>
                  </div>
                </div>
                {editingFieldId === field.crm_custom_field_id && (
                  <EditFieldForm
                    field={field}
                    onCancel={() => setEditingFieldId(null)}
                    onSaved={(updated) => {
                      setFields((prev) => (prev ? prev.map((f) => (f.crm_custom_field_id === updated.crm_custom_field_id ? updated : f)) : prev));
                      setEditingFieldId(null);
                    }}
                  />
                )}
              </CardContent>
            </Card>
          ))}
          {fields.length === 0 && <p className="text-sm text-muted-foreground">No custom fields yet.</p>}
        </div>
      )}
    </div>
  );
}
