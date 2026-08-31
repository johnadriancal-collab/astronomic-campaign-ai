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
import { MailCampaignScheduleTab } from "@/components/mail-campaign-schedule-tab";
import { MailCampaignSettingsTab } from "@/components/mail-campaign-settings-tab";
import { MAIL_CAMPAIGN_DETAIL_CONTAINER_CLASS } from "@/lib/mail-campaign-layout";
import { cn } from "@/lib/utils";
import {
  addMailSequenceStep,
  archiveMailCampaign,
  ApiError,
  deleteMailSequenceStep,
  getMailCampaign,
  getMailCampaignChannels,
  getMailCampaignReview,
  listCrmLists,
  listMailboxes,
  listMailEnrollments,
  listMailSequenceSteps,
  markMailCampaignReady,
  reorderMailSequenceSteps,
  setMailCampaignChannels,
  unlockMailCampaign,
  updateMailCampaign,
  type CrmContactListSummary,
  type Mailbox,
  type MailCampaign,
  type MailCampaignReview,
  type MailCampaignSharing,
  type MailEnrollment,
  type MailSequenceStep,
} from "@/lib/api";

export default function MailCampaignDetailPage() {
  const params = useParams<{ id: string }>();
  const campaignId = params.id;

  const [campaign, setCampaign] = useState<MailCampaign | null>(null);
  const [steps, setSteps] = useState<MailSequenceStep[]>([]);
  const [review, setReview] = useState<MailCampaignReview | null>(null);
  const [enrollments, setEnrollments] = useState<MailEnrollment[]>([]);
  const [lists, setLists] = useState<CrmContactListSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [name, setName] = useState("");
  const [sourceListId, setSourceListId] = useState("");
  const [sendingDays, setSendingDays] = useState<number[]>([]);
  const [startTime, setStartTime] = useState("");
  const [endTime, setEndTime] = useState("");
  const [timezone, setTimezone] = useState("");
  const [allHours, setAllHours] = useState(false);
  const [savingSchedule, setSavingSchedule] = useState(false);

  const [sharing, setSharing] = useState<MailCampaignSharing>("everyone");
  const [startImmediately, setStartImmediately] = useState(false);
  const [dailyLeadStartLimit, setDailyLeadStartLimit] = useState("");
  const [savingSettings, setSavingSettings] = useState(false);
  const [settingsError, setSettingsError] = useState<string | null>(null);

  const [stepSubject, setStepSubject] = useState("");
  const [stepBody, setStepBody] = useState("");
  const [stepDelay, setStepDelay] = useState(2);
  const [addingStep, setAddingStep] = useState(false);
  const [stepError, setStepError] = useState<string | null>(null);

  const [mailboxes, setMailboxes] = useState<Mailbox[] | null>(null);
  const [selectedMailboxIds, setSelectedMailboxIds] = useState<string[]>([]);
  const [savingChannels, setSavingChannels] = useState(false);
  const [channelsError, setChannelsError] = useState<string | null>(null);

  const editable = campaign?.status === "draft";

  async function load() {
    try {
      const [c, s, r, e, l, mb, ch] = await Promise.all([
        getMailCampaign(campaignId),
        listMailSequenceSteps(campaignId),
        getMailCampaignReview(campaignId),
        listMailEnrollments(campaignId),
        listCrmLists(),
        listMailboxes(),
        getMailCampaignChannels(campaignId),
      ]);
      setCampaign(c);
      setSteps(s);
      setReview(r);
      setEnrollments(e);
      setLists(l);
      setMailboxes(mb);
      setSelectedMailboxIds(ch);
      setName(c.name);
      setSourceListId(c.source_list_id ?? "");
      setSendingDays(c.sending_days);
      setStartTime(c.start_time?.slice(0, 5) ?? "");
      setEndTime(c.end_time?.slice(0, 5) ?? "");
      setTimezone(c.timezone ?? "");
      setAllHours(c.all_hours);
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
    setSavingSchedule(true);
    setActionError(null);
    try {
      const updated = await updateMailCampaign(campaignId, {
        sending_days: sendingDays,
        start_time: allHours ? null : startTime || null,
        end_time: allHours ? null : endTime || null,
        timezone: timezone || null,
        all_hours: allHours,
      });
      setCampaign(updated);
      // The backend forces literal 00:00/23:59 bounds when all_hours is true --
      // reflect that back into the (disabled) time inputs so a reload doesn't
      // show stale values.
      setStartTime(updated.start_time?.slice(0, 5) ?? "");
      setEndTime(updated.end_time?.slice(0, 5) ?? "");
      await refreshReview();
    } catch (err) {
      setActionError(err instanceof ApiError ? `Couldn't save schedule (${err.status}): ${err.message}` : "Couldn't reach the backend.");
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

  async function handleAddStep(e: React.FormEvent) {
    e.preventDefault();
    if (!stepSubject.trim() || !stepBody.trim()) return;
    setAddingStep(true);
    setStepError(null);
    try {
      await addMailSequenceStep(campaignId, { subject: stepSubject, body: stepBody, delay_days: stepDelay });
      setStepSubject("");
      setStepBody("");
      setStepDelay(2);
      setSteps(await listMailSequenceSteps(campaignId));
      await refreshReview();
    } catch (err) {
      setStepError(err instanceof ApiError ? `Couldn't add step (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    } finally {
      setAddingStep(false);
    }
  }

  async function handleDeleteStep(stepId: string) {
    setBusy(true);
    setActionError(null);
    try {
      setSteps(await deleteMailSequenceStep(campaignId, stepId));
      await refreshReview();
    } catch (err) {
      setActionError(err instanceof ApiError ? `Couldn't delete step (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    } finally {
      setBusy(false);
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
          <AlertTitle>{campaign.status === "ready" ? "Ready -- locked for editing" : "Archived"}</AlertTitle>
          <AlertDescription>
            {campaign.status === "ready"
              ? "The audience was snapshotted when this campaign was marked ready. Unlock it to make changes -- this clears the snapshot so it can be re-created fresh."
              : "This campaign is archived."}
          </AlertDescription>
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
          <MailCampaignLeadsTab campaign={campaign} enrollments={enrollments} />
        </TabsPanel>

        <TabsPanel value="steps">
          <MailCampaignStepsTab
            steps={steps}
            editable={editable}
            busy={busy}
            onMoveStep={handleMoveStep}
            onDeleteStep={handleDeleteStep}
            onAddStep={handleAddStep}
            stepSubject={stepSubject}
            setStepSubject={setStepSubject}
            stepBody={stepBody}
            setStepBody={setStepBody}
            stepDelay={stepDelay}
            setStepDelay={setStepDelay}
            addingStep={addingStep}
            stepError={stepError}
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
            editable={editable}
            sendingDays={sendingDays}
            setSendingDays={setSendingDays}
            allHours={allHours}
            setAllHours={setAllHours}
            startTime={startTime}
            setStartTime={setStartTime}
            endTime={endTime}
            setEndTime={setEndTime}
            timezone={timezone}
            setTimezone={setTimezone}
            savingSchedule={savingSchedule}
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
