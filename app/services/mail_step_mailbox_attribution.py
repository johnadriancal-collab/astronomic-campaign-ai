"""
Shared, safe "which mailbox does this not-yet-sent step belong to"
attribution logic (2026-09-18) -- used by both MailboxMetricsService
(Queue count) and MailCampaignMailboxNextSendService (proactive OAuth
expiration warnings). One implementation, so the two features can never
silently drift apart on the exact same underlying question.

Exists because MailEnrollmentStep.mailbox_id and
MailEnrollment.assigned_mailbox_id are both null until an enrollment's
Step 1 is actually claimed (see MailSendingService.
assign_mailbox_if_needed()/persist_prepared_fields()) -- a not-yet-sent
step cannot always be attributed to a mailbox by reading its own row.
"""

# Steps that haven't been sent yet and aren't in a terminal state --
# PENDING (not yet eligible), QUEUED (eligible, due now or later), CLAIMED
# (a worker won it, running safety checks, no provider call made yet).
# Deliberately excludes SENDING (a provider call is actively in flight --
# "sending", not "waiting") and every terminal status (SENT, FAILED,
# SKIPPED_REPLIED, SKIPPED_SUPPRESSED, UNKNOWN).
from app.models.mail import MailEnrollmentStepStatus

WAITING_STEP_STATUSES = {
    MailEnrollmentStepStatus.PENDING,
    MailEnrollmentStepStatus.QUEUED,
    MailEnrollmentStepStatus.CLAIMED,
}


def attribute_step_to_mailbox(assigned_mailbox_id: str | None, channel_mailbox_ids: list[str]) -> str | None:
    """Which mailbox (if any) a waiting step should be counted against.

    Exact, never guessed: a step whose enrollment already has a sticky
    assigned_mailbox_id (set once that lead's Step 1 is claimed -- see
    MailEnrollment.assigned_mailbox_id's own docstring) is attributed
    there. An enrollment that hasn't been assigned yet is only
    attributed when the campaign has exactly one channel mailbox --
    the sole possible candidate. A not-yet-assigned enrollment on a
    campaign with multiple channel mailboxes is left unattributed
    (returns None) rather than guessed, since the eventual sender
    depends on MailSendingService._pick_mailbox_deterministic()'s
    runtime choice.
    """
    if assigned_mailbox_id is not None:
        return assigned_mailbox_id
    if len(channel_mailbox_ids) == 1:
        return channel_mailbox_ids[0]
    return None
