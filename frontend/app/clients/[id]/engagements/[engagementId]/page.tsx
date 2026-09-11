"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { AlertTriangle, ArrowLeft, Lock } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ExternalLink } from "lucide-react";
import { EngagementFormModal } from "@/components/engagement-form-modal";
import { EngagementCloseoutFormModal } from "@/components/engagement-closeout-form-modal";
import { EngagementParticipantFormModal } from "@/components/engagement-participant-form-modal";
import { LumaEventPicker } from "@/components/luma-event-picker";
import {
  ApiError,
  getClient,
  getClientEngagement,
  getEngagementCloseout,
  getLumaEvent,
  listEngagementParticipants,
  updateClientEngagement,
  updateEngagementCloseout,
  updateEngagementParticipant,
  type Client,
  type Engagement,
  type EngagementCloseout,
  type EngagementParticipant,
  type EngagementParticipantView,
  type LumaEventSummary,
} from "@/lib/api";
import {
  dinnerTypeLabel,
  engagementCloseoutAttendanceRate,
  engagementContractStatusLabel,
  engagementPaymentStatusLabel,
  engagementStatusBadgeClass,
  engagementStatusLabel,
  engagementTypeLabel,
  formatEngagementDate,
  participantAttendanceStatusLabel,
  participantRoleLabel,
  participantRsvpStatusLabel,
} from "@/lib/client-crm";
import { CLIENT_CRM_DETAIL_CONTAINER_CLASS, ENGAGEMENT_OVERVIEW_LUMA_GRID_CLASS } from "@/lib/client-crm-detail-layout";
import { cn } from "@/lib/utils";

// Stage 1E: Overview + Commercial; Stage 1F adds a real Closeout section
// now that EngagementCloseout actually exists; Stage 1G adds a real
// Participants section now that EngagementParticipant actually exists
// (see each stage's own STOP report). Stage 1H-A adds a real Linked Luma
// Event section -- link-only: no EngagementParticipant is ever created,
// updated, or historically imported from Luma here (that's a future,
// separate, dedicated sync stage). Still deliberately NOT a tabbed page with other
// empty/fake sections (staged relationship follow-ups, notes/activity) --
// only Overview, Linked Luma Event, Commercial, Closeout, and Participants
// have any real, backend-supported data behind them today. Nothing about
// turnout, guest quality, or outcomes is fabricated anywhere on this page
// -- a Closeout only ever renders once one has actually been recorded,
// and Participant-derived counts are never used to auto-fill Closeout's
// own separate aggregate fields.
export default function EngagementDetailPage() {
  const params = useParams<{ id: string; engagementId: string }>();
  const clientId = params.id;
  const engagementId = params.engagementId;

  const [client, setClient] = useState<Client | null>(null);
  const [engagement, setEngagement] = useState<Engagement | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editOpen, setEditOpen] = useState(false);
  const [archiving, setArchiving] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  // undefined = still loading; null = confirmed no Closeout recorded yet
  // (a normal, expected state, not an error); an object = the recorded
  // Closeout (possibly archived -- rendered with its own Archived state).
  const [closeout, setCloseout] = useState<EngagementCloseout | null | undefined>(undefined);
  const [closeoutError, setCloseoutError] = useState<string | null>(null);
  const [closeoutModalOpen, setCloseoutModalOpen] = useState(false);
  const [archivingCloseout, setArchivingCloseout] = useState(false);
  const [closeoutActionError, setCloseoutActionError] = useState<string | null>(null);

  const [participants, setParticipants] = useState<EngagementParticipantView[] | null>(null);
  const [participantsError, setParticipantsError] = useState<string | null>(null);
  const [participantModalOpen, setParticipantModalOpen] = useState(false);
  const [editingParticipant, setEditingParticipant] = useState<EngagementParticipant | null>(null);
  const [busyParticipantId, setBusyParticipantId] = useState<string | null>(null);
  const [participantActionError, setParticipantActionError] = useState<string | null>(null);

  // Client CRM Stage 1H-A -- Luma <-> Engagement link. undefined = still
  // resolving; null = confirmed unlinked; an object = the linked Luma
  // event's read-only summary (fetched separately, since Engagement only
  // stores the bare luma_event_id). Link-only: no participant sync exists
  // yet, so linking/unlinking never touches Participants above.
  const [lumaEvent, setLumaEvent] = useState<LumaEventSummary | null | undefined>(undefined);
  const [lumaEventError, setLumaEventError] = useState<string | null>(null);
  const [savingLumaLink, setSavingLumaLink] = useState(false);
  const [lumaLinkError, setLumaLinkError] = useState<string | null>(null);

  async function load() {
    try {
      const [loadedClient, loadedEngagement] = await Promise.all([
        getClient(clientId),
        getClientEngagement(clientId, engagementId),
      ]);
      setClient(loadedClient);
      setEngagement(loadedEngagement);
      setNotFound(false);
      setError(null);
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        setNotFound(true);
        return;
      }
      setError(err instanceof ApiError ? `Couldn't load this Engagement (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    }
  }

  async function loadCloseout() {
    try {
      setCloseout(await getEngagementCloseout(clientId, engagementId));
      setCloseoutError(null);
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        setCloseout(null); // confirmed: none recorded yet
        setCloseoutError(null);
        return;
      }
      setCloseoutError(
        err instanceof ApiError ? `Couldn't load the Closeout (${err.status}): ${err.message}` : "Couldn't reach the backend."
      );
    }
  }

  async function loadParticipants() {
    try {
      setParticipants(await listEngagementParticipants(clientId, engagementId));
      setParticipantsError(null);
    } catch (err) {
      setParticipantsError(
        err instanceof ApiError ? `Couldn't load Participants (${err.status}): ${err.message}` : "Couldn't reach the backend."
      );
    }
  }

  useEffect(() => {
    load();
    loadCloseout();
    loadParticipants();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clientId, engagementId]);

  useEffect(() => {
    if (engagement === null) return;
    if (!engagement.luma_event_id) {
      setLumaEvent(null);
      setLumaEventError(null);
      return;
    }
    let cancelled = false;
    setLumaEvent(undefined);
    setLumaEventError(null);
    getLumaEvent(engagement.luma_event_id)
      .then((event) => {
        if (!cancelled) setLumaEvent(event);
      })
      .catch((err) => {
        if (cancelled) return;
        setLumaEventError(
          err instanceof ApiError ? `Couldn't load the linked Luma event (${err.status}): ${err.message}` : "Couldn't reach the backend."
        );
        setLumaEvent(null);
      });
    return () => {
      cancelled = true;
    };
  }, [engagement]);

  async function handleArchiveCloseoutToggle() {
    if (!closeout) return;
    const nextArchived = !closeout.archived;
    if (nextArchived) {
      const confirmed = window.confirm("Archive this Closeout? It can be restored anytime.");
      if (!confirmed) return;
    }
    setArchivingCloseout(true);
    setCloseoutActionError(null);
    try {
      setCloseout(await updateEngagementCloseout(clientId, engagementId, { archived: nextArchived }));
    } catch (err) {
      setCloseoutActionError(
        err instanceof ApiError ? `Couldn't update the Closeout (${err.status}): ${err.message}` : "Couldn't reach the backend."
      );
    } finally {
      setArchivingCloseout(false);
    }
  }

  async function handleArchiveParticipantToggle(participant: EngagementParticipantView) {
    const nextArchived = !participant.archived;
    if (nextArchived) {
      const confirmed = window.confirm(`Archive ${participant.resolved_name}? They can be restored anytime.`);
      if (!confirmed) return;
    }
    setBusyParticipantId(participant.participant_id);
    setParticipantActionError(null);
    try {
      await updateEngagementParticipant(clientId, engagementId, participant.participant_id, { archived: nextArchived });
      // Re-fetch rather than patch the plain EngagementParticipant this
      // update call returns into local state -- that response has no
      // resolved_* fields (Stage 4A), and the list endpoint is the one
      // place that resolves them server-side. See loadParticipants().
      await loadParticipants();
    } catch (err) {
      setParticipantActionError(
        err instanceof ApiError ? `Couldn't update this Participant (${err.status}): ${err.message}` : "Couldn't reach the backend."
      );
    } finally {
      setBusyParticipantId(null);
    }
  }

  function handleParticipantSaved() {
    // Same reasoning as handleArchiveParticipantToggle -- re-fetch via the
    // list endpoint so the resolved_* display fields stay correct, rather
    // than patching in the plain EngagementParticipant this callback
    // receives (create/update intentionally do not return resolved fields).
    void loadParticipants();
  }

  async function handleLinkLumaEvent(event: LumaEventSummary) {
    if (!engagement) return;
    setSavingLumaLink(true);
    setLumaLinkError(null);
    try {
      const saved = await updateClientEngagement(clientId, engagement.engagement_id, { luma_event_id: event.luma_event_id });
      setEngagement(saved);
      setLumaEvent(event);
    } catch (err) {
      setLumaLinkError(
        err instanceof ApiError ? `Couldn't link this Luma event (${err.status}): ${err.message}` : "Couldn't reach the backend."
      );
    } finally {
      setSavingLumaLink(false);
    }
  }

  async function handleUnlinkLumaEvent() {
    if (!engagement) return;
    setSavingLumaLink(true);
    setLumaLinkError(null);
    try {
      const saved = await updateClientEngagement(clientId, engagement.engagement_id, { luma_event_id: null });
      setEngagement(saved);
      setLumaEvent(null);
    } catch (err) {
      setLumaLinkError(
        err instanceof ApiError ? `Couldn't unlink this Luma event (${err.status}): ${err.message}` : "Couldn't reach the backend."
      );
    } finally {
      setSavingLumaLink(false);
    }
  }

  async function handleArchiveToggle() {
    if (!engagement) return;
    const nextArchived = !engagement.archived;
    if (nextArchived) {
      const confirmed = window.confirm(
        `Archive "${engagement.title}"? It will be hidden from the default Engagements list but can be restored anytime.`
      );
      if (!confirmed) return;
    }
    setArchiving(true);
    setActionError(null);
    try {
      setEngagement(await updateClientEngagement(clientId, engagement.engagement_id, { archived: nextArchived }));
    } catch (err) {
      setActionError(err instanceof ApiError ? `Couldn't update this Engagement (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    } finally {
      setArchiving(false);
    }
  }

  if (notFound) {
    return (
      <div className={CLIENT_CRM_DETAIL_CONTAINER_CLASS}>
        <Alert>
          <AlertTriangle />
          <AlertTitle>Engagement not found</AlertTitle>
          <AlertDescription>
            This Engagement doesn&apos;t exist, or may have been removed.{" "}
            <Link href={`/clients/${clientId}`} className="underline underline-offset-2">
              Back to Client
            </Link>
            .
          </AlertDescription>
        </Alert>
      </div>
    );
  }

  if (error) {
    return (
      <div className={CLIENT_CRM_DETAIL_CONTAINER_CLASS}>
        <Alert variant="destructive">
          <AlertTriangle />
          <AlertTitle>Couldn&apos;t load this Engagement</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      </div>
    );
  }

  if (!client || !engagement) {
    return <div className={cn(CLIENT_CRM_DETAIL_CONTAINER_CLASS, "text-sm text-muted-foreground")}>Loading…</div>;
  }

  return (
    <div className={CLIENT_CRM_DETAIL_CONTAINER_CLASS}>
      <Link
        href={`/clients/${client.client_id}`}
        className="mb-4 inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="h-4 w-4" />
        {client.name}
      </Link>

      {engagement.archived && (
        <Alert className="mb-4">
          <Lock className="h-4 w-4" />
          <AlertTitle>Archived</AlertTitle>
          <AlertDescription>
            This Engagement is archived and hidden from the default Engagements list. Its record is unchanged and can be
            restored anytime.
          </AlertDescription>
        </Alert>
      )}

      {actionError && (
        <Alert variant="destructive" className="mb-4">
          <AlertTriangle />
          <AlertDescription>{actionError}</AlertDescription>
        </Alert>
      )}

      <div className="mb-6 flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="font-serif text-2xl font-medium tracking-tight sm:text-3xl">{engagement.title}</h1>
            <Badge variant="secondary" className={engagementStatusBadgeClass(engagement.status)}>
              {engagementStatusLabel(engagement.status)}
            </Badge>
          </div>
          <p className="mt-1 text-sm text-muted-foreground">
            {formatEngagementDate(engagement.engagement_date)}
            {engagement.location ? ` · ${engagement.location}` : ""}
          </p>
        </div>
        <div className="flex shrink-0 gap-2">
          <Button size="sm" onClick={() => setEditOpen(true)}>
            Edit Engagement
          </Button>
          <Button
            size="sm"
            variant="outline"
            onClick={handleArchiveToggle}
            disabled={archiving}
            className={engagement.archived ? undefined : "text-muted-foreground hover:text-destructive"}
          >
            {archiving ? "Saving..." : engagement.archived ? "Restore Engagement" : "Archive Engagement"}
          </Button>
        </div>
      </div>

      <div className="space-y-6">
        <div className={ENGAGEMENT_OVERVIEW_LUMA_GRID_CLASS}>
          <Card>
            <CardHeader>
              <CardTitle className="text-sm">Overview</CardTitle>
            </CardHeader>
            <CardContent className="grid gap-4 sm:grid-cols-2">
              <OverviewField label="Engagement Type" value={engagementTypeLabel(engagement.engagement_type)} />
              <OverviewField label="Dinner Type" value={dinnerTypeLabel(engagement.dinner_type)} />
              <OverviewField label="Date" value={formatEngagementDate(engagement.engagement_date)} />
              <OverviewField label="Location" value={engagement.location || "—"} />
              <OverviewField label="Owner" value={engagement.owner || "—"} />
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="text-sm">Linked Luma Event</CardTitle>
            </CardHeader>
            <CardContent>
              {lumaEventError && (
                <Alert variant="destructive" className="mb-3">
                  <AlertDescription>{lumaEventError}</AlertDescription>
                </Alert>
              )}
              {lumaLinkError && (
                <Alert variant="destructive" className="mb-3">
                  <AlertDescription>{lumaLinkError}</AlertDescription>
                </Alert>
              )}

              {lumaEvent === undefined && !lumaEventError && <p className="text-sm text-muted-foreground">Loading…</p>}

              {lumaEvent === null && (
                <LumaEventPicker selected={null} onSelect={(event) => event && handleLinkLumaEvent(event)} />
              )}

              {lumaEvent && (
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <p className="font-medium">{lumaEvent.name}</p>
                    <p className="text-xs text-muted-foreground">
                      {formatEngagementDate(lumaEvent.start_at)}
                      {lumaEvent.location_summary ? ` · ${lumaEvent.location_summary}` : ""}
                    </p>
                    {lumaEvent.url && (
                      <a
                        href={lumaEvent.url}
                        target="_blank"
                        rel="noreferrer"
                        className="mt-1 inline-flex items-center gap-1 text-xs text-foreground hover:underline"
                      >
                        Open in Luma
                        <ExternalLink className="h-3 w-3" />
                      </a>
                    )}
                  </div>
                  <Button size="sm" variant="outline" disabled={savingLumaLink} onClick={handleUnlinkLumaEvent}>
                    {savingLumaLink ? "Saving..." : "Unlink"}
                  </Button>
                </div>
              )}
            </CardContent>
          </Card>
        </div>

        <Card>
          <CardHeader>
            <CardTitle className="text-sm">Commercial</CardTitle>
          </CardHeader>
          <CardContent className="grid gap-4 sm:grid-cols-2">
            <OverviewField label="Engagement Fee" value={engagement.fee === null ? "—" : `$${engagement.fee.toLocaleString()}`} />
            <OverviewField label="Contract Status" value={engagementContractStatusLabel(engagement.contract_status)} />
            <OverviewField label="Signed Date" value={formatEngagementDate(engagement.signed_date)} />
            <OverviewField label="Payment Status" value={engagementPaymentStatusLabel(engagement.payment_status)} />
            {engagement.contract_url && (
              <div className="sm:col-span-2">
                <p className="text-xs font-medium text-muted-foreground">Contract URL</p>
                <a
                  href={engagement.contract_url}
                  target="_blank"
                  rel="noreferrer"
                  className="mt-0.5 inline-block break-all text-sm text-foreground hover:underline"
                >
                  {engagement.contract_url}
                </a>
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0">
            <CardTitle className="text-sm">Closeout</CardTitle>
            {closeout && !closeout.archived && (
              <div className="flex gap-2">
                <Button size="sm" variant="outline" onClick={() => setCloseoutModalOpen(true)}>
                  Edit Closeout
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={archivingCloseout}
                  className="text-muted-foreground hover:text-destructive"
                  onClick={handleArchiveCloseoutToggle}
                >
                  {archivingCloseout ? "Saving..." : "Archive Closeout"}
                </Button>
              </div>
            )}
            {closeout && closeout.archived && (
              <Button size="sm" variant="outline" disabled={archivingCloseout} onClick={handleArchiveCloseoutToggle}>
                {archivingCloseout ? "Saving..." : "Restore Closeout"}
              </Button>
            )}
          </CardHeader>
          <CardContent>
            {closeoutError && (
              <Alert variant="destructive" className="mb-3">
                <AlertDescription>{closeoutError}</AlertDescription>
              </Alert>
            )}
            {closeoutActionError && (
              <Alert variant="destructive" className="mb-3">
                <AlertDescription>{closeoutActionError}</AlertDescription>
              </Alert>
            )}

            {closeout === undefined && !closeoutError && <p className="text-sm text-muted-foreground">Loading…</p>}

            {closeout === null && (
              <div className="flex flex-col items-start gap-3">
                <p className="text-sm text-muted-foreground">No closeout recorded yet</p>
                <Button size="sm" onClick={() => setCloseoutModalOpen(true)}>
                  Add Closeout
                </Button>
              </div>
            )}

            {closeout && (
              <div className="space-y-6">
                {closeout.archived && (
                  <Alert>
                    <Lock className="h-4 w-4" />
                    <AlertTitle>Archived</AlertTitle>
                    <AlertDescription>This Closeout is archived. Its record is unchanged and can be restored anytime.</AlertDescription>
                  </Alert>
                )}

                <div>
                  <p className="mb-2 text-xs font-medium text-muted-foreground">Turnout</p>
                  <div className="grid gap-4 sm:grid-cols-5">
                    <OverviewField label="Confirmed" value={formatCloseoutCount(closeout.confirmed_guest_count)} />
                    <OverviewField label="Attended" value={formatCloseoutCount(closeout.attended_count)} />
                    <OverviewField label="No-Shows" value={formatCloseoutCount(closeout.no_show_count)} />
                    <OverviewField label="Cancelled" value={formatCloseoutCount(closeout.cancelled_count)} />
                    <OverviewField label="Unexpected/Walk-ins" value={formatCloseoutCount(closeout.unexpected_attendee_count)} />
                  </div>
                  <p className="mt-2 text-xs text-muted-foreground">
                    Attendance rate: {formatAttendanceRate(engagementCloseoutAttendanceRate(closeout))}
                  </p>
                </div>

                {(closeout.guest_quality || closeout.dinner_dynamics || closeout.initial_client_experience) && (
                  <div>
                    <p className="mb-2 text-xs font-medium text-muted-foreground">Dinner Review</p>
                    <div className="space-y-3">
                      {closeout.guest_quality && <OverviewField label="Guest Quality" value={closeout.guest_quality} />}
                      {closeout.dinner_dynamics && <OverviewField label="Dinner Dynamics" value={closeout.dinner_dynamics} />}
                      {closeout.initial_client_experience && (
                        <OverviewField label="Initial Client Experience" value={closeout.initial_client_experience} />
                      )}
                    </div>
                  </div>
                )}

                {(closeout.immediate_outcomes || closeout.notable_signals || closeout.issues || closeout.referrals || closeout.future_opportunities) && (
                  <div>
                    <p className="mb-2 text-xs font-medium text-muted-foreground">Outcomes</p>
                    <div className="space-y-3">
                      {closeout.immediate_outcomes && <OverviewField label="Immediate Outcomes" value={closeout.immediate_outcomes} />}
                      {closeout.notable_signals && <OverviewField label="Notable Signals" value={closeout.notable_signals} />}
                      {closeout.issues && <OverviewField label="Issues / Problems" value={closeout.issues} />}
                      {closeout.referrals && <OverviewField label="Referrals" value={closeout.referrals} />}
                      {closeout.future_opportunities && <OverviewField label="Future Opportunities" value={closeout.future_opportunities} />}
                    </div>
                  </div>
                )}

                <div>
                  <p className="mb-2 text-xs font-medium text-muted-foreground">Internal</p>
                  <div className="grid gap-4 sm:grid-cols-2">
                    {closeout.internal_notes && (
                      <div className="sm:col-span-2">
                        <OverviewField label="Internal Closeout Notes" value={closeout.internal_notes} />
                      </div>
                    )}
                    <OverviewField label="Completed By" value={closeout.completed_by || "—"} />
                    <OverviewField label="Completed At" value={formatEngagementDate(closeout.completed_at)} />
                  </div>
                </div>
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0">
            <CardTitle className="text-sm">Participants</CardTitle>
            <Button
              size="sm"
              onClick={() => {
                setEditingParticipant(null);
                setParticipantModalOpen(true);
              }}
            >
              Add Participant
            </Button>
          </CardHeader>
          <CardContent>
            {participantsError && (
              <Alert variant="destructive" className="mb-3">
                <AlertDescription>{participantsError}</AlertDescription>
              </Alert>
            )}
            {participantActionError && (
              <Alert variant="destructive" className="mb-3">
                <AlertDescription>{participantActionError}</AlertDescription>
              </Alert>
            )}

            {participants === null && !participantsError && <p className="text-sm text-muted-foreground">Loading…</p>}

            {participants !== null && participants.length === 0 && (
              <p className="text-sm text-muted-foreground">No Participants recorded yet.</p>
            )}

            {participants !== null && participants.length > 0 && (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead className="bg-secondary/40 text-xs text-muted-foreground">
                    <tr>
                      <th className="px-3 py-2 text-left font-medium">Name</th>
                      <th className="px-3 py-2 text-left font-medium">Title</th>
                      <th className="px-3 py-2 text-left font-medium">Company</th>
                      <th className="px-3 py-2 text-left font-medium">Role</th>
                      <th className="px-3 py-2 text-left font-medium">RSVP</th>
                      <th className="px-3 py-2 text-left font-medium">Attendance</th>
                      <th className="px-3 py-2 text-left font-medium">Actions</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border/60">
                    {participants.map((participant) => (
                      <tr key={participant.participant_id} className="hover:bg-secondary/30">
                        <td className="px-3 py-2.5 font-medium">
                          <div className="flex flex-wrap items-center gap-1.5">
                            {participant.crm_contact_id ? (
                              <Link href={`/crm/${participant.crm_contact_id}`} className="hover:underline">
                                {participant.resolved_name}
                              </Link>
                            ) : (
                              <span>{participant.resolved_name}</span>
                            )}
                            {participant.is_walk_in && (
                              <Badge variant="secondary" className="bg-amber-100 text-amber-800">
                                Walk-in
                              </Badge>
                            )}
                            {participant.archived && (
                              <Badge variant="secondary" className="bg-secondary text-muted-foreground">
                                Archived
                              </Badge>
                            )}
                          </div>
                        </td>
                        <td className="px-3 py-2.5 text-muted-foreground">{participant.resolved_title || "—"}</td>
                        <td className="px-3 py-2.5 text-muted-foreground">{participant.resolved_company || "—"}</td>
                        <td className="px-3 py-2.5 text-muted-foreground">{participantRoleLabel(participant.role)}</td>
                        <td className="px-3 py-2.5 text-muted-foreground">
                          {participantRsvpStatusLabel(participant.rsvp_status, participant.decline_origin)}
                        </td>
                        <td className="px-3 py-2.5 text-muted-foreground">
                          {participantAttendanceStatusLabel(participant.attendance_status)}
                        </td>
                        <td className="px-3 py-2.5">
                          <div className="flex flex-wrap gap-2">
                            <Button
                              size="sm"
                              variant="outline"
                              disabled={busyParticipantId === participant.participant_id}
                              onClick={() => {
                                setEditingParticipant(participant);
                                setParticipantModalOpen(true);
                              }}
                            >
                              Edit
                            </Button>
                            <Button
                              size="sm"
                              variant="outline"
                              disabled={busyParticipantId === participant.participant_id}
                              className={participant.archived ? undefined : "text-muted-foreground hover:text-destructive"}
                              onClick={() => handleArchiveParticipantToggle(participant)}
                            >
                              {participant.archived ? "Restore" : "Archive"}
                            </Button>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      <EngagementFormModal
        open={editOpen}
        onOpenChange={setEditOpen}
        client={client}
        existingEngagement={engagement}
        onSaved={setEngagement}
      />
      <EngagementCloseoutFormModal
        open={closeoutModalOpen}
        onOpenChange={setCloseoutModalOpen}
        client={client}
        engagement={engagement}
        existingCloseout={closeout ?? null}
        onSaved={setCloseout}
      />
      <EngagementParticipantFormModal
        open={participantModalOpen}
        onOpenChange={setParticipantModalOpen}
        client={client}
        engagement={engagement}
        existingParticipant={editingParticipant}
        onSaved={handleParticipantSaved}
      />
    </div>
  );
}

function formatCloseoutCount(value: number | null): string {
  return value === null ? "—" : String(value);
}

function formatAttendanceRate(rate: number | null): string {
  return rate === null ? "—" : `${Math.round(rate * 100)}%`;
}

function OverviewField({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="text-xs font-medium text-muted-foreground">{label}</p>
      <p className="mt-0.5 text-sm">{value}</p>
    </div>
  );
}
