"""
Pydantic models for the deployed email sequence -- Phase 1 of the
Campaign -> EmailSequence -> EmailMessage -> Lead chain (EmailMessage is
NOT built yet; see docs/ARCHITECTURE.md and the Apollo research this phase
was approved from).

EmailSequence is a durable, synced mirror of one Apollo sequence
(`/emailer_campaigns/search`) -- Apollo remains the source of truth for
status and aggregate engagement stats; we store a snapshot with
`last_synced_at`, refreshed only by an explicit sync action, never
presented as live/real-time.

EmailSequenceStep is a point-in-time snapshot of what was actually
deployed to Apollo -- captured from CampaignPlan.sequence (the immutable
Builder output) the first time this campaign's sequence is synced. It is
NOT a live mirror of CampaignPlan.sequence and is never regenerated from
it afterward -- if template editing exists in the future, the deployed
snapshot and a later-edited plan are allowed to differ; that divergence is
exactly what this snapshot is for.
"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel


class EmailSequenceStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"
    # Derived at sync time from Apollo's `active`/`archived` booleans --
    # never set independently by us. Apollo can autonomously pause a
    # sequence (e.g. bounce-rate safety -- confirmed via `auto_pause_*`
    # fields on the live /emailer_campaigns/search response) with zero
    # notification to us, which is exactly why this is a SYNCED field, not
    # something we infer from our own Campaign.status.


class EmailSequence(BaseModel):
    email_sequence_id: str
    workspace_id: str | None = None  # unused today -- same future-multi-tenancy convention as Campaign/Lead

    campaign_id: str  # 1:1 with Campaign today
    apollo_sequence_id: str  # the sync join key

    name: str
    status: EmailSequenceStatus
    status_reason: str | None = None  # Apollo's own `status_reason` field, passed through verbatim

    created_at: datetime
    updated_at: datetime
    last_synced_at: datetime | None = None  # None until the first successful sync

    # Checkpoint for the SEPARATE EmailMessage sync (app/services/
    # email_message_sync_service.py) -- distinct from last_synced_at above,
    # which only covers this sequence's own status/aggregate-stat sync.
    # Only advances once a full paginated message sweep succeeds; a
    # partial/failed sweep leaves it untouched, same discipline as
    # last_synced_at.
    messages_last_synced_at: datetime | None = None

    # Synced mirror of Apollo's own aggregate rollup (confirmed live on
    # /emailer_campaigns/search) -- NOT computed by us from anything else.
    # Always rendered alongside last_synced_at; never treated as real-time.
    unique_scheduled: int = 0
    unique_delivered: int = 0
    unique_opened: int = 0
    unique_clicked: int = 0
    unique_replied: int = 0
    unique_bounced: int = 0
    unique_unsubscribed: int = 0


class EmailSequenceStep(BaseModel):
    email_sequence_step_id: str
    email_sequence_id: str
    apollo_step_id: str | None = None  # None until a sync confirms Apollo's id for this position

    position: int  # 1-based, matches Apollo's `position`
    day: int  # snapshot from CampaignPlan.sequence at first-sync time
    subject: str
    body: str


class EmailSequenceWithSteps(EmailSequence):
    steps: list[EmailSequenceStep]
