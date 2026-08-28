"use client";

import { useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { AlertTriangle, ArrowLeft, Archive, Ban, CheckCircle2, Save } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Accordion, AccordionItem, AccordionPanel, AccordionTrigger } from "@/components/ui/accordion";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import {
  ApiError,
  archiveCrmContact,
  getCrmContact,
  getCrmContactLumaRegistrations,
  getMailSuppressionStatus,
  listCrmCustomFields,
  suppressMailEmail,
  unsuppressMailEmail,
  updateCrmContact,
  type CrmContact,
  type CrmContactLumaRegistration,
  type CrmCustomFieldDefinition,
  type MailContactSuppressionStatus,
} from "@/lib/api";
import { buildEventHistory } from "@/lib/contact-event-history";
import { buildContactSummary } from "@/lib/contact-summary";
import { DIETARY_PREFERENCE_OPTIONS, INVESTOR_MODE_OPTIONS, THESIS_SECTION_FIELDS } from "@/lib/crm-thesis-options";
import { mailSuppressionReasonLabel } from "@/lib/mail";
import { addTagValue, removeTagValue } from "@/lib/tag-multi-select";
import { cn } from "@/lib/utils";

const CORE_TEXT_FIELDS: [keyof CrmContact, string][] = [
  ["first_name", "First name"],
  ["last_name", "Last name"],
  ["email", "Email"],
  ["email_status", "Email status"],
  ["phone", "Phone"],
  ["linkedin_url", "LinkedIn URL"],
  ["title", "Title"],
  ["company", "Company"],
  ["company_website", "Company website"],
  ["city", "City"],
  ["state", "State"],
  ["country", "Country"],
  ["industry", "Industry"],
  ["company_size", "Company size"],
  ["revenue", "Revenue"],
  ["funding_stage", "Funding stage"],
  ["funding_amount", "Funding amount"],
  ["seniority", "Seniority"],
  ["department", "Department"],
  ["job_function", "Job function"],
  ["apollo_contact_id", "Apollo contact ID"],
];

function TextField({ label, value, onChange }: { label: string; value: string; onChange: (v: string) => void }) {
  return (
    <div className="space-y-1">
      <label className="text-xs font-medium text-muted-foreground">{label}</label>
      <Input value={value} onChange={(e) => onChange(e.target.value)} />
    </div>
  );
}

// The 7 questions asked identically for private and institutional investing. Grouped
// for display only -- meeting/demographic preferences get their own cross-mode
// sections below since they're about how contacts want to engage, not what they
// invest in. No schema change: still reads/writes the same thesis_{mode}_{key} fields.
const CRITERIA_FIELDS = THESIS_SECTION_FIELDS.filter(
  (f) => f.key !== "meeting_preferences" && f.key !== "demographic_preferences"
);
const MEETING_FIELD = THESIS_SECTION_FIELDS.find((f) => f.key === "meeting_preferences")!;
const DEMOGRAPHIC_FIELD = THESIS_SECTION_FIELDS.find((f) => f.key === "demographic_preferences")!;

function ThesisCriteriaField({
  contact,
  mode,
  field,
  labelSuffix,
  set,
  setCustomField,
  customFieldOptions,
}: {
  contact: CrmContact;
  mode: "private" | "institutional";
  field: { key: string; label: string; options: string[] };
  labelSuffix?: string;
  set: <K extends keyof CrmContact>(key: K, value: CrmContact[K]) => void;
  setCustomField?: (fieldKey: string, value: unknown) => void;
  customFieldOptions?: string[];
}) {
  // 2026-08-06 Check Size consolidation: check_size_personal/check_size_institutional
  // (custom fields) are the sole canonical destination -- reads/writes them directly
  // instead of the deprecated thesis_{mode}_check_sizes fields. No separate free-text
  // "Other" input here: "Other:" is already one of the custom field's own checkbox
  // options, unlike every other criteria field which has a dedicated _other column.
  if (field.key === "check_sizes" && setCustomField && customFieldOptions) {
    const customKey = mode === "private" ? "check_size_personal" : "check_size_institutional";
    const selected = (contact.custom_fields[customKey] as string[]) ?? [];
    return (
      <MultiSelect
        label={`${field.label}${labelSuffix ?? ""}`}
        options={customFieldOptions}
        selected={selected}
        onChange={(v) => setCustomField(customKey, v)}
      />
    );
  }

  const key = `thesis_${mode}_${field.key}` as keyof CrmContact;
  const otherKey = `thesis_${mode}_${field.key}_other` as keyof CrmContact;
  return (
    <div className="space-y-2">
      <MultiSelect
        label={`${field.label}${labelSuffix ?? ""}`}
        options={field.options}
        selected={(contact[key] as string[]) ?? []}
        onChange={(v) => set(key, v as CrmContact[typeof key])}
      />
      <Input
        placeholder="Other (free text)"
        value={(contact[otherKey] as string) ?? ""}
        onChange={(e) => set(otherKey, e.target.value as CrmContact[typeof otherKey])}
      />
    </div>
  );
}

function MultiSelect({
  label,
  options,
  selected,
  onChange,
}: {
  label: string;
  options: string[];
  selected: string[];
  onChange: (values: string[]) => void;
}) {
  function toggle(option: string) {
    onChange(selected.includes(option) ? selected.filter((v) => v !== option) : [...selected, option]);
  }

  // No predefined options -- an open-ended field (e.g. Investment Industry).
  // A checkbox grid over zero options would render nothing even though
  // `selected` holds real stored values, so this is a tag editor instead.
  if (options.length === 0) {
    return <TagMultiSelect label={label} selected={selected} onChange={onChange} />;
  }

  return (
    <div className="space-y-1.5">
      <p className="text-xs font-medium text-muted-foreground">{label}</p>
      <div className="grid gap-1 sm:grid-cols-2">
        {options.map((option) => (
          <label key={option} className="flex items-start gap-2 text-sm">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={selected.includes(option)}
              onChange={() => toggle(option)}
            />
            <span>{option}</span>
          </label>
        ))}
      </div>
    </div>
  );
}

function TagMultiSelect({
  label,
  selected,
  onChange,
}: {
  label: string;
  selected: string[];
  onChange: (values: string[]) => void;
}) {
  const [draft, setDraft] = useState("");

  function commitDraft() {
    onChange(addTagValue(selected, draft));
    setDraft("");
  }

  return (
    <div className="space-y-1.5">
      <p className="text-xs font-medium text-muted-foreground">{label}</p>
      {selected.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {selected.map((value) => (
            <span
              key={value}
              className="inline-flex items-center gap-1.5 rounded-full border border-input bg-muted px-2.5 py-0.5 text-xs"
            >
              {value}
              <button
                type="button"
                aria-label={`Remove ${value}`}
                className="text-muted-foreground hover:text-foreground"
                onClick={() => onChange(removeTagValue(selected, value))}
              >
                ×
              </button>
            </span>
          ))}
        </div>
      )}
      <div className="flex gap-2">
        <input
          type="text"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              commitDraft();
            }
          }}
          placeholder="Add a value..."
          className="h-9 flex-1 rounded-md border border-input bg-transparent px-3 text-sm"
        />
        <Button type="button" variant="outline" onClick={commitDraft}>
          Add
        </Button>
      </div>
    </div>
  );
}

export default function CrmContactDetailPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const [contact, setContact] = useState<CrmContact | null>(null);
  const [customFields, setCustomFields] = useState<CrmCustomFieldDefinition[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  // Astronomic Mail suppression -- keyed by this contact's email, entirely
  // independent of the contact's own email_status field (see lib/mail.ts /
  // app/models/mail.py). Loaded as a separate, best-effort call so a
  // suppression-lookup failure never blocks the contact page itself.
  const [suppression, setSuppression] = useState<MailContactSuppressionStatus | null>(null);
  const [suppressionBusy, setSuppressionBusy] = useState(false);
  const [suppressionError, setSuppressionError] = useState<string | null>(null);

  // Event History -- read-only, best-effort, loaded independently of the
  // main contact fetch (same pattern as Mail suppression above) so a
  // failure here never blocks the rest of the page.
  const [lumaRegistrations, setLumaRegistrations] = useState<CrmContactLumaRegistration[] | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const [c, fields] = await Promise.all([getCrmContact(params.id), listCrmCustomFields(false)]);
        setContact(c);
        setCustomFields(fields);
      } catch (err) {
        setError(err instanceof ApiError ? `Couldn't load contact (${err.status}): ${err.message}` : "Couldn't reach the backend.");
      }
    })();
  }, [params.id]);

  useEffect(() => {
    if (!contact?.email) {
      setSuppression(null);
      return;
    }
    getMailSuppressionStatus(contact.email)
      .then(setSuppression)
      .catch(() => setSuppression(null));
  }, [contact?.email]);

  useEffect(() => {
    if (!contact?.crm_contact_id) return;
    getCrmContactLumaRegistrations(contact.crm_contact_id)
      .then(setLumaRegistrations)
      .catch(() => setLumaRegistrations([]));
  }, [contact?.crm_contact_id]);

  async function handleSuppress() {
    if (!contact?.email) return;
    setSuppressionBusy(true);
    setSuppressionError(null);
    try {
      await suppressMailEmail(contact.email, "manual");
      setSuppression(await getMailSuppressionStatus(contact.email));
    } catch (err) {
      setSuppressionError(err instanceof ApiError ? `Couldn't suppress (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    } finally {
      setSuppressionBusy(false);
    }
  }

  async function handleUnsuppress() {
    if (!contact?.email) return;
    setSuppressionBusy(true);
    setSuppressionError(null);
    try {
      await unsuppressMailEmail(contact.email);
      setSuppression(await getMailSuppressionStatus(contact.email));
    } catch (err) {
      setSuppressionError(err instanceof ApiError ? `Couldn't unsuppress (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    } finally {
      setSuppressionBusy(false);
    }
  }

  function set<K extends keyof CrmContact>(key: K, value: CrmContact[K]) {
    setContact((prev) => (prev ? { ...prev, [key]: value } : prev));
    setSaved(false);
  }

  function setCustomField(fieldKey: string, value: unknown) {
    setContact((prev) => (prev ? { ...prev, custom_fields: { ...prev.custom_fields, [fieldKey]: value } } : prev));
    setSaved(false);
  }

  async function handleSave() {
    if (!contact) return;
    setSaving(true);
    setSaveError(null);
    try {
      const updated = await updateCrmContact(contact.crm_contact_id, contact as unknown as Record<string, unknown>);
      setContact(updated);
      setSaved(true);
    } catch (err) {
      setSaveError(err instanceof ApiError ? `Save failed (${err.status}): ${err.message}` : "Couldn't reach the backend to save.");
    } finally {
      setSaving(false);
    }
  }

  async function handleArchive() {
    if (!contact) return;
    const updated = await archiveCrmContact(contact.crm_contact_id);
    setContact(updated);
  }

  if (error) {
    return (
      <div className="mx-auto max-w-3xl px-6 py-10">
        <Alert variant="destructive">
          <AlertTriangle />
          <AlertTitle>Couldn&apos;t load contact</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      </div>
    );
  }

  if (!contact) {
    return (
      <div className="mx-auto max-w-3xl space-y-4 px-6 py-10">
        <Skeleton className="h-8 w-64" />
        <Skeleton className="h-64 w-full rounded-xl" />
      </div>
    );
  }

  const name = [contact.first_name, contact.last_name].filter(Boolean).join(" ") || "Unnamed contact";

  // 2026-08-06 Check Size consolidation: check_size_personal/check_size_institutional
  // (custom fields) are the sole canonical Check Size destination -- the "Which size
  // investments are you open to making?" checkbox group in the Investor Thesis
  // section below reads/writes these directly instead of the deprecated
  // thesis_private_check_sizes/thesis_institutional_check_sizes fields. Excluded
  // from the generic "Custom fields" card below so there's exactly one Check Size
  // control, not two.
  const checkSizePersonalField = customFields.find((f) => f.field_key === "check_size_personal");
  const checkSizeInstitutionalField = customFields.find((f) => f.field_key === "check_size_institutional");
  const visibleCustomFields = customFields.filter(
    (f) => f.field_key !== "check_size_personal" && f.field_key !== "check_size_institutional"
  );

  // Overview + Event History: both computed deterministically from data
  // already on this page -- no LLM call, no extra network round trip for
  // the summary itself (see lib/contact-summary.ts's module docstring).
  const contactSummary = buildContactSummary(contact);
  const eventHistory = lumaRegistrations ? buildEventHistory(lumaRegistrations) : null;

  return (
    <div className="mx-auto max-w-3xl px-6 py-10">
      <button
        onClick={() => router.push("/crm")}
        className="mb-6 flex items-center gap-1.5 text-sm text-muted-foreground transition-colors hover:text-foreground"
      >
        <ArrowLeft className="h-3.5 w-3.5" />
        All contacts
      </button>

      <div className="mb-6 flex items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2">
            <h1 className="font-serif text-2xl font-medium tracking-tight">{name}</h1>
            {contact.archived && <Badge variant="outline">Archived</Badge>}
          </div>
          <p className="mt-1 text-sm text-muted-foreground">
            {[contact.title, contact.company].filter(Boolean).join(" @ ")}
          </p>
        </div>
        <div className="flex shrink-0 gap-2">
          <Button variant="outline" size="sm" onClick={handleArchive} className="gap-1.5" disabled={contact.archived}>
            <Archive className="h-3.5 w-3.5" />
            Archive
          </Button>
          <Button size="sm" onClick={handleSave} disabled={saving} className="gap-1.5">
            <Save className="h-3.5 w-3.5" />
            {saving ? "Saving..." : "Save"}
          </Button>
        </div>
      </div>

      {saveError && (
        <Alert variant="destructive" className="mb-4">
          <AlertTriangle />
          <AlertTitle>Save failed</AlertTitle>
          <AlertDescription>{saveError}</AlertDescription>
        </Alert>
      )}
      {saved && <p className="mb-4 text-sm text-muted-foreground">Saved.</p>}

      <div className="mb-6 space-y-6">
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">Overview</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {contactSummary.sentence ? (
              <p className="text-sm text-foreground">{contactSummary.sentence}</p>
            ) : (
              <p className="text-sm text-muted-foreground">Not enough structured data yet to summarize this contact.</p>
            )}
            {contactSummary.highlights.length > 0 && (
              <div className="flex flex-wrap gap-2">
                {contactSummary.highlights.map((h) => (
                  // A plain div, not <Badge>, on purpose: Badge forces
                  // whitespace-nowrap (fine for short status labels, but a
                  // long Investor Type/Investment Focus value would then
                  // overflow the card horizontally instead of wrapping).
                  <div
                    key={h.label}
                    className="max-w-full rounded-md bg-secondary/60 px-2 py-1 text-xs break-words text-secondary-foreground"
                  >
                    <span className="font-medium">{h.label}:</span> {h.value}
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-sm">Event History</CardTitle>
          </CardHeader>
          <CardContent>
            {eventHistory === null ? (
              <p className="text-sm text-muted-foreground">Loading…</p>
            ) : eventHistory.length === 0 ? (
              <p className="text-sm text-muted-foreground">No event history yet.</p>
            ) : (
              <ul className="space-y-3">
                {eventHistory.map((entry) => (
                  <li key={entry.lumaEventId} className="border-b border-border pb-3 last:border-0 last:pb-0">
                    <p className="text-sm font-medium text-foreground">{entry.eventName}</p>
                    <p className="text-xs text-muted-foreground">
                      {entry.statusLabel}
                      {entry.dateLabel ? ` · ${entry.dateLabel}` : ""}
                    </p>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      </div>

      {contact.email && suppression && (
        <Card className={cn("mb-6", suppression.suppressed && "border-destructive/40 bg-destructive/5")}>
          <CardContent className="flex items-center justify-between gap-3 py-4">
            <div className="flex items-center gap-2">
              {suppression.suppressed ? (
                <Ban className="h-4 w-4 text-destructive" />
              ) : (
                <CheckCircle2 className="h-4 w-4 text-muted-foreground" />
              )}
              <div>
                <p className={cn("text-sm font-medium", suppression.suppressed && "text-destructive")}>
                  {suppression.suppressed ? "Email suppressed -- will not be mailed" : "Not suppressed for Mail"}
                </p>
                {suppression.suppressed && suppression.reason && (
                  <p className="text-xs text-muted-foreground">
                    Reason: {mailSuppressionReasonLabel(suppression.reason)}
                    {suppression.notes ? ` -- ${suppression.notes}` : ""}
                  </p>
                )}
              </div>
            </div>
            {suppressionError && <p className="text-xs text-destructive">{suppressionError}</p>}
            {suppression.suppressed ? (
              <Button variant="outline" size="sm" onClick={handleUnsuppress} disabled={suppressionBusy}>
                Unsuppress
              </Button>
            ) : (
              <Button variant="outline" size="sm" onClick={handleSuppress} disabled={suppressionBusy} className="gap-1.5">
                <Ban className="h-3.5 w-3.5" />
                Suppress from Mail
              </Button>
            )}
          </CardContent>
        </Card>
      )}

      <div className="space-y-6">
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">Profile (external / source data)</CardTitle>
          </CardHeader>
          <CardContent className="grid gap-3 sm:grid-cols-2">
            {CORE_TEXT_FIELDS.map(([key, label]) => (
              <TextField
                key={key}
                label={label}
                value={(contact[key] as string) ?? ""}
                onChange={(v) => set(key, v as CrmContact[typeof key])}
              />
            ))}
            <div className="sm:col-span-2 space-y-1">
              <label className="text-xs font-medium text-muted-foreground">Technologies (one per line)</label>
              <Textarea
                value={contact.technologies.join("\n")}
                onChange={(e) => set("technologies", e.target.value.split("\n").map((v) => v.trim()).filter(Boolean))}
              />
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-sm">Investor Thesis</CardTitle>
          </CardHeader>
          <CardContent>
            <Accordion multiple defaultValue={["overview"]}>
              <AccordionItem value="overview">
                <AccordionTrigger>Investor Overview</AccordionTrigger>
                <AccordionPanel className="space-y-4">
                  <TextField label="City/cities they live in or frequent" value={contact.thesis_cities ?? ""} onChange={(v) => set("thesis_cities", v)} />
                  <div className="space-y-1">
                    <div className="flex items-center justify-between gap-2">
                      <label className="text-xs font-medium text-muted-foreground">Invests privately or institutionally?</label>
                      <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
                        <input
                          type="checkbox"
                          checked={contact.thesis_investor_mode_manual_override}
                          onChange={(e) => set("thesis_investor_mode_manual_override", e.target.checked)}
                        />
                        Manually override
                      </label>
                    </div>
                    <select
                      value={contact.thesis_investor_mode ?? ""}
                      onChange={(e) => set("thesis_investor_mode", e.target.value)}
                      disabled={!contact.thesis_investor_mode_manual_override}
                      className="h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm disabled:cursor-not-allowed disabled:opacity-60"
                    >
                      <option value="">-- not set --</option>
                      {INVESTOR_MODE_OPTIONS.map((o) => (
                        <option key={o} value={o}>{o}</option>
                      ))}
                    </select>
                    <p className="text-xs text-muted-foreground">
                      {contact.thesis_investor_mode_manual_override
                        ? "Manual override is on -- this value will not change automatically."
                        : "Auto-derived from Investor Type. Check \"Manually override\" to set this by hand."}
                    </p>
                  </div>
                  <label className="flex items-center gap-2 text-sm font-medium">
                    <input
                      type="checkbox"
                      checked={contact.thesis_also_invests_institutionally ?? false}
                      onChange={(e) => set("thesis_also_invests_institutionally", e.target.checked)}
                    />
                    Also invests institutionally (via a fund)?
                  </label>
                </AccordionPanel>
              </AccordionItem>

              <AccordionItem value="private">
                <AccordionTrigger>Private Investments</AccordionTrigger>
                <AccordionPanel className="space-y-5">
                  {CRITERIA_FIELDS.map((field) => (
                    <ThesisCriteriaField
                      key={field.key}
                      contact={contact}
                      mode="private"
                      field={field}
                      set={set}
                      setCustomField={setCustomField}
                      customFieldOptions={checkSizePersonalField?.options}
                    />
                  ))}
                  <div className="space-y-1">
                    <label className="text-xs font-medium text-muted-foreground">Other criteria or feedback (private)</label>
                    <Textarea value={contact.thesis_private_other_criteria ?? ""} onChange={(e) => set("thesis_private_other_criteria", e.target.value)} />
                  </div>
                </AccordionPanel>
              </AccordionItem>

              <AccordionItem value="institutional">
                <AccordionTrigger>Institutional Investments</AccordionTrigger>
                <AccordionPanel className="space-y-5">
                  {CRITERIA_FIELDS.map((field) => (
                    <ThesisCriteriaField
                      key={field.key}
                      contact={contact}
                      mode="institutional"
                      field={field}
                      set={set}
                      setCustomField={setCustomField}
                      customFieldOptions={checkSizeInstitutionalField?.options}
                    />
                  ))}
                  <div className="space-y-1">
                    <label className="text-xs font-medium text-muted-foreground">Other criteria or feedback (institutional)</label>
                    <Textarea value={contact.thesis_institutional_other_criteria ?? ""} onChange={(e) => set("thesis_institutional_other_criteria", e.target.value)} />
                  </div>
                </AccordionPanel>
              </AccordionItem>

              <AccordionItem value="meeting">
                <AccordionTrigger>Fundraising / Meeting Preferences</AccordionTrigger>
                <AccordionPanel className="space-y-5">
                  <ThesisCriteriaField contact={contact} mode="private" field={MEETING_FIELD} labelSuffix=" (private)" set={set} />
                  <ThesisCriteriaField contact={contact} mode="institutional" field={MEETING_FIELD} labelSuffix=" (institutional)" set={set} />
                </AccordionPanel>
              </AccordionItem>

              <AccordionItem value="founder">
                <AccordionTrigger>Founder Preferences</AccordionTrigger>
                <AccordionPanel className="space-y-5">
                  <ThesisCriteriaField contact={contact} mode="private" field={DEMOGRAPHIC_FIELD} labelSuffix=" (private)" set={set} />
                  <ThesisCriteriaField contact={contact} mode="institutional" field={DEMOGRAPHIC_FIELD} labelSuffix=" (institutional)" set={set} />
                </AccordionPanel>
              </AccordionItem>

              <AccordionItem value="additional">
                <AccordionTrigger>Additional Information</AccordionTrigger>
                <AccordionPanel className="space-y-4">
                  <div className="space-y-2">
                    <MultiSelect
                      label="Dietary preferences"
                      options={DIETARY_PREFERENCE_OPTIONS}
                      selected={contact.thesis_dietary_preferences ?? []}
                      onChange={(v) => set("thesis_dietary_preferences", v)}
                    />
                    <Input
                      placeholder="Other (free text)"
                      value={contact.thesis_dietary_preferences_other ?? ""}
                      onChange={(e) => set("thesis_dietary_preferences_other", e.target.value)}
                    />
                  </div>
                  <div className="space-y-1">
                    <label className="text-xs font-medium text-muted-foreground">Referral emails (other investor-friends to invite)</label>
                    <Textarea value={contact.thesis_referral_emails ?? ""} onChange={(e) => set("thesis_referral_emails", e.target.value)} />
                  </div>
                </AccordionPanel>
              </AccordionItem>
            </Accordion>
          </CardContent>
        </Card>

        {visibleCustomFields.length > 0 && (
          <Card>
            <CardHeader>
              <CardTitle className="text-sm">Custom fields</CardTitle>
            </CardHeader>
            <CardContent className="grid gap-3 sm:grid-cols-2">
              {visibleCustomFields.map((field) => {
                const value = contact.custom_fields[field.field_key];
                if (field.field_type === "boolean") {
                  return (
                    <label key={field.field_key} className="flex items-center gap-2 text-sm">
                      <input type="checkbox" checked={Boolean(value)} onChange={(e) => setCustomField(field.field_key, e.target.checked)} />
                      {field.label}
                    </label>
                  );
                }
                if (field.field_type === "single_select") {
                  return (
                    <div key={field.field_key} className="space-y-1">
                      <label className="text-xs font-medium text-muted-foreground">{field.label}</label>
                      <select
                        value={(value as string) ?? ""}
                        onChange={(e) => setCustomField(field.field_key, e.target.value)}
                        className="h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm"
                      >
                        <option value="">-- not set --</option>
                        {field.options.map((o) => (
                          <option key={o} value={o}>{o}</option>
                        ))}
                      </select>
                    </div>
                  );
                }
                if (field.field_type === "multi_select") {
                  return (
                    <div key={field.field_key} className="sm:col-span-2">
                      <MultiSelect
                        label={field.label}
                        options={field.options}
                        selected={(value as string[]) ?? []}
                        onChange={(v) => setCustomField(field.field_key, v)}
                      />
                    </div>
                  );
                }
                if (field.field_type === "long_text") {
                  return (
                    <div key={field.field_key} className="sm:col-span-2 space-y-1">
                      <label className="text-xs font-medium text-muted-foreground">{field.label}</label>
                      <Textarea value={(value as string) ?? ""} onChange={(e) => setCustomField(field.field_key, e.target.value)} />
                    </div>
                  );
                }
                return (
                  <TextField
                    key={field.field_key}
                    label={field.label}
                    value={value != null ? String(value) : ""}
                    onChange={(v) => setCustomField(field.field_key, field.field_type === "number" ? Number(v) : v)}
                  />
                );
              })}
            </CardContent>
          </Card>
        )}
      </div>
    </div>
  );
}
