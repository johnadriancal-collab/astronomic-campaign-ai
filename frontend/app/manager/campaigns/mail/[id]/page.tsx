"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { AlertTriangle, ArrowLeft, Lock } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Tabs, TabsList, TabsPanel, TabsTab } from "@/components/ui/tabs";
import { MailCampaignHeader } from "@/components/mail-campaign-header";
import { MailCampaignDashboardTab } from "@/components/mail-campaign-dashboard-tab";
import { MailCampaignLeadsTab } from "@/components/mail-campaign-leads-tab";
import { MailCampaignStepsTab } from "@/components/mail-campaign-steps-tab";
import { MailCampaignChannelsTab } from "@/components/mail-campaign-channels-tab";
import { MailCampaignScheduleTab, type TabWindow } from "@/components/mail-campaign-schedule-tab";
import { MailCampaignSettingsTab } from "@/components/mail-campaign-settings-tab";
import { MAIL_CAMPAIGN_DETAIL_CONTAINER_CLASS } from "@/lib/mail-campaign-layout";
import { campaignLockedBannerDescription, campaignLockedBannerTitle } from "@/lib/mail";
import { findOverlappingPairs, isUnsavedWindowId, minutesFromTimeString, timeStringFromMinutes } from "@/lib/schedule";
import type { StepSelection } from "@/lib/mail-campaign-steps";
import { cn } from "@/lib/utils";
import {
  addMailSequenceStep,
  archiveMailCampaign,
  ApiError,
  deleteMailSequenceStep,
  getMailCampaign,
  getMailCampaignChannels,
  getMailCampaignReview,
  getMailCampaignSchedule,
  getMailCampaignWorkload,
  listCrmLists,
  listMailboxes,
  listMailCampaignBatches,
  listMailEnrollments,
  listMailSequenceSteps,
  markMailCampaignReady,
  reorderMailSequenceSteps,
  setMailCampaignChannels,
  setMailCampaignSchedule,
  unlockMailCampaign,
  updateMailCampaign,
  updateMailSequenceStep,
  type CrmContactListSummary,
  type Mailbox,
  type MailCampaign,
  type MailCampaignReview,
  type MailCampaignSharing,
  type MailCampaignWorkload,
  type MailEnrollment,
  type MailEnrollmentBatch,
  type MailSequenceStep,
} from "@/lib/api";

export default function MailCampaignDetailPage() {
  const params = useParams<{ id: string }>();
  const campaignId = params.id;

  const [campaign, setCampaign] = useState<MailCampaign | null>(null);
  const [steps, setSteps] = useState<MailSequenceStep[]>([]);
  const [review, setReview] = useState<MailCampaignReview | null>(null);
  const [enrollments, setEnrollments] = useState<MailEnrollment[]>([]);
  const [workload, setWorkload] = useState<MailCampaignWorkload | null>(null);
  const [batches, setBatches] = useState<MailEnrollmentBatch[]>([]);
  const [lists, setLists] = useState<CrmContactListSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [name, setName] = useState("");
  const [sourceListId, setSourceListId] = useState("");
  const [timezone, setTimezone] = useState("");
  const [windows, setWindows] = useState<TabWindow[]>([]);
  const [savingSchedule, setSavingSchedule] = useState(false);
  const [scheduleError, setScheduleError] = useState<string | null>(null);

  const [sharing, setSharing] = useState<MailCampaignSharing>("everyone");
  const [startImmediately, setStartImmediately] = useState(false);
  const [dailyLeadStartLimit, setDailyLeadStartLimit] = useState("");
  const [savingSettings, setSavingSettings] = useState(false);
  const [settingsError, setSettingsError] = useState<string | null>(null);

  // The Steps tab's sequence builder: `selection` is the user's EXPLICIT
  // choice of which ONE node (an Email step, a Wait node, or the not-yet-
  // saved "new email" being composed) the right-hand editor shows --
  // `null` means "no explicit choice yet," not "nothing shown." The draft
  // text/number fields themselves (subject/body/delay) live locally inside
  // mail-campaign-step-editor.tsx, not here -- see that file's docstring
  // for why (a key-based remount, not an effect, is what seeds them
  // correctly on every selection change, including the very first one).
  const [selection, setSelection] = useState<StepSelection>(null);
  const [savingSelection, setSavingSelection] = useState(false);
  const [selectionError, setSelectionError] = useState<string | null>(null);

  // The actual selection shown to the Steps tab: the user's explicit
  // choice if they've made one, otherwise Step 1 (or nothing, for an
  // empty sequence) -- computed fresh every render, purely from current
  // `selection`/`steps`, never written back into state. This is what
  // auto-selects Step 1 on first load (and after Step 1 itself is
  // deleted, etc.) WITHOUT a useEffect: there is nothing to "sync," only
  // a value to compute. An explicit `selection` (any step id, including
  // steps[0]'s) always wins and survives untouched across a steps[]
  // refetch/reorder, since it's compared by id, never by array position.
  const effectiveSelection: StepSelection = selection ?? (steps.length > 0 ? { type: "email", stepId: steps[0].step_id } : null);

  const [mailboxes, setMailboxes] = useState<Mailbox[] | null>(null);
  const [selectedMailboxIds, setSelectedMailboxIds] = useState<string[]>([]);
  const [savingChannels, setSavingChannels] = useState(false);
  const [channelsError, setChannelsError] = useState<string | null>(null);

  const editable = campaign?.status === "draft";

  async function load() {
    try {
      const [c, s, r, e, w, b, l, mb, ch, sched] = await Promise.all([
        getMailCampaign(campaignId),
        listMailSequenceSteps(campaignId),
        getMailCampaignReview(campaignId),
        listMailEnrollments(campaignId),
        getMailCampaignWorkload(campaignId),
        listMailCampaignBatches(campaignId),
        listCrmLists(),
        listMailboxes(),
        getMailCampaignChannels(campaignId),
        getMailCampaignSchedule(campaignId),
      ]);
      setCampaign(c);
      setSteps(s);
      setReview(r);
      setEnrollments(e);
      setWorkload(w);
      setBatches(b);
      setLists(l);
      setMailboxes(mb);
      setSelectedMailboxIds(ch);
      setName(c.name);
      setSourceListId(c.source_list_id ?? "");
      setTimezone(sched.timezone ?? "");
      setWindows(
        sched.windows.map((w) => ({
          id: w.window_id,
          day: w.day_of_week,
          start: minutesFromTimeString(w.start_time),
          end: minutesFromTimeString(w.end_time),
        }))
      );
      setSharing(c.sharing);
      setStartImmediately(c.start_immediately);
      setDailyLeadStartLimit(c.daily_lead_start_limit === null ? "" : String(c.daily_lead_start_limit));
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? `Couldn't load this campaign (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [campaignId]);

  async function refreshReview() {
    try {
      setReview(await getMailCampaignReview(campaignId));
    } catch {
      // Review is a convenience panel -- a refresh failure here doesn't block anything else.
    }
  }

  // Called after a successful Add Prospects (CRM List or CSV) so the Leads
  // tab reflects the new batch immediately, without a full page reload --
  // see MailCampaignLeadsTab's onProspectsAdded prop. Deliberately
  // re-fetches ONLY the four things Add Prospects can actually change
  // (campaign -- its status may have just reopened from a legacy
  // COMPLETED via Stage 3's own reopening rule; enrollments; workload;
  // batch history), never steps/schedule/channels/mailboxes/settings --
  // this never resets campaign lifecycle/configuration state, it only
  // shows whatever it genuinely now is.
  async function refreshLeadsSection() {
    try {
      const [c, e, w, b] = await Promise.all([
        getMailCampaign(campaignId),
        listMailEnrollments(campaignId),
        getMailCampaignWorkload(campaignId),
        listMailCampaignBatches(campaignId),
      ]);
      setCampaign(c);
      setEnrollments(e);
      setWorkload(w);
      setBatches(b);
    } catch {
      // Best-effort refresh -- the modal already showed the user their
      // successful result; a refresh hiccup here doesn't undo that or
      // block anything else on the page.
    }
  }

  async function handleSaveDetails() {
    setBusy(true);
    setActionError(null);
    try {
      const patch: Record<string, unknown> = { name, source_list_id: sourceListId || null };
      const updated = await updateMailCampaign(campaignId, patch);
      setCampaign(updated);
      await refreshReview();
    } catch (err) {
      setActionError(err instanceof ApiError ? `Couldn't save (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    } finally {
      setBusy(false);
    }
  }

  async function handleSaveSchedule() {
    // Client-side pre-check using the same overlap rule the backend
    // enforces (see lib/schedule.ts's findOverlappingPairs) -- catches an
    // obviously-broken schedule (possible via the manual time inputs,
    // which don't live-clamp like dragging does) before a network round
    // trip, but the backend's own rejection remains the real guarantee.
    for (const day of [0, 1, 2, 3, 4, 5, 6]) {
      const dayWindows = windows.filter((w) => w.day === day);
      if (findOverlappingPairs(dayWindows).length > 0) {
        setScheduleError(`Two send windows on the same day overlap -- fix that before saving.`);
        return;
      }
    }

    setSavingSchedule(true);
    setScheduleError(null);
    try {
      // Preserve identity for windows the server already knows about --
      // any synthetic, not-yet-persisted window (see lib/schedule.ts's
      // isUnsavedWindowId: covers both a brand-new "new-..." window AND a
      // legacy campaign's "legacy-..." fallback windows, which the GET
      // .../schedule fallback fabricates fresh every time but never saves)
      // omits window_id so the backend mints a real one for it instead of
      // rejecting an id it never persisted. A dragged/resized EXISTING
      // window keeps its real id here (only start/end changed, not
      // `w.id`), so the backend correctly treats it as the same entity,
      // not a delete+recreate.
      const payload = windows.map((w) => ({
        window_id: isUnsavedWindowId(w.id) ? undefined : w.id,
        day_of_week: w.day,
        start_time: timeStringFromMinutes(w.start),
        end_time: timeStringFromMinutes(w.end),
      }));
      const updated = await setMailCampaignSchedule(campaignId, timezone, payload);
      setWindows(
        updated.windows.map((w) => ({
          id: w.window_id,
          day: w.day_of_week,
          start: minutesFromTimeString(w.start_time),
          end: minutesFromTimeString(w.end_time),
        }))
      );
      await refreshReview();
    } catch (err) {
      setScheduleError(
        err instanceof ApiError ? `Couldn't save schedule (${err.status}): ${err.message}` : "Couldn't reach the backend."
      );
    } finally {
      setSavingSchedule(false);
    }
  }

  async function handleSaveSettings() {
    if (dailyLeadStartLimit.trim() !== "") {
      const parsed = Number(dailyLeadStartLimit);
      if (!Number.isInteger(parsed) || parsed < 1) {
        setSettingsError("Number of leads to start daily must be a positive integer.");
        return;
      }
    }
    setSavingSettings(true);
    setSettingsError(null);
    try {
      const updated = await updateMailCampaign(campaignId, {
        sharing,
        start_immediately: startImmediately,
        daily_lead_start_limit: dailyLeadStartLimit.trim() === "" ? null : Number(dailyLeadStartLimit),
      });
      setCampaign(updated);
    } catch (err) {
      setSettingsError(err instanceof ApiError ? `Couldn't save (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    } finally {
      setSavingSettings(false);
    }
  }

  // --- Steps tab: sequence builder selection/save handlers ---------------
  //
  // Delay is edited in exactly one place per step: Step 1's is permanently
  // 0 and never editable anywhere; a Step 2+'s is edited ONLY via its own
  // Wait node (handleSaveWaitDelay), never duplicated as a field in the
  // Email editor. handleSaveEmail therefore never sends delay_days at all --
  // for a legacy Step 1 whose stored delay_days predates that invariant,
  // the backend still self-heals it to 0 on this same save regardless (see
  // MailCampaignService.update_step()'s docstring), patch key present or not.

  function handleSelectEmail(step: MailSequenceStep) {
    setSelection({ type: "email", stepId: step.step_id });
    setSelectionError(null);
  }

  function handleSelectWait(step: MailSequenceStep) {
    setSelection({ type: "wait", stepId: step.step_id });
    setSelectionError(null);
  }

  function handleStartAddStep() {
    setSelection({ type: "new-email" });
    setSelectionError(null);
  }

  // The only selection-clearing Cancel left in page.tsx -- reverting an
  // Email/Wait draft back to its persisted values is now purely local to
  // mail-campaign-step-editor.tsx (it owns that draft state and already
  // has the persisted step via props, so there's nothing for page.tsx to
  // do). A "new email" has no persisted step to revert to, so cancelling
  // it clears the selection entirely instead. No backend write either way.
  function handleCancelNewStep() {
    setSelection(null);
    setSelectionError(null);
  }

  async function handleAddStep(subject: string, body: string, delayDaysInput: number) {
    if (!subject.trim() || !body.trim() || savingSelection) return;
    setSavingSelection(true);
    setSelectionError(null);
    // The backend forces delay_days=0 for a first step regardless of what's
    // sent, but submitting it explicitly here too keeps the request honest
    // about what's about to be stored -- the empty-sequence editor never
    // shows a Delay input, so `delayDaysInput` may still hold a stale value
    // from a step added/removed earlier in this session.
    const delayDays = steps.length === 0 ? 0 : delayDaysInput;
    try {
      const created = await addMailSequenceStep(campaignId, { subject, body, delay_days: delayDays });
      setSteps(await listMailSequenceSteps(campaignId));
      setSelection({ type: "email", stepId: created.step_id });
      await refreshReview();
    } catch (err) {
      setSelectionError(err instanceof ApiError ? `Couldn't add step (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    } finally {
      setSavingSelection(false);
    }
  }

  async function handleDeleteStep(stepId: string) {
    setBusy(true);
    setActionError(null);
    try {
      // If the deleted step was the one showing in the editor (whether
      // selected as an Email node or as the Wait node representing its
      // own delay -- both keyed by this same stepId, see
      // lib/mail-campaign-steps.ts), select a sensible survivor instead of
      // just dropping back to null: prefer whichever step now occupies
      // that same position (the one immediately after it, shifted down),
      // else the one immediately before it, else there's nothing left.
      const wasSelected = effectiveSelection !== null && effectiveSelection.type !== "new-email" && effectiveSelection.stepId === stepId;
      const deletedIndex = steps.findIndex((s) => s.step_id === stepId);
      const remaining = await deleteMailSequenceStep(campaignId, stepId);
      setSteps(remaining);
      if (wasSelected) {
        const survivor = remaining[deletedIndex] ?? remaining[deletedIndex - 1] ?? null;
        setSelection(survivor ? { type: "email", stepId: survivor.step_id } : null);
      }
      await refreshReview();
    } catch (err) {
      setActionError(err instanceof ApiError ? `Couldn't delete step (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    } finally {
      setBusy(false);
    }
  }

  async function handleSaveEmail(stepId: string, subject: string, body: string) {
    if (!subject.trim() || !body.trim() || savingSelection) return;
    setSavingSelection(true);
    setSelectionError(null);
    try {
      const updated = await updateMailSequenceStep(campaignId, stepId, { subject, body });
      setSteps((prev) => prev.map((s) => (s.step_id === stepId ? updated : s)));
      await refreshReview();
    } catch (err) {
      setSelectionError(err instanceof ApiError ? `Couldn't save step (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    } finally {
      setSavingSelection(false);
    }
  }

  async function handleSaveWaitDelay(stepId: string, delayDays: number) {
    if (delayDays < 0 || savingSelection) return;
    setSavingSelection(true);
    setSelectionError(null);
    try {
      const updated = await updateMailSequenceStep(campaignId, stepId, { delay_days: delayDays });
      setSteps((prev) => prev.map((s) => (s.step_id === stepId ? updated : s)));
      await refreshReview();
    } catch (err) {
      setSelectionError(err instanceof ApiError ? `Couldn't save delay (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    } finally {
      setSavingSelection(false);
    }
  }

  async function handleMoveStep(index: number, direction: -1 | 1) {
    const target = index + direction;
    if (target < 0 || target >= steps.length) return;
    const reordered = [...steps];
    [reordered[index], reordered[target]] = [reordered[target], reordered[index]];
    setBusy(true);
    setActionError(null);
    try {
      setSteps(await reorderMailSequenceSteps(campaignId, reordered.map((s) => s.step_id)));
    } catch (err) {
      setActionError(err instanceof ApiError ? `Couldn't reorder steps (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    } finally {
      setBusy(false);
    }
  }

  function handleToggleChannel(mailboxId: string, selected: boolean) {
    if (campaign?.status === "archived") return;
    setSelectedMailboxIds((prev) =>
      selected ? [...prev, mailboxId] : prev.filter((id) => id !== mailboxId)
    );
  }

  async function handleSaveChannels() {
    if (campaign?.status === "archived") return;
    setSavingChannels(true);
    setChannelsError(null);
    try {
      const updated = await setMailCampaignChannels(campaignId, selectedMailboxIds);
      setSelectedMailboxIds(updated.map((m) => m.mailbox_id));
      await refreshReview();
    } catch (err) {
      setChannelsError(
        err instanceof ApiError ? `Couldn't save Channels (${err.status}): ${err.message}` : "Couldn't reach the backend."
      );
    } finally {
      setSavingChannels(false);
    }
  }

  async function handleMarkReady() {
    setBusy(true);
    setActionError(null);
    try {
      setCampaign(await markMailCampaignReady(campaignId));
      await refreshReview();
      setEnrollments(await listMailEnrollments(campaignId));
      // The Schedule tab is about to go from editable to locked -- any
      // error left over from an earlier Save Schedule attempt (while still
      // DRAFT) belongs to a context that no longer exists, and would
      // otherwise sit there indefinitely since nothing else ever clears it
      // once the user stops interacting with that tab. Only clearing this
      // on SUCCESS, not in the catch below: if Mark Ready itself fails,
      // the campaign is still DRAFT/editable and that error may still be
      // exactly what the user needs to see.
      setScheduleError(null);
    } catch (err) {
      setActionError(err instanceof ApiError ? `Couldn't mark ready (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    } finally {
      setBusy(false);
    }
  }

  async function handleUnlock() {
    setBusy(true);
    setActionError(null);
    try {
      await unlockMailCampaign(campaignId);
      await load();
      // Back to DRAFT/editable -- load() re-fetches the real current
      // schedule, but doesn't itself touch scheduleError (it's not part of
      // its own generic `error` state), so a stale message from before
      // this unlock would otherwise survive it. See handleMarkReady's own
      // comment for why this is a success-only clear.
      setScheduleError(null);
    } catch (err) {
      setActionError(err instanceof ApiError ? `Couldn't unlock (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    } finally {
      setBusy(false);
    }
  }

  async function handleArchive() {
    setBusy(true);
    setActionError(null);
    try {
      setCampaign(await archiveMailCampaign(campaignId));
      // Stays on this page (see this handler's own body -- no navigation),
      // and the Schedule tab remains locked, so the same stale-error
      // concern as handleMarkReady/handleUnlock applies here too.
      setScheduleError(null);
    } catch (err) {
      setActionError(err instanceof ApiError ? `Couldn't archive (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    } finally {
      setBusy(false);
    }
  }

  if (error) {
    return (
      <div className={MAIL_CAMPAIGN_DETAIL_CONTAINER_CLASS}>
        <Alert variant="destructive">
          <AlertTriangle />
          <AlertTitle>Couldn&apos;t load this campaign</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      </div>
    );
  }

  if (!campaign) {
    return <div className={cn(MAIL_CAMPAIGN_DETAIL_CONTAINER_CLASS, "text-sm text-muted-foreground")}>Loading…</div>;
  }

  return (
    <div className={MAIL_CAMPAIGN_DETAIL_CONTAINER_CLASS}>
      <Link href="/manager/campaigns" className="mb-4 inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground">
        <ArrowLeft className="h-4 w-4" />
        All Campaigns
      </Link>

      <MailCampaignHeader campaign={campaign} busy={busy} onMarkReady={handleMarkReady} onUnlock={handleUnlock} onArchive={handleArchive} />

      {actionError && (
        <Alert variant="destructive" className="mb-4">
          <AlertTriangle />
          <AlertDescription>{actionError}</AlertDescription>
        </Alert>
      )}

      {!editable && (
        <Alert className="mb-4">
          <Lock className="h-4 w-4" />
          <AlertTitle>{campaignLockedBannerTitle(campaign.status)}</AlertTitle>
          <AlertDescription>{campaignLockedBannerDescription(campaign.status)}</AlertDescription>
        </Alert>
      )}

      <Tabs defaultValue="dashboard">
        <TabsList>
          <TabsTab value="dashboard">Dashboard</TabsTab>
          <TabsTab value="leads">Leads</TabsTab>
          <TabsTab value="steps">Steps</TabsTab>
          <TabsTab value="channels">Channels</TabsTab>
          <TabsTab value="schedule">Schedule</TabsTab>
          <TabsTab value="settings">Settings</TabsTab>
        </TabsList>

        <TabsPanel value="dashboard">
          <MailCampaignDashboardTab campaign={campaign} review={review} enrollments={enrollments} />
        </TabsPanel>

        <TabsPanel value="leads">
          <MailCampaignLeadsTab
            campaign={campaign}
            enrollments={enrollments}
            workload={workload}
            batches={batches}
            lists={lists}
            onProspectsAdded={refreshLeadsSection}
          />
        </TabsPanel>

        <TabsPanel value="steps">
          <MailCampaignStepsTab
            steps={steps}
            editable={editable}
            busy={busy}
            selection={effectiveSelection}
            onSelectEmail={handleSelectEmail}
            onSelectWait={handleSelectWait}
            onStartAddStep={handleStartAddStep}
            onMoveStep={handleMoveStep}
            onDeleteStep={handleDeleteStep}
            onSaveEmail={handleSaveEmail}
            onAddStep={handleAddStep}
            onSaveWait={handleSaveWaitDelay}
            onCancelNewStep={handleCancelNewStep}
            savingSelection={savingSelection}
            selectionError={selectionError}
          />
        </TabsPanel>

        <TabsPanel value="channels">
          <MailCampaignChannelsTab
            mailboxes={mailboxes}
            selectedMailboxIds={selectedMailboxIds}
            onToggle={handleToggleChannel}
            busy={busy}
            saving={savingChannels}
            error={channelsError}
            onSave={handleSaveChannels}
            readOnly={campaign.status === "archived"}
          />
        </TabsPanel>

        <TabsPanel value="schedule">
          <MailCampaignScheduleTab
            timezone={timezone}
            setTimezone={setTimezone}
            windows={windows}
            setWindows={setWindows}
            editable={editable}
            saving={savingSchedule}
            error={scheduleError}
            onSave={handleSaveSchedule}
          />
        </TabsPanel>

        <TabsPanel value="settings">
          <MailCampaignSettingsTab
            editable={editable}
            busy={busy}
            name={name}
            setName={setName}
            sourceListId={sourceListId}
            setSourceListId={setSourceListId}
            lists={lists}
            onSaveDetails={handleSaveDetails}
            sharing={sharing}
            setSharing={setSharing}
            startImmediately={startImmediately}
            setStartImmediately={setStartImmediately}
            dailyLeadStartLimit={dailyLeadStartLimit}
            setDailyLeadStartLimit={setDailyLeadStartLimit}
            savingSettings={savingSettings}
            settingsError={settingsError}
            onSaveSettings={handleSaveSettings}
          />
        </TabsPanel>
      </Tabs>
    </div>
  );
}
