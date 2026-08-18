"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { AlertTriangle, ArrowDown, ArrowLeft, ArrowUp, Archive, Lock, Plus, Trash2, Unlock } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { SendDaysPicker } from "@/components/send-days-picker";
import { SharingSelector } from "@/components/mail-sharing-selector";
import {
  addMailSequenceStep,
  archiveMailCampaign,
  ApiError,
  deleteMailSequenceStep,
  getMailCampaign,
  getMailCampaignReview,
  listCrmLists,
  listMailSequenceSteps,
  markMailCampaignReady,
  MAIL_TEMPLATE_VARIABLES,
  reorderMailSequenceSteps,
  unlockMailCampaign,
  updateMailCampaign,
  type CrmContactListSummary,
  type MailCampaign,
  type MailCampaignReview,
  type MailCampaignSharing,
  type MailSequenceStep,
} from "@/lib/api";
import { mailCampaignStatusBadgeClass, mailCampaignStatusLabel } from "@/lib/mail";
import { timezoneOptionsIncluding } from "@/lib/timezones";
import { cn } from "@/lib/utils";

export default function MailCampaignDetailPage() {
  const params = useParams<{ id: string }>();
  const campaignId = params.id;

  const [campaign, setCampaign] = useState<MailCampaign | null>(null);
  const [steps, setSteps] = useState<MailSequenceStep[]>([]);
  const [review, setReview] = useState<MailCampaignReview | null>(null);
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

  const editable = campaign?.status === "draft";

  async function load() {
    try {
      const [c, s, r, l] = await Promise.all([
        getMailCampaign(campaignId),
        listMailSequenceSteps(campaignId),
        getMailCampaignReview(campaignId),
        listCrmLists(),
      ]);
      setCampaign(c);
      setSteps(s);
      setReview(r);
      setLists(l);
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

  async function handleMarkReady() {
    setBusy(true);
    setActionError(null);
    try {
      setCampaign(await markMailCampaignReady(campaignId));
      await refreshReview();
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
      const updated = await unlockMailCampaign(campaignId);
      setCampaign(updated);
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
      <div className="mx-auto max-w-3xl px-6 py-10">
        <Alert variant="destructive">
          <AlertTriangle />
          <AlertTitle>Couldn&apos;t load this campaign</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      </div>
    );
  }

  if (!campaign) {
    return <div className="mx-auto max-w-3xl px-6 py-10 text-sm text-muted-foreground">Loading…</div>;
  }

  return (
    <div className="mx-auto max-w-3xl px-6 py-10">
      <Link href="/manager/campaigns" className="mb-4 inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground">
        <ArrowLeft className="h-4 w-4" />
        All Campaigns
      </Link>

      <div className="mb-6 flex items-start justify-between gap-4">
        <div>
          <h1 className="font-serif text-2xl font-medium tracking-tight">{campaign.name}</h1>
          <p className="mt-1 text-xs text-muted-foreground">Astronomic Mail -- Phase 1 (no sending capability yet)</p>
        </div>
        <span className={cn("shrink-0 rounded-full px-2 py-0.5 text-xs font-medium", mailCampaignStatusBadgeClass(campaign.status))}>
          {mailCampaignStatusLabel(campaign.status)}
        </span>
      </div>

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

      <div className="space-y-6">
        {/* Audience */}
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">Audience</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="space-y-1">
              <label className="text-xs font-medium text-muted-foreground">Campaign name</label>
              <Input value={name} onChange={(e) => setName(e.target.value)} disabled={!editable} />
            </div>
            <div className="space-y-1">
              <label className="text-xs font-medium text-muted-foreground">CRM List</label>
              <select
                value={sourceListId}
                onChange={(e) => setSourceListId(e.target.value)}
                disabled={!editable}
                className="h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm disabled:cursor-not-allowed disabled:opacity-60"
              >
                <option value="">-- choose a CRM List --</option>
                {lists.map((l) => (
                  <option key={l.list_id} value={l.list_id}>
                    {l.name} ({l.contact_count} contact{l.contact_count === 1 ? "" : "s"})
                  </option>
                ))}
              </select>
              <p className="text-xs text-muted-foreground">
                Referenced by list -- contacts are never copied. Changes to the list after this campaign is marked
                Ready will not retroactively change its audience.
              </p>
            </div>
            {editable && (
              <Button size="sm" onClick={handleSaveDetails} disabled={busy || !name.trim()}>
                Save
              </Button>
            )}
          </CardContent>
        </Card>

        {/* Sequence */}
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">Sequence</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {steps.length === 0 && <p className="text-sm text-muted-foreground">No steps yet.</p>}
            {steps.map((step, i) => (
              <div key={step.step_id} className="rounded-md border border-border p-3 text-sm">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <p className="font-medium">
                      Step {step.step_number}: {step.subject}
                    </p>
                    <p className="mt-1 whitespace-pre-wrap text-muted-foreground">{step.body}</p>
                    <p className="mt-1 text-xs text-muted-foreground">
                      {step.delay_days === 0 ? "Sent immediately" : `${step.delay_days} day${step.delay_days === 1 ? "" : "s"} after previous step`}
                    </p>
                  </div>
                  {editable && (
                    <div className="flex shrink-0 gap-1">
                      <Button variant="outline" size="sm" onClick={() => handleMoveStep(i, -1)} disabled={busy || i === 0}>
                        <ArrowUp className="h-3.5 w-3.5" />
                      </Button>
                      <Button variant="outline" size="sm" onClick={() => handleMoveStep(i, 1)} disabled={busy || i === steps.length - 1}>
                        <ArrowDown className="h-3.5 w-3.5" />
                      </Button>
                      <Button variant="outline" size="sm" onClick={() => handleDeleteStep(step.step_id)} disabled={busy}>
                        <Trash2 className="h-3.5 w-3.5" />
                      </Button>
                    </div>
                  )}
                </div>
              </div>
            ))}

            {editable && (
              <form onSubmit={handleAddStep} className="space-y-2 rounded-md border border-dashed border-border p-3">
                {stepError && <Alert variant="destructive"><AlertDescription>{stepError}</AlertDescription></Alert>}
                <p className="text-xs font-medium text-muted-foreground">
                  Add a step -- allowed variables: {MAIL_TEMPLATE_VARIABLES.map((v) => `{{${v}}}`).join(", ")}
                </p>
                <Input value={stepSubject} onChange={(e) => setStepSubject(e.target.value)} placeholder="Subject, e.g. Quick intro, {{first_name}}" />
                <Textarea value={stepBody} onChange={(e) => setStepBody(e.target.value)} placeholder="Body" rows={3} />
                <div className="flex items-center gap-2">
                  <label className="text-xs text-muted-foreground">Wait</label>
                  <Input
                    type="number"
                    min={0}
                    value={stepDelay}
                    onChange={(e) => setStepDelay(Number(e.target.value))}
                    className="w-20"
                  />
                  <label className="text-xs text-muted-foreground">day(s) after previous step</label>
                </div>
                <Button type="submit" size="sm" disabled={addingStep || !stepSubject.trim() || !stepBody.trim()} className="gap-1.5">
                  <Plus className="h-3.5 w-3.5" />
                  {addingStep ? "Adding..." : "Add step"}
                </Button>
              </form>
            )}
          </CardContent>
        </Card>

        {/* Schedule */}
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">Schedule</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <div>
              <p className="mb-1.5 text-xs font-medium text-muted-foreground">Sending days</p>
              <SendDaysPicker days={sendingDays} onChange={setSendingDays} disabled={!editable} />
            </div>
            <div className="flex items-center justify-between rounded-md border border-border/60 px-3 py-2">
              <span className="text-sm">All hours</span>
              <Switch checked={allHours} onCheckedChange={(v) => setAllHours(Boolean(v))} disabled={!editable} />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1">
                <label className="text-xs font-medium text-muted-foreground">Start time</label>
                <input
                  type="time"
                  value={startTime}
                  onChange={(e) => setStartTime(e.target.value)}
                  disabled={!editable || allHours}
                  className="h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm disabled:cursor-not-allowed disabled:opacity-60"
                />
              </div>
              <div className="space-y-1">
                <label className="text-xs font-medium text-muted-foreground">End time</label>
                <input
                  type="time"
                  value={endTime}
                  onChange={(e) => setEndTime(e.target.value)}
                  disabled={!editable || allHours}
                  className="h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm disabled:cursor-not-allowed disabled:opacity-60"
                />
              </div>
            </div>
            <div className="space-y-1">
              <label className="text-xs font-medium text-muted-foreground">Timezone</label>
              <select
                value={timezone}
                onChange={(e) => setTimezone(e.target.value)}
                disabled={!editable}
                className="h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm disabled:cursor-not-allowed disabled:opacity-60"
              >
                <option value="">-- choose a timezone --</option>
                {timezoneOptionsIncluding(timezone || null).map((tz) => (
                  <option key={tz.value} value={tz.value}>
                    {tz.label}
                  </option>
                ))}
              </select>
            </div>
            {editable && (
              <Button size="sm" onClick={handleSaveSchedule} disabled={savingSchedule}>
                {savingSchedule ? "Saving..." : "Save schedule"}
              </Button>
            )}
          </CardContent>
        </Card>

        {/* Campaign Settings -- Campaign Manager Integration Phase additions
            with no existing home: Sharing, Start Immediately preference, and
            Daily Lead Start Limit. Campaign name lives in Audience above;
            send days/timezone/window live in Schedule above -- not duplicated
            here. */}
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">Campaign Settings</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            {settingsError && (
              <Alert variant="destructive">
                <AlertDescription>{settingsError}</AlertDescription>
              </Alert>
            )}
            <div className="space-y-1.5">
              <label className="text-xs font-medium text-muted-foreground">Sharing</label>
              <SharingSelector value={sharing} onChange={setSharing} disabled={!editable} />
              <p className="text-xs text-muted-foreground/70">Saved for later -- not yet enforced.</p>
            </div>
            <div className="flex items-start justify-between gap-4 rounded-md border border-border/60 p-3">
              <div>
                <p className="text-sm font-medium">Start campaign immediately</p>
                <p className="text-xs text-muted-foreground">
                  When enabled, newly added leads can begin progressing through the campaign once sending is enabled.
                </p>
              </div>
              <Switch
                checked={startImmediately}
                onCheckedChange={(v) => setStartImmediately(Boolean(v))}
                disabled={!editable}
                className="mt-0.5 shrink-0"
              />
            </div>
            <div className="space-y-1.5">
              <label className="text-xs font-medium text-muted-foreground">Number of leads to start daily</label>
              <Input
                type="number"
                min={1}
                step={1}
                value={dailyLeadStartLimit}
                onChange={(e) => setDailyLeadStartLimit(e.target.value)}
                disabled={!editable}
                placeholder="50 (leave blank for unlimited)"
              />
              <p className="text-xs text-muted-foreground/70">
                How many new leads may begin this sequence per day -- separate from a mailbox&apos;s own daily sending limit.
              </p>
            </div>
            {editable && (
              <Button size="sm" onClick={handleSaveSettings} disabled={savingSettings}>
                {savingSettings ? "Saving..." : "Save settings"}
              </Button>
            )}
          </CardContent>
        </Card>

        {/* Review */}
        {review && (
          <Card>
            <CardHeader>
              <CardTitle className="text-sm">Review</CardTitle>
            </CardHeader>
            <CardContent>
              <dl className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-3">
                <div>
                  <dt className="text-xs text-muted-foreground">Source list</dt>
                  <dd>{review.source_list_exists ? review.source_list_name : "Not set / missing"}</dd>
                </div>
                <div>
                  <dt className="text-xs text-muted-foreground">Total contacts</dt>
                  <dd>{review.total_contacts}</dd>
                </div>
                <div>
                  <dt className="text-xs text-muted-foreground">Missing email</dt>
                  <dd>{review.contacts_missing_email}</dd>
                </div>
                <div>
                  <dt className="text-xs text-muted-foreground">Suppressed</dt>
                  <dd>{review.contacts_suppressed}</dd>
                </div>
                <div>
                  <dt className="text-xs text-muted-foreground">Eligible</dt>
                  <dd className="font-medium">{review.contacts_eligible}</dd>
                </div>
                <div>
                  <dt className="text-xs text-muted-foreground">Sequence steps</dt>
                  <dd>{review.sequence_step_count}</dd>
                </div>
                <div>
                  <dt className="text-xs text-muted-foreground">Theoretical total sends</dt>
                  <dd className="font-medium">{review.theoretical_total_sends}</dd>
                </div>
                <div className="col-span-2 sm:col-span-3">
                  <dt className="text-xs text-muted-foreground">Daily capacity estimate</dt>
                  <dd>{review.daily_capacity_estimate ?? review.daily_capacity_note}</dd>
                </div>
              </dl>
            </CardContent>
          </Card>
        )}

        {/* Actions / launch safety */}
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">Status</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <Alert>
              <AlertTitle>Sending will be enabled after mailbox setup</AlertTitle>
              <AlertDescription>
                There is no Gmail connection and no send queue in this phase. Marking a campaign Ready only snapshots
                its audience for review -- it never sends or schedules anything.
              </AlertDescription>
            </Alert>
            <div className="flex gap-2">
              {campaign.status === "draft" && (
                <Button onClick={handleMarkReady} disabled={busy}>
                  Mark Ready
                </Button>
              )}
              {campaign.status === "ready" && (
                <Button variant="outline" onClick={handleUnlock} disabled={busy} className="gap-1.5">
                  <Unlock className="h-3.5 w-3.5" />
                  Unlock to edit
                </Button>
              )}
              {campaign.status !== "archived" && (
                <Button variant="outline" onClick={handleArchive} disabled={busy} className="gap-1.5">
                  <Archive className="h-3.5 w-3.5" />
                  Archive
                </Button>
              )}
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
