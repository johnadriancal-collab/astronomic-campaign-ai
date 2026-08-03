"""
Pydantic models for EmailMessage/EmailMessageEvent -- the final links in
the Campaign -> EmailSequence -> EmailMessage -> Lead chain (see
docs/APOLLO_MESSAGE_API_FINDINGS.md #8 for the live-data research and
architecture decisions this implements).

Two hard rules carried over from that research, both enforced by shape
rather than by convention:

1. `EmailMessage.status` is an open `str`, not a closed Enum. Apollo has
   only ever been observed returning "completed"/"failed" live, but a
   closed enum would reject or force-guess at any value Apollo adds later
   (e.g. a pending/scheduled state). Store the raw value, always.

2. `EmailMessageSource` distinguishes real synced Apollo data from locally
   fabricated demo data -- this is NOT an Apollo field, it's ours, so it's
   the one legitimately closed enum here. Every message/event is one or
   the other, never ambiguous, and the UI must always render this
   distinction (never present a fixture as live Apollo data).

Opens/clicks are NOT counted fields on EmailMessage -- Apollo's own API
has no such field (confirmed live), so counts are derived by counting
EmailMessageEvent rows at query time, per the "computed, not duplicated"
convention (docs/ARCHITECTURE.md #7.3) already used for EmailSequence's
own aggregate stats.
"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel


class EmailMessageSource(str, Enum):
    APOLLO_SYNC = "apollo_sync"  # persisted from a real, live Apollo API response
    TEST_FIXTURE = "test_fixture"  # fabricated locally; Apollo was never called


class EmailMessage(BaseModel):
    email_message_id: str
    apollo_message_id: str | None = None  # Apollo's own message id; None only for test fixtures -- the dedup key for sync

    email_sequence_id: str
    email_sequence_step_id: str | None = None
    apollo_touch_id: str | None = None  # Apollo's `emailer_touch_id` -- confirmed live to exist, exact semantics still unknown

    lead_id: str  # resolved via contact_id -> Lead.apollo_contact_id at sync time

    status: str  # Apollo's raw value, open string -- see module docstring
    failure_reason: str | None = None
    bounce: bool = False
    spam_blocked: bool = False

    replied: bool = False
    reply_class: str | None = None  # Apollo's own reply classification, passed through verbatim

    provider_message_id: str | None = None  # underlying Gmail message id -- future Gmail/Outlook Inbox bridge
    provider_thread_id: str | None = None  # underlying Gmail thread id

    created_at: datetime | None = None
    due_at: datetime | None = None
    completed_at: datetime | None = None
    failed_at: datetime | None = None

    source: EmailMessageSource
    last_synced_at: datetime | None = None  # None for test fixtures -- they were never synced from Apollo


class EmailMessageEvent(BaseModel):
    email_message_event_id: str
    email_message_id: str
    apollo_event_id: str | None = None  # Apollo's own event id; None only for test fixtures -- the dedup key for sync

    event_type: str  # raw `event_group_type`/`type` ("open"/"click"), open string -- same reasoning as EmailMessage.status
    occurred_at: datetime  # Apollo's per-event created_at -- confirmed live to be individually timestamped, not aggregated

    apollo_contact_id: str | None = None
    readable_user_agent: str | None = None
    region: str | None = None  # Apollo's `state`
    country: str | None = None

    source: EmailMessageSource


class EmailMessageWithEventCounts(EmailMessage):
    """
    Read view for list endpoints -- open_count/click_count are computed
    from this message's EmailMessageEvent rows by the service layer, never
    stored on EmailMessage itself.
    """

    open_count: int = 0
    click_count: int = 0
