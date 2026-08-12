"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { AlertTriangle, ArrowLeft, Paperclip } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  ApiError,
  approveEmailIntakeItem,
  getEmailIntakeItem,
  listCrmContacts,
  matchEmailIntakeItem,
  rejectEmailIntakeItem,
  type ApproveEmailIntakeResult,
  type CrmContact,
  type EmailIntakeItem,
  type StaleFieldConflict,
} from "@/lib/api";
import { defaultSelectedFieldKeys, formatFieldValue, senderDisplayName, statusBadgeClass, statusLabel } from "@/lib/email-intake";
import { cn } from "@/lib/utils";

function contactDisplayName(contact: CrmContact): string {
  const name = [contact.first_name, contact.last_name].filter(Boolean).join(" ").trim();
  return name || contact.email || contact.crm_contact_id;
}

export default function EmailIntakeDetailPage() {
  const params = useParams<{ id: string }>();
  const intakeId = params.id;

  const [item, setItem] = useState<EmailIntakeItem | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [conflicts, setConflicts] = useState<StaleFieldConflict[]>([]);

  const [matchQuery, setMatchQuery] = useState("");
  const [matchResults, setMatchResults] = useState<CrmContact[] | null>(null);
  const [matchSearching, setMatchSearching] = useState(false);

  async function load() {
    setLoading(true);
    try {
      const fetched = await getEmailIntakeItem(intakeId);
      setItem(fetched);
      setSelected(defaultSelectedFieldKeys(fetched.proposal));
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? `Couldn't load this item (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intakeId]);

  function toggleField(fieldKey: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(fieldKey)) next.delete(fieldKey);
      else next.add(fieldKey);
      return next;
    });
  }

  async function handleApprove() {
    if (!item || selected.size === 0 || busy) return;
    setBusy(true);
    setActionError(null);
    setConflicts([]);
    try {
      const result: ApproveEmailIntakeResult = await approveEmailIntakeItem(item.intake_id, [...selected]);
      setItem(result.item);
      if (result.status === "stale") {
        setConflicts(result.conflicts);
        setSelected(defaultSelectedFieldKeys(result.item.proposal));
      } else {
        setSelected(new Set());
      }
    } catch (err) {
      setActionError(err instanceof ApiError ? `Couldn't approve (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    } finally {
      setBusy(false);
    }
  }

  async function handleReject() {
    if (!item || busy) return;
    setBusy(true);
    setActionError(null);
    try {
      const updated = await rejectEmailIntakeItem(item.intake_id);
      setItem(updated);
    } catch (err) {
      setActionError(err instanceof ApiError ? `Couldn't reject (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    } finally {
      setBusy(false);
    }
  }

  async function runMatchSearch() {
    if (!matchQuery.trim()) {
      setMatchResults(null);
      return;
    }
    setMatchSearching(true);
    try {
      const page = await listCrmContacts({ q: matchQuery.trim(), page_size: 10 });
      setMatchResults(page.items);
    } catch {
      setMatchResults([]);
    } finally {
      setMatchSearching(false);
    }
  }

  async function handleManualMatch(contactId: string) {
    if (!item || busy) return;
    setBusy(true);
    setActionError(null);
    try {
      const updated = await matchEmailIntakeItem(item.intake_id, contactId);
      setItem(updated);
      setSelected(defaultSelectedFieldKeys(updated.proposal));
      setMatchResults(null);
      setMatchQuery("");
    } catch (err) {
      setActionError(err instanceof ApiError ? `Couldn't set this match (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto max-w-3xl px-6 py-10">
      <Link href="/crm/settings/email-intake" className="mb-4 inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground">
        <ArrowLeft className="h-4 w-4" />
        Back to Email Intake
      </Link>

      {loading && <p className="py-8 text-center text-sm text-muted-foreground">Loading…</p>}

      {error && (
        <Alert variant="destructive" className="mb-4">
          <AlertTriangle />
          <AlertTitle>Couldn&apos;t load this item</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {item && (
        <div className="space-y-6">
          <div className="flex items-start justify-between gap-3">
            <div>
              <h1 className="font-serif text-2xl font-medium tracking-tight">{item.subject || "(no subject)"}</h1>
              <p className="mt-1 text-sm text-muted-foreground">{senderDisplayName(item.sender)}</p>
            </div>
            <span className={cn("shrink-0 rounded-full px-2 py-0.5 text-xs font-medium", statusBadgeClass(item.status))}>
              {statusLabel(item.status)}
            </span>
          </div>

          {actionError && (
            <Alert variant="destructive">
              <AlertTriangle />
              <AlertDescription>{actionError}</AlertDescription>
            </Alert>
          )}

          {conflicts.length > 0 && (
            <Alert variant="destructive">
              <AlertTriangle />
              <AlertTitle>This contact changed since this proposal was generated</AlertTitle>
              <AlertDescription>
                <p className="mb-2">Review the updated differences before approving.</p>
                <ul className="space-y-1">
                  {conflicts.map((c) => (
                    <li key={c.field_key}>
                      <strong>{c.field_label}</strong> -- current CRM value:{" "}
                      <code>{formatFieldValue(c.live_value)}</code>, value originally reviewed:{" "}
                      <code>{formatFieldValue(c.reviewed_value)}</code>, proposed: <code>{formatFieldValue(c.proposed_value)}</code>
                    </li>
                  ))}
                </ul>
              </AlertDescription>
            </Alert>
          )}

          {/* Source email */}
          <section className="rounded-lg border border-border bg-card p-4">
            <h2 className="mb-3 text-sm font-medium">Source email</h2>
            <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
              <dt className="text-muted-foreground">From</dt>
              <dd>{item.sender}</dd>
              <dt className="text-muted-foreground">To</dt>
              <dd>{item.recipients.join(", ") || "—"}</dd>
              <dt className="text-muted-foreground">Subject</dt>
              <dd>{item.subject || "—"}</dd>
              <dt className="text-muted-foreground">Received</dt>
              <dd title={item.received_at}>{new Date(item.received_at).toLocaleString()}</dd>
            </dl>
            <p className="mt-3 whitespace-pre-wrap rounded-md bg-secondary/40 p-3 text-sm">{item.body_text || "(empty body)"}</p>
            {item.attachments.length > 0 && (
              <div className="mt-3 space-y-1">
                {item.attachments.map((a, i) => (
                  <div key={i} className="flex items-center gap-1.5 text-xs text-muted-foreground">
                    <Paperclip className="h-3 w-3" />
                    <span>
                      {a.filename} {a.content_type ? `(${a.content_type})` : ""} -- Attachment present, not processed.
                    </span>
                  </div>
                ))}
              </div>
            )}
          </section>

          {/* Contact match */}
          <section className="rounded-lg border border-border bg-card p-4">
            <h2 className="mb-3 text-sm font-medium">Contact match</h2>
            {item.matched_contact_id ? (
              <p className="text-sm">
                Matched Contact:{" "}
                <Link href={`/crm/${item.matched_contact_id}`} className="text-primary hover:underline">
                  {item.matched_contact_name}
                </Link>
                {item.matched_on && <span className="text-muted-foreground"> · Matched on: {item.matched_on}</span>}
              </p>
            ) : item.status === "needs_match" ? (
              <div className="space-y-3">
                <p className="text-sm text-muted-foreground">
                  No confident CRM contact match. Search and select the correct contact before a proposal can be generated.
                </p>
                <div className="flex gap-2">
                  <Input
                    placeholder="Search name, email, company..."
                    value={matchQuery}
                    onChange={(e) => setMatchQuery(e.target.value)}
                    onKeyDown={(e) => e.key === "Enter" && runMatchSearch()}
                  />
                  <Button type="button" size="sm" variant="outline" onClick={runMatchSearch} disabled={matchSearching}>
                    Search
                  </Button>
                </div>
                {matchResults && matchResults.length === 0 && (
                  <p className="text-xs text-muted-foreground">No matching contacts found.</p>
                )}
                {matchResults && matchResults.length > 0 && (
                  <ul className="space-y-1">
                    {matchResults.map((contact) => (
                      <li key={contact.crm_contact_id} className="flex items-center justify-between gap-2 rounded-md border border-border p-2 text-sm">
                        <span>
                          {contactDisplayName(contact)}
                          {contact.company && <span className="text-muted-foreground"> · {contact.company}</span>}
                          {contact.email && <span className="text-muted-foreground"> · {contact.email}</span>}
                        </span>
                        <Button type="button" size="sm" disabled={busy} onClick={() => handleManualMatch(contact.crm_contact_id)}>
                          Confirm match
                        </Button>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">No contact matched.</p>
            )}
          </section>

          {/* Proposed changes */}
          <section className="rounded-lg border border-border bg-card p-4">
            <h2 className="mb-3 text-sm font-medium">Proposed changes</h2>
            {item.proposal.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                No CRM field changes were confidently extracted from this email. Review the source email before deciding what to do.
              </p>
            ) : (
              <div className="space-y-3">
                {item.proposal.map((change) => (
                  <label
                    key={change.field_key}
                    className="flex items-start gap-3 rounded-md border border-border p-3 text-sm"
                  >
                    <input
                      type="checkbox"
                      className="mt-0.5"
                      checked={selected.has(change.field_key)}
                      onChange={() => toggleField(change.field_key)}
                      disabled={item.status !== "pending_review" || busy}
                    />
                    <div className="min-w-0 flex-1">
                      <p className="font-medium">{change.field_label}</p>
                      <p className="mt-1">
                        <span className="text-muted-foreground">Current: </span>
                        <code>{formatFieldValue(change.current_value)}</code>
                      </p>
                      <p>
                        <span className="text-muted-foreground">Proposed: </span>
                        <code>{formatFieldValue(change.proposed_value)}</code>
                      </p>
                      {change.source_text && (
                        <p className="mt-1 text-xs text-muted-foreground">Source: &quot;{change.source_text}&quot;</p>
                      )}
                    </div>
                  </label>
                ))}
              </div>
            )}
          </section>

          {item.status === "pending_review" && (
            <div className="flex gap-2">
              <Button type="button" onClick={handleApprove} disabled={selected.size === 0 || busy}>
                Approve Selected Changes
              </Button>
              <Button type="button" variant="outline" onClick={handleReject} disabled={busy}>
                Reject
              </Button>
            </div>
          )}

          {item.reviewed_at && (
            <p className="text-xs text-muted-foreground">Reviewed {new Date(item.reviewed_at).toLocaleString()}</p>
          )}
        </div>
      )}
    </div>
  );
}
