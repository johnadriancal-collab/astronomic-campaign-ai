"""
EmailMessage/EmailMessageEvent sync -- the final stage of
Campaign -> EmailSequence -> EmailMessage -> Lead. Built entirely from the
LIVE-verified findings in docs/APOLLO_MESSAGE_API_FINDINGS.md (#1, #2, #6,
#8); nothing here guesses at an undocumented field or status.

Three distinct, separately-triggered operations, all manual (no
scheduler), all idempotent:

1. sync_messages(campaign_id) -- pages `/emailer_messages/search` for one
   campaign's sequence. Confirmed live: this endpoint has no total-count
   envelope and no supported sort param, so the loop pages forward with a
   fixed per_page until a short/empty page comes back -- that's the only
   reliable end-of-results signal (see #6). Upserts are keyed by
   `apollo_message_id`, so re-running from page 1 is always safe. A
   message whose `contact_id` matches no known Lead is skipped rather
   than persisted -- this is a deliberate design choice, not error
   swallowing: it covers the disconnected, structurally-anomalous
   `downloaded_email` record observed in research (#7), which belongs to
   no Lead of ours and shouldn't be stored as if it did. No try/except
   around the Apollo call itself -- a failure partway through the
   pagination sweep propagates untouched, and `messages_last_synced_at`
   is only advanced after the ENTIRE sweep succeeds.

2. sync_message_events(email_message_id) -- fetches ONE message's
   `/activities` and upserts its open/click events, keyed by Apollo's own
   event id. Deliberately separate from sync_messages and only ever
   triggered per-message, never bundled automatically across a whole
   sequence -- Apollo has no bulk-events endpoint, so syncing events for
   every message in one sync_messages() call would silently turn into an
   uncapped N+1 call pattern. Events are immutable once observed (an
   "open" that already happened doesn't change), so an existing event is
   left alone rather than re-fetched/updated.

3. generate_test_fixtures(campaign_id) -- makes ZERO Apollo calls. Purely
   local, fabricated EmailMessage/EmailMessageEvent rows tagged
   `source=test_fixture`, so the Messages/Opens/Clicks UI has something to
   render while this Apollo account's sending mailboxes are all revoked or
   still in warmup (see docs/APOLLO_MESSAGE_API_FINDINGS.md #9). Idempotent:
   a second call is a no-op if fixtures already exist for that sequence.
"""

import asyncio
import uuid
from collections import defaultdict
from datetime import datetime, timezone

from app.apollo import ApolloClient
from app.models.email_message import (
    EmailMessage,
    EmailMessageEvent,
    EmailMessageSource,
    EmailMessageWithEventCounts,
)
from app.repositories.campaign_lead_store import CampaignLeadStore
from app.repositories.email_message_event_store import EmailMessageEventStore
from app.repositories.email_message_store import EmailMessageNotFoundError, EmailMessageStore
from app.models.email_sequence import EmailSequence
from app.repositories.email_sequence_step_store import EmailSequenceStepStore
from app.repositories.email_sequence_store import EmailSequenceStore
from app.repositories.lead_store import LeadStore

# Safety backstop against a runaway pagination loop -- not a silent cap.
# At 100/page this is 50,000 messages; hitting it raises loudly rather
# than truncating results without saying so.
MAX_SYNC_PAGES = 500

MESSAGE_STATUS_UNKNOWN = "unknown"  # our own placeholder when Apollo omits `status` entirely -- never a guessed real value


class EmailMessageSyncService:
    def __init__(
        self,
        sequence_store: EmailSequenceStore,
        step_store: EmailSequenceStepStore,
        message_store: EmailMessageStore,
        event_store: EmailMessageEventStore,
        lead_store: LeadStore,
        campaign_lead_store: CampaignLeadStore,
        apollo: ApolloClient | None = None,
    ):
        self.sequence_store = sequence_store
        self.step_store = step_store
        self.message_store = message_store
        self.event_store = event_store
        self.lead_store = lead_store
        self.campaign_lead_store = campaign_lead_store
        self.apollo = apollo or ApolloClient()
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def list_for_campaign(self, campaign_id: str) -> list[EmailMessageWithEventCounts]:
        """Read-only: every stored message (real + fixture) for this campaign's sequence."""
        sequence = await self.sequence_store.get_by_campaign_id(campaign_id)
        if sequence is None:
            return []
        return await self._list_with_counts(sequence.email_sequence_id)

    async def list_events(self, email_message_id: str) -> list[EmailMessageEvent]:
        return await self.event_store.list_for_message(email_message_id)

    async def sync_messages(self, campaign_id: str) -> tuple[EmailSequence, list[EmailMessageWithEventCounts]]:
        async with self._locks[f"messages:{campaign_id}"]:
            sequence = await self._require_sequence(campaign_id)
            steps = await self.step_store.list_for_sequence(sequence.email_sequence_id)
            step_by_apollo_id = {s.apollo_step_id: s for s in steps if s.apollo_step_id}

            all_raw: list[dict] = []
            page = 1
            while True:
                if page > MAX_SYNC_PAGES:
                    raise RuntimeError(
                        f"Exceeded {MAX_SYNC_PAGES} pages syncing messages for sequence "
                        f"{sequence.apollo_sequence_id} -- aborting instead of looping unbounded"
                    )
                # Apollo call -- deliberately no try/except. If this raises,
                # nothing below runs, so messages_last_synced_at is left
                # exactly as it was before this sync attempt.
                resp = await self.apollo.search_messages(sequence.apollo_sequence_id, page=page, per_page=100)
                page_messages = resp.get("emailer_messages", [])
                all_raw.extend(page_messages)
                if len(page_messages) < 100:
                    break
                page += 1

            # Only reached once every page of the sweep has succeeded.
            for raw in all_raw:
                await self._upsert_message(raw, sequence.email_sequence_id, step_by_apollo_id)

            sequence.messages_last_synced_at = datetime.now(timezone.utc)
            await self.sequence_store.save(sequence)

            return sequence, await self._list_with_counts(sequence.email_sequence_id)

    async def sync_message_events(self, email_message_id: str) -> list[EmailMessageEvent]:
        async with self._locks[f"events:{email_message_id}"]:
            message = await self.message_store.get(email_message_id)
            if message is None:
                raise EmailMessageNotFoundError(email_message_id)
            if message.apollo_message_id is None:
                raise ValueError(
                    f"EmailMessage {email_message_id} is a test fixture -- there is no Apollo message to sync events from"
                )

            # Apollo call -- no try/except, same discipline as sync_messages.
            resp = await self.apollo.get_message_activities(message.apollo_message_id)
            for group in resp.get("activities", []):
                event_type = group.get("event_group_type", MESSAGE_STATUS_UNKNOWN)
                for raw_event in group.get("emailer_message_events", []):
                    await self._upsert_event(raw_event, email_message_id, event_type)

            return await self.event_store.list_for_message(email_message_id)

    async def generate_test_fixtures(self, campaign_id: str) -> list[EmailMessageWithEventCounts]:
        async with self._locks[f"messages:{campaign_id}"]:
            sequence = await self._require_sequence(campaign_id)

            existing = await self.message_store.list_for_sequence(sequence.email_sequence_id)
            if any(m.source == EmailMessageSource.TEST_FIXTURE for m in existing):
                return await self._list_with_counts(sequence.email_sequence_id)  # idempotent no-op

            steps = await self.step_store.list_for_sequence(sequence.email_sequence_id)
            if not steps:
                raise ValueError(
                    f"EmailSequence {sequence.email_sequence_id} has no deployed steps -- sync the sequence first"
                )
            step = steps[0]

            memberships = await self.campaign_lead_store.list_for_campaign(campaign_id)
            if not memberships:
                raise ValueError(f"Campaign {campaign_id} has no Leads yet -- build it before generating test fixtures")

            now = datetime.now(timezone.utc)
            statuses = ["completed", "completed", "completed", "failed"]
            for i, membership in enumerate(memberships):
                status = statuses[i % len(statuses)]
                bounce = status == "failed" and i % 8 == 0
                spam_blocked = status == "failed" and not bounce
                replied = status == "completed" and i % 5 == 0

                message = EmailMessage(
                    email_message_id=str(uuid.uuid4()),
                    apollo_message_id=None,
                    email_sequence_id=sequence.email_sequence_id,
                    email_sequence_step_id=step.email_sequence_step_id,
                    apollo_touch_id=None,
                    lead_id=membership.lead_id,
                    status=status,
                    failure_reason="Spam Blocked" if spam_blocked else None,
                    bounce=bounce,
                    spam_blocked=spam_blocked,
                    replied=replied,
                    reply_class="interested" if replied else None,
                    provider_message_id=None,
                    provider_thread_id=None,
                    created_at=now,
                    due_at=now,
                    completed_at=now if status == "completed" else None,
                    failed_at=now if status == "failed" else None,
                    source=EmailMessageSource.TEST_FIXTURE,
                    last_synced_at=None,
                )
                await self.message_store.create(message)

                if status == "completed" and i % 3 == 0:
                    await self.event_store.create(
                        EmailMessageEvent(
                            email_message_event_id=str(uuid.uuid4()),
                            email_message_id=message.email_message_id,
                            apollo_event_id=None,
                            event_type="open",
                            occurred_at=now,
                            apollo_contact_id=None,
                            readable_user_agent="Test Fixture",
                            region=None,
                            country=None,
                            source=EmailMessageSource.TEST_FIXTURE,
                        )
                    )
                    if i % 6 == 0:
                        await self.event_store.create(
                            EmailMessageEvent(
                                email_message_event_id=str(uuid.uuid4()),
                                email_message_id=message.email_message_id,
                                apollo_event_id=None,
                                event_type="click",
                                occurred_at=now,
                                apollo_contact_id=None,
                                readable_user_agent="Test Fixture",
                                region=None,
                                country=None,
                                source=EmailMessageSource.TEST_FIXTURE,
                            )
                        )

            return await self._list_with_counts(sequence.email_sequence_id)

    async def _require_sequence(self, campaign_id: str):
        sequence = await self.sequence_store.get_by_campaign_id(campaign_id)
        if sequence is None:
            raise ValueError(f"Campaign {campaign_id} has no synced EmailSequence yet -- sync the sequence first")
        return sequence

    async def _upsert_message(self, raw: dict, email_sequence_id: str, step_by_apollo_id: dict) -> None:
        apollo_message_id = raw.get("id")
        if not apollo_message_id:
            return  # can't dedupe or reference this later without an id; skip rather than store something unreferenceable

        lead = await self.lead_store.get_by_apollo_contact_id(raw.get("contact_id") or "")
        if lead is None:
            # Covers the disconnected/anomalous record shape observed in
            # research (#7) -- a message whose contact isn't one of our
            # own Leads isn't ours to store.
            return

        step = step_by_apollo_id.get(raw.get("emailer_step_id"))
        existing = await self.message_store.get_by_apollo_message_id(apollo_message_id)

        message = EmailMessage(
            email_message_id=existing.email_message_id if existing else str(uuid.uuid4()),
            apollo_message_id=apollo_message_id,
            email_sequence_id=email_sequence_id,
            email_sequence_step_id=step.email_sequence_step_id if step else None,
            apollo_touch_id=raw.get("emailer_touch_id"),
            lead_id=lead.lead_id,
            status=raw.get("status") or MESSAGE_STATUS_UNKNOWN,
            failure_reason=raw.get("failure_reason"),
            bounce=raw.get("bounce", False),
            spam_blocked=raw.get("spam_blocked", False),
            replied=raw.get("replied", False),
            reply_class=raw.get("reply_class"),
            provider_message_id=raw.get("provider_message_id"),
            provider_thread_id=raw.get("provider_thread_id"),
            created_at=raw.get("created_at"),
            due_at=raw.get("due_at"),
            completed_at=raw.get("completed_at"),
            failed_at=raw.get("failed_at"),
            source=EmailMessageSource.APOLLO_SYNC,
            last_synced_at=datetime.now(timezone.utc),
        )
        if existing:
            await self.message_store.save(message)
        else:
            await self.message_store.create(message)

    async def _upsert_event(self, raw: dict, email_message_id: str, event_type: str) -> None:
        apollo_event_id = raw.get("id")
        if apollo_event_id:
            existing = await self.event_store.get_by_apollo_event_id(apollo_event_id)
            if existing is not None:
                return  # events are immutable once observed -- nothing to refresh

        event = EmailMessageEvent(
            email_message_event_id=str(uuid.uuid4()),
            email_message_id=email_message_id,
            apollo_event_id=apollo_event_id,
            event_type=raw.get("type") or event_type,
            occurred_at=raw["created_at"],
            apollo_contact_id=raw.get("contact_id"),
            readable_user_agent=raw.get("readable_user_agent"),
            region=raw.get("state"),
            country=raw.get("country"),
            source=EmailMessageSource.APOLLO_SYNC,
        )
        await self.event_store.create(event)

    async def _list_with_counts(self, email_sequence_id: str) -> list[EmailMessageWithEventCounts]:
        messages = await self.message_store.list_for_sequence(email_sequence_id)
        result = []
        for message in messages:
            events = await self.event_store.list_for_message(message.email_message_id)
            open_count = sum(1 for e in events if e.event_type == "open")
            click_count = sum(1 for e in events if e.event_type == "click")
            result.append(
                EmailMessageWithEventCounts(**message.model_dump(), open_count=open_count, click_count=click_count)
            )
        return result
