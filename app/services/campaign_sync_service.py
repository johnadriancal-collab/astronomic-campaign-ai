"""
CampaignSyncService -- keeps our Campaign/EmailSequence/EmailSequenceStep
records synchronized with Apollo. Today Apollo is the only source this
reads from; the service is deliberately named and shaped around the
*operation* (sync), not the provider, so a future push-to-Apollo,
reconcile, or additional-provider capability is a new method here, not a
new service.

sync() does two passes, both against data gathered from a single,
consistent snapshot of Apollo's current sequence list:

1. Discover/update -- page through Apollo's full sequence list (using its
   real `pagination.total_pages`, confirmed reliable, unlike
   /emailer_messages/search). For each sequence Apollo returns:
     - Not known locally -> create a new Campaign(source=SYNCED) +
       EmailSequence + EmailSequenceStep(s).
     - Known locally, and that Campaign's source is SYNCED -> Apollo is
       source of truth for it, so name/status/stats/steps are overwritten
       from Apollo's current data every run.
     - Known locally, but that Campaign's source is NATIVE -> untouched.
       A campaign we built ourselves owns its own name/plan/steps; this
       service only mirrors Apollo's status for it via the reconcile pass
       below (EmailSequence.status only), never its content.

2. Reconcile archived/deleted -- for every locally-known EmailSequence
   (NATIVE or SYNCED, not just ones this service created) that did NOT
   appear in this run's full list, confirm via a direct per-id lookup
   (list_sequences' filter params don't work, see app/apollo/sequences.py)
   and mark EmailSequenceStatus.ARCHIVED on either an explicit
   `archived: true` or a 404 (treated the same -- soft-hide, never
   hard-delete, per explicit product decision). Any other outcome from
   that lookup leaves the record untouched; this service never guesses.

No try/except around the discover/update pass's Apollo calls -- a failure
there raises and the caller (the API route) turns that into a 502.
Whatever writes already completed stay completed (they were real,
individually-successful writes); the run just doesn't produce a report.

The reconcile pass has exactly one narrow, deliberate exception to that:
each per-id lookup IS wrapped, but only to distinguish a definite 404
(treated as "confirmed gone") from every other failure (re-raised
untouched, same as everywhere else) -- an ambiguous error must never be
silently read as "archived." This is the one place in the whole sync run
where catching an exception is the correct, safer choice, not a shortcut
around it.

Known, deliberate limitation: Apollo's sequence-list/get endpoints return
step position/day/id but NOT subject/body content (confirmed live --
`emailer_steps[].emailer_touches` is an empty list on every real sequence
checked). Synced campaigns' EmailSequenceStep.subject/body are therefore
stored as empty strings rather than fabricated -- there is currently no
verified Apollo endpoint this service calls that provides real step copy
for a sequence we didn't build ourselves.
"""

import time
import uuid
from datetime import datetime, timezone

from app.apollo import ApolloClient
from app.models.campaign import Campaign, CampaignPlan, CampaignSource, CampaignStatus, Filters, SequenceStep
from app.models.campaign_sync import CampaignSyncReport
from app.models.email_sequence import EmailSequence, EmailSequenceStatus, EmailSequenceStep
from app.repositories.campaign_store import CampaignStore
from app.repositories.email_sequence_step_store import EmailSequenceStepStore
from app.repositories.email_sequence_store import EmailSequenceStore

MAX_SYNC_PAGES = 100  # safety backstop, not a silent cap -- raises loudly rather than looping unbounded


def _status_from_apollo(apollo_sequence: dict) -> EmailSequenceStatus:
    if apollo_sequence.get("archived"):
        return EmailSequenceStatus.ARCHIVED
    if apollo_sequence.get("active"):
        return EmailSequenceStatus.ACTIVE
    return EmailSequenceStatus.PAUSED


def _campaign_status_from_apollo(apollo_sequence: dict) -> CampaignStatus:
    return CampaignStatus.ACTIVE if apollo_sequence.get("active") else CampaignStatus.PAUSED


def _cumulative_days(apollo_steps: list[dict]) -> dict[int, int]:
    """Apollo's wait_time is the delta from the previous step; we store cumulative day, same conversion add_sequence_steps() already does in reverse."""
    ordered = sorted(apollo_steps, key=lambda s: s.get("position", 0))
    day_by_position: dict[int, int] = {}
    running = 0
    for i, step in enumerate(ordered):
        position = step.get("position", i + 1)
        running = step.get("wait_time", 0) if i == 0 else running + step.get("wait_time", 0)
        day_by_position[position] = running
    return day_by_position


class CampaignSyncService:
    def __init__(
        self,
        campaign_store: CampaignStore,
        sequence_store: EmailSequenceStore,
        step_store: EmailSequenceStepStore,
        apollo: ApolloClient | None = None,
    ):
        self.campaign_store = campaign_store
        self.sequence_store = sequence_store
        self.step_store = step_store
        self.apollo = apollo or ApolloClient()

    async def sync(self) -> CampaignSyncReport:
        started = time.monotonic()

        apollo_sequences = await self._fetch_all_sequences()

        created = updated = unchanged = 0
        seen_apollo_ids: set[str] = set()

        for apollo_sequence in apollo_sequences:
            apollo_id = apollo_sequence.get("id")
            if not apollo_id:
                continue
            seen_apollo_ids.add(apollo_id)

            existing = await self.sequence_store.get_by_apollo_sequence_id(apollo_id)
            if existing is None:
                await self._create_synced_campaign(apollo_sequence)
                created += 1
                continue

            campaign = await self.campaign_store.get(existing.campaign_id)
            if campaign is None or campaign.source != CampaignSource.SYNCED:
                continue  # NATIVE campaign's content is never touched here

            changed = await self._update_synced_campaign(campaign, existing, apollo_sequence)
            if changed:
                updated += 1
            else:
                unchanged += 1

        archived = await self._reconcile_archived(seen_apollo_ids)

        duration_ms = (time.monotonic() - started) * 1000
        return CampaignSyncReport(
            found=len(apollo_sequences),
            created=created,
            updated=updated,
            archived=archived,
            unchanged=unchanged,
            duration_ms=duration_ms,
        )

    async def _fetch_all_sequences(self) -> list[dict]:
        all_sequences: list[dict] = []
        page = 1
        while True:
            if page > MAX_SYNC_PAGES:
                raise RuntimeError(f"Exceeded {MAX_SYNC_PAGES} pages listing Apollo sequences -- aborting")
            # Apollo call -- no try/except; a failure here aborts the whole sync.
            resp = await self.apollo.list_sequences(page=page, per_page=100)
            batch = resp.get("emailer_campaigns", [])
            all_sequences.extend(batch)
            pagination = resp.get("pagination") or {}
            total_pages = pagination.get("total_pages", 1)
            if page >= total_pages:
                break
            page += 1
        return all_sequences

    async def _create_synced_campaign(self, apollo_sequence: dict) -> None:
        now = datetime.now(timezone.utc)
        apollo_id = apollo_sequence["id"]
        name = apollo_sequence.get("name") or "(untitled sequence)"
        apollo_steps = apollo_sequence.get("emailer_steps", [])
        day_by_position = _cumulative_days(apollo_steps)

        campaign = Campaign(
            campaign_id=str(uuid.uuid4()),
            original_prompt="",
            created_at=now,
            status=_campaign_status_from_apollo(apollo_sequence),
            source=CampaignSource.SYNCED,
            plan=CampaignPlan(
                campaign_name=name,
                filters=Filters(),
                sequence=[
                    SequenceStep(day=day_by_position.get(s.get("position", i + 1), 0), subject="", body="")
                    for i, s in enumerate(sorted(apollo_steps, key=lambda s: s.get("position", 0)))
                ],
            ),
            apollo_sequence_id=apollo_id,
        )
        await self.campaign_store.create(campaign)

        sequence = EmailSequence(
            email_sequence_id=str(uuid.uuid4()),
            campaign_id=campaign.campaign_id,
            apollo_sequence_id=apollo_id,
            name=name,
            status=_status_from_apollo(apollo_sequence),
            status_reason=apollo_sequence.get("status_reason"),
            created_at=now,
            updated_at=now,
            last_synced_at=now,
            unique_scheduled=apollo_sequence.get("unique_scheduled", 0),
            unique_delivered=apollo_sequence.get("unique_delivered", 0),
            unique_opened=apollo_sequence.get("unique_opened", 0),
            unique_clicked=apollo_sequence.get("unique_clicked", 0),
            unique_replied=apollo_sequence.get("unique_replied", 0),
            unique_bounced=apollo_sequence.get("unique_bounced", 0),
            unique_unsubscribed=apollo_sequence.get("unique_unsubscribed", 0),
        )
        await self.sequence_store.create(sequence)

        for i, step in enumerate(sorted(apollo_steps, key=lambda s: s.get("position", 0))):
            position = step.get("position", i + 1)
            await self.step_store.create(
                EmailSequenceStep(
                    email_sequence_step_id=str(uuid.uuid4()),
                    email_sequence_id=sequence.email_sequence_id,
                    apollo_step_id=step.get("id"),
                    position=position,
                    day=day_by_position.get(position, 0),
                    subject="",
                    body="",
                )
            )

    async def _update_synced_campaign(
        self, campaign: Campaign, sequence: EmailSequence, apollo_sequence: dict
    ) -> bool:
        """Apollo is source of truth for a SYNCED campaign's name/status/stats/steps. Returns whether anything actually changed."""
        changed = False
        now = datetime.now(timezone.utc)

        name = apollo_sequence.get("name") or "(untitled sequence)"
        new_campaign_status = _campaign_status_from_apollo(apollo_sequence)
        if campaign.plan.campaign_name != name or campaign.status != new_campaign_status:
            campaign.plan.campaign_name = name
            campaign.status = new_campaign_status
            await self.campaign_store.save(campaign)
            changed = True

        new_status = _status_from_apollo(apollo_sequence)
        new_status_reason = apollo_sequence.get("status_reason")
        stat_fields = (
            "unique_scheduled",
            "unique_delivered",
            "unique_opened",
            "unique_clicked",
            "unique_replied",
            "unique_bounced",
            "unique_unsubscribed",
        )
        sequence_changed = (
            sequence.name != name
            or sequence.status != new_status
            or sequence.status_reason != new_status_reason
            or any(getattr(sequence, f) != apollo_sequence.get(f, 0) for f in stat_fields)
        )
        if sequence_changed:
            sequence.name = name
            sequence.status = new_status
            sequence.status_reason = new_status_reason
            for f in stat_fields:
                setattr(sequence, f, apollo_sequence.get(f, 0))
            sequence.updated_at = now
            changed = True
        sequence.last_synced_at = now
        await self.sequence_store.save(sequence)

        steps_changed = await self._upsert_steps_from_apollo(
            sequence.email_sequence_id, apollo_sequence.get("emailer_steps", [])
        )
        return changed or steps_changed

    async def _upsert_steps_from_apollo(self, email_sequence_id: str, apollo_steps: list[dict]) -> bool:
        changed = False
        day_by_position = _cumulative_days(apollo_steps)
        existing_by_position = {s.position: s for s in await self.step_store.list_for_sequence(email_sequence_id)}

        for i, apollo_step in enumerate(sorted(apollo_steps, key=lambda s: s.get("position", 0))):
            position = apollo_step.get("position", i + 1)
            day = day_by_position.get(position, 0)
            apollo_step_id = apollo_step.get("id")
            existing_step = existing_by_position.get(position)

            if existing_step is None:
                await self.step_store.create(
                    EmailSequenceStep(
                        email_sequence_step_id=str(uuid.uuid4()),
                        email_sequence_id=email_sequence_id,
                        apollo_step_id=apollo_step_id,
                        position=position,
                        day=day,
                        subject="",
                        body="",
                    )
                )
                changed = True
            elif existing_step.apollo_step_id != apollo_step_id or existing_step.day != day:
                existing_step.apollo_step_id = apollo_step_id
                existing_step.day = day
                await self.step_store.save(existing_step)
                changed = True

        return changed

    async def _reconcile_archived(self, seen_apollo_ids: set[str]) -> int:
        archived_count = 0
        all_sequences = await self.sequence_store.list_all()
        for sequence in all_sequences:
            if sequence.status == EmailSequenceStatus.ARCHIVED:
                continue
            if sequence.apollo_sequence_id in seen_apollo_ids:
                continue

            # Apollo call -- no try/except for the same reason as above:
            # an ambiguous failure here must not be silently interpreted
            # as "archived." Only a definite 404 or archived:true does.
            try:
                resp = await self.apollo.get_sequence(sequence.apollo_sequence_id)
            except Exception as e:
                status_code = getattr(e, "status_code", None)
                if status_code == 404:
                    sequence.status = EmailSequenceStatus.ARCHIVED
                    sequence.updated_at = datetime.now(timezone.utc)
                    await self.sequence_store.save(sequence)
                    archived_count += 1
                    continue
                raise

            apollo_sequence = resp.get("emailer_campaign")
            if apollo_sequence and apollo_sequence.get("archived"):
                sequence.status = EmailSequenceStatus.ARCHIVED
                sequence.updated_at = datetime.now(timezone.utc)
                await self.sequence_store.save(sequence)
                archived_count += 1
            # Any other outcome (still visible, not archived -- an
            # unexplained gap in this run's list) is left untouched.

        return archived_count
