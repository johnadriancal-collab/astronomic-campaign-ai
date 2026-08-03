"""
Explicit, manual sync between one Campaign's Apollo sequence and our own
EmailSequence/EmailSequenceStep records -- Phase 1 of the
Campaign -> EmailSequence -> EmailMessage -> Lead chain (EmailMessage does
not exist yet). No scheduler here -- sync() is only ever called from an
explicit API request.

sync() does two distinct things, in order:
  1. Ensure our own deployed-configuration snapshot exists (EmailSequence +
     EmailSequenceStep, created once from the Campaign's stored plan/
     apollo_sequence_id). This is NOT itself "syncing Apollo state" -- it's
     deriving from data we already have stored, so it can't fail the way
     an Apollo call can, and creating it does not touch last_synced_at.
  2. Call Apollo (`search_sequences`) and, ONLY if that call succeeds,
     update status/status_reason/aggregate stats/last_synced_at and any
     newly-confirmed apollo_step_id values. No try/except around the
     Apollo call -- an exception propagates to the caller untouched, so a
     failed sync can never leave last_synced_at advanced or any Apollo-
     sourced field silently updated. Mirrors CampaignService.activate()/
     pause()'s exact discipline.

Idempotent: calling sync() twice creates the snapshot only once (the
second call finds it already exists and just refreshes it), and Apollo
step ids are matched/updated by position, never duplicated.
"""

import asyncio
import uuid
from collections import defaultdict
from datetime import datetime, timezone

from app.apollo import ApolloClient
from app.models.campaign import Campaign
from app.models.email_sequence import EmailSequence, EmailSequenceStatus, EmailSequenceStep
from app.repositories.campaign_store import CampaignNotFoundError, CampaignStore
from app.repositories.email_sequence_step_store import EmailSequenceStepStore
from app.repositories.email_sequence_store import EmailSequenceStore


def _status_from_apollo(apollo_sequence: dict) -> EmailSequenceStatus:
    if apollo_sequence.get("archived"):
        return EmailSequenceStatus.ARCHIVED
    if apollo_sequence.get("active"):
        return EmailSequenceStatus.ACTIVE
    return EmailSequenceStatus.PAUSED


class EmailSequenceSyncService:
    def __init__(
        self,
        campaign_store: CampaignStore,
        store: EmailSequenceStore,
        step_store: EmailSequenceStepStore,
        apollo: ApolloClient | None = None,
    ):
        self.campaign_store = campaign_store
        self.store = store
        self.step_store = step_store
        self.apollo = apollo or ApolloClient()
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def get_for_campaign(self, campaign_id: str) -> tuple[EmailSequence, list[EmailSequenceStep]] | None:
        """Read-only: the currently stored sequence + steps, or None if never synced."""
        sequence = await self.store.get_by_campaign_id(campaign_id)
        if sequence is None:
            return None
        steps = await self.step_store.list_for_sequence(sequence.email_sequence_id)
        return sequence, steps

    async def sync(self, campaign_id: str) -> tuple[EmailSequence, list[EmailSequenceStep]]:
        async with self._locks[campaign_id]:
            campaign = await self.campaign_store.get(campaign_id)
            if campaign is None:
                raise CampaignNotFoundError(campaign_id)
            if not campaign.apollo_sequence_id:
                raise ValueError(
                    f"Campaign {campaign_id} has no Apollo sequence yet -- build it before syncing"
                )

            sequence = await self.store.get_by_campaign_id(campaign_id)
            if sequence is None:
                sequence = await self._create_snapshot(campaign_id, campaign)

            # Apollo call -- deliberately no try/except here. If this
            # raises, nothing below runs, so status/stats/last_synced_at
            # and step apollo_step_ids are left exactly as they were.
            resp = await self.apollo.search_sequences(campaign.apollo_sequence_id)
            apollo_sequences = resp.get("emailer_campaigns", [])
            apollo_sequence = next(
                (s for s in apollo_sequences if s.get("id") == campaign.apollo_sequence_id), None
            )
            if apollo_sequence is None:
                raise ValueError(
                    f"Apollo returned no sequence matching {campaign.apollo_sequence_id}"
                )

            now = datetime.now(timezone.utc)
            sequence.status = _status_from_apollo(apollo_sequence)
            sequence.status_reason = apollo_sequence.get("status_reason")
            sequence.unique_scheduled = apollo_sequence.get("unique_scheduled", 0)
            sequence.unique_delivered = apollo_sequence.get("unique_delivered", 0)
            sequence.unique_opened = apollo_sequence.get("unique_opened", 0)
            sequence.unique_clicked = apollo_sequence.get("unique_clicked", 0)
            sequence.unique_replied = apollo_sequence.get("unique_replied", 0)
            sequence.unique_bounced = apollo_sequence.get("unique_bounced", 0)
            sequence.unique_unsubscribed = apollo_sequence.get("unique_unsubscribed", 0)
            sequence.updated_at = now
            sequence.last_synced_at = now
            await self.store.save(sequence)

            await self._sync_steps(sequence.email_sequence_id, apollo_sequence.get("emailer_steps", []))

            steps = await self.step_store.list_for_sequence(sequence.email_sequence_id)
            return sequence, steps

    async def _create_snapshot(self, campaign_id: str, campaign: Campaign) -> EmailSequence:
        """
        The deployed-configuration snapshot, created exactly once from the
        Campaign's already-stored, immutable plan -- not an Apollo call, so
        it cannot fail the way sync's Apollo call can.
        """
        now = datetime.now(timezone.utc)
        sequence = EmailSequence(
            email_sequence_id=str(uuid.uuid4()),
            campaign_id=campaign_id,
            apollo_sequence_id=campaign.apollo_sequence_id,
            name=campaign.plan.campaign_name,
            status=EmailSequenceStatus.PAUSED,  # placeholder -- overwritten by the Apollo call below
            created_at=now,
            updated_at=now,
            last_synced_at=None,
        )
        await self.store.create(sequence)

        for i, step in enumerate(campaign.plan.sequence):
            await self.step_store.create(
                EmailSequenceStep(
                    email_sequence_step_id=str(uuid.uuid4()),
                    email_sequence_id=sequence.email_sequence_id,
                    apollo_step_id=None,
                    position=i + 1,
                    day=step.day,
                    subject=step.subject,
                    body=step.body,
                )
            )
        return sequence

    async def _sync_steps(self, email_sequence_id: str, apollo_steps: list[dict]) -> None:
        """Match our snapshot steps to Apollo's by position, filling in apollo_step_id. Idempotent."""
        apollo_step_id_by_position = {s["position"]: s.get("id") for s in apollo_steps if "position" in s}
        our_steps = await self.step_store.list_for_sequence(email_sequence_id)
        for step in our_steps:
            apollo_step_id = apollo_step_id_by_position.get(step.position)
            if apollo_step_id and step.apollo_step_id != apollo_step_id:
                step.apollo_step_id = apollo_step_id
                await self.step_store.save(step)
