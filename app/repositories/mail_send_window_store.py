"""
Storage abstraction for MailSendWindow -- a campaign's real, explicit send
windows (Schedule Phase 2). Structurally mirrors mail_sequence_step_store.py
(an owned, multi-row-per-campaign child entity with its own stable id) more
than it mirrors mail_campaign_mailbox_store.py (a pure join table) -- a
MailSendWindow has real fields of its own (day_of_week/start_time/end_time),
not just a bare foreign-key reference.

`replace_for_campaign()` is a full, atomic replace of a campaign's ENTIRE
window set (matching the Schedule tab's single "Save" action and PUT
.../schedule's "atomic full replacement" contract) -- see
sqlite_mail_send_window_store.py for the transaction guarantee. This store
itself is deliberately dumb about identity: it writes exactly the
`MailSendWindow` objects it's given, full stop. Deciding WHICH window_ids
survive a save (an existing id whose row is being edited keeps its
identity/created_at; an id with no window_id in the request mints a fresh
one; an existing id absent from the request is dropped) is
MailCampaignService.set_schedule()'s job, not this store's -- it resolves
that against list_for_campaign()'s current rows before ever calling
replace_for_campaign(). See MailSendWindow's own docstring for why identity
continuity across saves matters.
"""

from abc import ABC, abstractmethod

from app.models.mail import MailSendWindow


class MailSendWindowStore(ABC):
    @abstractmethod
    async def list_for_campaign(self, mail_campaign_id: str) -> list[MailSendWindow]:
        """Every window currently saved for this campaign, ordered by
        (day_of_week, start_time). Empty list means either no schedule has
        ever been saved through the new Schedule tab, or the campaign was
        explicitly saved with zero windows (an intentional all-days-off
        schedule) -- callers cannot distinguish those two cases from this
        alone; MailCampaignService._resolve_schedule() is what decides
        whether to fall back to synthesizing legacy fields."""

    @abstractmethod
    async def replace_for_campaign(self, mail_campaign_id: str, windows: list[MailSendWindow]) -> None:
        """Atomically replaces this campaign's ENTIRE window set with
        exactly `windows`. All-or-nothing: on any failure, the previous
        windows are left completely unchanged."""


class MemoryMailSendWindowStore(MailSendWindowStore):
    """Dict-backed, keyed by mail_campaign_id -- not persistent, for tests/local dev."""

    def __init__(self):
        self._rows: dict[str, list[MailSendWindow]] = {}

    async def list_for_campaign(self, mail_campaign_id: str) -> list[MailSendWindow]:
        windows = self._rows.get(mail_campaign_id, [])
        return sorted(windows, key=lambda w: (w.day_of_week, w.start_time))

    async def replace_for_campaign(self, mail_campaign_id: str, windows: list[MailSendWindow]) -> None:
        self._rows[mail_campaign_id] = list(windows)
