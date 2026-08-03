"use client";

import { useEffect, useState } from "react";
import {
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  FlaskConical,
  Loader2,
  MousePointerClick,
  RefreshCw,
  Reply,
  ShieldAlert,
} from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { DataSourceBadge } from "@/components/data-source-badge";
import {
  ApiError,
  generateCampaignMessageFixtures,
  listCampaignMessages,
  listMessageEvents,
  syncCampaignMessages,
  syncMessageEvents,
  type CampaignLeadView,
  type EmailMessageEvent,
  type EmailMessageWithEventCounts,
} from "@/lib/api";

function formatDateTime(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function leadLabel(leadId: string, leadsById: Map<string, CampaignLeadView>): string {
  const lead = leadsById.get(leadId);
  if (!lead) return leadId;
  return [lead.first_name, lead.last_name].filter(Boolean).join(" ") || lead.email || leadId;
}

export function CampaignMessagesSection({
  campaignId,
  leads,
}: {
  campaignId: string;
  leads: CampaignLeadView[];
}) {
  const [messages, setMessages] = useState<EmailMessageWithEventCounts[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [syncLoading, setSyncLoading] = useState(false);
  const [syncError, setSyncError] = useState<string | null>(null);
  const [fixturesLoading, setFixturesLoading] = useState(false);
  const [fixturesError, setFixturesError] = useState<string | null>(null);

  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [eventsByMessage, setEventsByMessage] = useState<Record<string, EmailMessageEvent[]>>({});
  const [eventsLoadingId, setEventsLoadingId] = useState<string | null>(null);
  const [eventsErrorByMessage, setEventsErrorByMessage] = useState<Record<string, string>>({});

  const leadsById = new Map(leads.map((l) => [l.lead_id, l]));

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setError(null);
      try {
        const data = await listCampaignMessages(campaignId);
        if (!cancelled) setMessages(data);
      } catch (err) {
        if (!cancelled) {
          setError(
            err instanceof ApiError
              ? `Couldn't load messages (${err.status}): ${err.message}`
              : "Couldn't reach the backend to load messages."
          );
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [campaignId]);

  async function runSync() {
    setSyncLoading(true);
    setSyncError(null);
    try {
      const data = await syncCampaignMessages(campaignId);
      setMessages(data);
    } catch (err) {
      setSyncError(
        err instanceof ApiError
          ? `Sync failed (${err.status}): ${err.message}`
          : "Couldn't reach the backend to sync messages."
      );
    } finally {
      setSyncLoading(false);
    }
  }

  async function runGenerateFixtures() {
    setFixturesLoading(true);
    setFixturesError(null);
    try {
      const data = await generateCampaignMessageFixtures(campaignId);
      setMessages(data);
    } catch (err) {
      setFixturesError(
        err instanceof ApiError
          ? `Couldn't generate fixtures (${err.status}): ${err.message}`
          : "Couldn't reach the backend to generate fixtures."
      );
    } finally {
      setFixturesLoading(false);
    }
  }

  async function loadEvents(messageId: string) {
    setEventsLoadingId(messageId);
    setEventsErrorByMessage((prev) => ({ ...prev, [messageId]: "" }));
    try {
      const events = await listMessageEvents(campaignId, messageId);
      setEventsByMessage((prev) => ({ ...prev, [messageId]: events }));
    } catch (err) {
      setEventsErrorByMessage((prev) => ({
        ...prev,
        [messageId]:
          err instanceof ApiError
            ? `Couldn't load events (${err.status}): ${err.message}`
            : "Couldn't reach the backend to load events.",
      }));
    } finally {
      setEventsLoadingId(null);
    }
  }

  async function toggleExpand(message: EmailMessageWithEventCounts) {
    if (expandedId === message.email_message_id) {
      setExpandedId(null);
      return;
    }
    setExpandedId(message.email_message_id);
    if (!eventsByMessage[message.email_message_id]) {
      await loadEvents(message.email_message_id);
    }
  }

  async function runSyncEvents(message: EmailMessageWithEventCounts) {
    setEventsLoadingId(message.email_message_id);
    setEventsErrorByMessage((prev) => ({ ...prev, [message.email_message_id]: "" }));
    try {
      const events = await syncMessageEvents(campaignId, message.email_message_id);
      setEventsByMessage((prev) => ({ ...prev, [message.email_message_id]: events }));
      setMessages(
        (prev) =>
          prev?.map((m) =>
            m.email_message_id === message.email_message_id
              ? {
                  ...m,
                  open_count: events.filter((e) => e.event_type === "open").length,
                  click_count: events.filter((e) => e.event_type === "click").length,
                }
              : m
          ) ?? prev
      );
    } catch (err) {
      setEventsErrorByMessage((prev) => ({
        ...prev,
        [message.email_message_id]:
          err instanceof ApiError
            ? `Sync failed (${err.status}): ${err.message}`
            : "Couldn't reach the backend to sync events.",
      }));
    } finally {
      setEventsLoadingId(null);
    }
  }

  const hasAnyFixtures = messages?.some((m) => m.source === "test_fixture") ?? false;

  return (
    <section>
      <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-medium text-muted-foreground">
          Messages {messages !== null && messages.length > 0 && `· ${messages.length}`}
        </h2>
        <div className="flex items-center gap-2">
          <Button
            onClick={runGenerateFixtures}
            disabled={fixturesLoading || hasAnyFixtures}
            variant="outline"
            size="sm"
            className="gap-1.5"
          >
            {fixturesLoading ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <FlaskConical className="h-3.5 w-3.5" />
            )}
            {hasAnyFixtures ? "Fixtures generated" : "Generate test fixtures"}
          </Button>
          <Button onClick={runSync} disabled={syncLoading} variant="outline" size="sm" className="gap-1.5">
            {syncLoading ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <RefreshCw className="h-3.5 w-3.5" />
            )}
            Sync now
          </Button>
        </div>
      </div>

      {error && (
        <Alert variant="destructive" className="mb-3">
          <AlertTriangle />
          <AlertTitle>Couldn&apos;t load messages</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
      {syncError && (
        <Alert variant="destructive" className="mb-3">
          <AlertTriangle />
          <AlertTitle>Sync failed</AlertTitle>
          <AlertDescription>{syncError}</AlertDescription>
        </Alert>
      )}
      {fixturesError && (
        <Alert variant="destructive" className="mb-3">
          <AlertTriangle />
          <AlertTitle>Couldn&apos;t generate fixtures</AlertTitle>
          <AlertDescription>{fixturesError}</AlertDescription>
        </Alert>
      )}

      {!error && messages === null && <Skeleton className="h-32 w-full rounded-xl" />}

      {!error && messages !== null && messages.length === 0 && (
        <p className="text-sm text-muted-foreground">
          No messages yet — click &quot;Sync now&quot; once Apollo has actually sent something, or
          &quot;Generate test fixtures&quot; to preview this view with clearly-labeled demo data instead.
        </p>
      )}

      {!error && messages !== null && messages.length > 0 && (
        <div className="overflow-x-auto rounded-xl border border-border/60">
          <table className="w-full text-sm">
            <thead className="bg-secondary/40 text-xs text-muted-foreground">
              <tr>
                <th className="px-3 py-2 text-left font-medium">Lead</th>
                <th className="px-3 py-2 text-left font-medium">Status</th>
                <th className="px-3 py-2 text-left font-medium">Source</th>
                <th className="px-3 py-2 text-center font-medium">Opens</th>
                <th className="px-3 py-2 text-center font-medium">Clicks</th>
                <th className="px-3 py-2 text-left font-medium">Replied</th>
                <th className="px-3 py-2 text-left font-medium">Completed</th>
                <th className="px-3 py-2" />
              </tr>
            </thead>
            <tbody className="divide-y divide-border/60">
              {messages.map((message) => (
                <MessageRow
                  key={message.email_message_id}
                  message={message}
                  leadDisplayName={leadLabel(message.lead_id, leadsById)}
                  expanded={expandedId === message.email_message_id}
                  events={eventsByMessage[message.email_message_id]}
                  eventsLoading={eventsLoadingId === message.email_message_id}
                  eventsError={eventsErrorByMessage[message.email_message_id]}
                  onToggle={() => toggleExpand(message)}
                  onSyncEvents={() => runSyncEvents(message)}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function MessageRow({
  message,
  leadDisplayName,
  expanded,
  events,
  eventsLoading,
  eventsError,
  onToggle,
  onSyncEvents,
}: {
  message: EmailMessageWithEventCounts;
  leadDisplayName: string;
  expanded: boolean;
  events: EmailMessageEvent[] | undefined;
  eventsLoading: boolean;
  eventsError: string | undefined;
  onToggle: () => void;
  onSyncEvents: () => void;
}) {
  const isFixture = message.source === "test_fixture";

  return (
    <>
      <tr className="cursor-pointer hover:bg-secondary/30" onClick={onToggle}>
        <td className="px-3 py-2.5">
          <div className="flex items-center gap-1.5">
            {expanded ? (
              <ChevronDown className="h-3.5 w-3.5 text-muted-foreground" />
            ) : (
              <ChevronRight className="h-3.5 w-3.5 text-muted-foreground" />
            )}
            {leadDisplayName}
          </div>
        </td>
        <td className="px-3 py-2.5">
          <div className="flex flex-wrap items-center gap-1">
            <Badge variant="outline" className="rounded-full font-normal">
              {message.status}
            </Badge>
            {message.bounce && (
              <Badge variant="destructive" className="gap-1 rounded-full font-normal">
                <ShieldAlert className="h-3 w-3" />
                Bounced
              </Badge>
            )}
            {message.spam_blocked && (
              <Badge variant="destructive" className="gap-1 rounded-full font-normal">
                <ShieldAlert className="h-3 w-3" />
                Spam blocked
              </Badge>
            )}
          </div>
        </td>
        <td className="px-3 py-2.5">
          <DataSourceBadge source={message.source} />
        </td>
        <td className="px-3 py-2.5 text-center text-muted-foreground">{message.open_count}</td>
        <td className="px-3 py-2.5 text-center text-muted-foreground">{message.click_count}</td>
        <td className="px-3 py-2.5">
          {message.replied ? (
            <Badge variant="secondary" className="gap-1 rounded-full font-normal">
              <Reply className="h-3 w-3" />
              {message.reply_class ?? "Replied"}
            </Badge>
          ) : (
            <span className="text-muted-foreground">—</span>
          )}
        </td>
        <td className="px-3 py-2.5 text-muted-foreground">{formatDateTime(message.completed_at)}</td>
        <td className="px-3 py-2.5 text-right">
          {!isFixture && (
            <Button
              onClick={(e) => {
                e.stopPropagation();
                onSyncEvents();
              }}
              disabled={eventsLoading}
              variant="ghost"
              size="sm"
              className="gap-1.5 text-xs"
            >
              {eventsLoading ? <Loader2 className="h-3 w-3 animate-spin" /> : <MousePointerClick className="h-3 w-3" />}
              Sync events
            </Button>
          )}
        </td>
      </tr>
      {expanded && (
        <tr className="bg-secondary/20">
          <td colSpan={8} className="px-3 py-3">
            {eventsError && (
              <Alert variant="destructive" className="mb-2">
                <AlertTriangle />
                <AlertDescription>{eventsError}</AlertDescription>
              </Alert>
            )}
            {eventsLoading && !events && <Skeleton className="h-10 w-full rounded-lg" />}
            {!eventsLoading && events && events.length === 0 && (
              <p className="text-xs text-muted-foreground">
                No open/click events {isFixture ? "on this test fixture." : "synced yet — try \"Sync events\"."}
              </p>
            )}
            {events && events.length > 0 && (
              <ul className="space-y-1.5 text-xs text-muted-foreground">
                {events.map((event) => (
                  <li key={event.email_message_event_id} className="flex flex-wrap items-center gap-2">
                    <Badge variant="outline" className="rounded-full font-normal capitalize">
                      {event.event_type}
                    </Badge>
                    <span>{formatDateTime(event.occurred_at)}</span>
                    {event.readable_user_agent && <span>· {event.readable_user_agent}</span>}
                    {event.country && <span>· {event.region ? `${event.region}, ` : ""}{event.country}</span>}
                    <DataSourceBadge source={event.source} />
                  </li>
                ))}
              </ul>
            )}
            <p className="mt-2 border-t border-border/40 pt-2 text-[11px] text-muted-foreground/70">
              Message ID: <span className="font-mono">{message.email_message_id}</span>
              {message.apollo_message_id && (
                <>
                  {" "}
                  · Apollo message ID: <span className="font-mono">{message.apollo_message_id}</span>
                </>
              )}
              {message.provider_thread_id && (
                <>
                  {" "}
                  · Provider thread ID: <span className="font-mono">{message.provider_thread_id}</span>
                </>
              )}
            </p>
          </td>
        </tr>
      )}
    </>
  );
}
