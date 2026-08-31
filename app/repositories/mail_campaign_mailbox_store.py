"""
Storage abstraction for the MailCampaign<->Mailbox "Channels" join -- which
already-connected mailboxes are allowed to send a given campaign. Same
shape/idempotency contract as CrmContactListMemberStore (composite primary
key, no separate model needed since this store only ever deals in bare ids).

`replace_for_campaign()` is a full, atomic replace of a campaign's selected
mailbox set (matching the Channels tab's single "Save" action) rather than
per-checkbox add()/remove() calls -- see sqlite_mail_campaign_mailbox_store.py
for the transaction guarantee. It preserves each surviving mailbox_id's
original `added_at` rather than resetting it on every save, and silently
de-duplicates its input (a duplicate id in the request can never produce two
rows, since the table's primary key is (mail_campaign_id, mailbox_id)).

This store never validates that a mailbox_id is real or usable -- that's
MailCampaignService's job (it already holds the MailboxStore reference this
would otherwise duplicate). It also never touches `mailboxes` or
`mailbox_credentials` at all -- a disconnected mailbox's link row here is
left exactly as-is (see MailCampaignService.set_channel_mailboxes()'s
docstring for why that's the deliberate, correct behavior).
"""

from abc import ABC, abstractmethod
from datetime import datetime, timezone


class MailCampaignMailboxStore(ABC):
    @abstractmethod
    async def list_mailbox_ids_for_campaign(self, mail_campaign_id: str) -> list[str]:
        """Every mailbox_id currently selected for this campaign, in no
        particular order."""

    @abstractmethod
    async def replace_for_campaign(self, mail_campaign_id: str, mailbox_ids: list[str]) -> None:
        """Atomically replaces this campaign's ENTIRE selected mailbox set
        with exactly `mailbox_ids` (deduplicated). All-or-nothing: on any
        failure, the previous selection is left completely unchanged."""


class MemoryMailCampaignMailboxStore(MailCampaignMailboxStore):
    """Dict-backed, keyed by mail_campaign_id -- not persistent, for tests/local dev."""

    def __init__(self):
        self._rows: dict[str, dict[str, datetime]] = {}

    async def list_mailbox_ids_for_campaign(self, mail_campaign_id: str) -> list[str]:
        return list(self._rows.get(mail_campaign_id, {}).keys())

    async def replace_for_campaign(self, mail_campaign_id: str, mailbox_ids: list[str]) -> None:
        deduped = list(dict.fromkeys(mailbox_ids))
        existing = self._rows.get(mail_campaign_id, {})
        now = datetime.now(timezone.utc)
        self._rows[mail_campaign_id] = {mailbox_id: existing.get(mailbox_id, now) for mailbox_id in deduped}
