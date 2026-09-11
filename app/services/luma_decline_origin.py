"""
Client CRM Stage 5A (2026-09-11) -- the ONE place that translates a Luma
approval_status transition (or a first-ever-seen already-declined
registration) into the provider-agnostic EngagementParticipant.decline_origin
fact. Deliberately the only place this logic exists -- called from
LumaSyncService (which is the only place that still holds the PRE-overwrite
LumaRegistration state, since LumaRegistrationStore.save() upserts in place
and never keeps history) and never duplicated in LumaEngagementParticipantSyncService,
which only ever receives the already-computed result.

Two, and only two, situations ever produce a DeclineOrigin here:

1. An OBSERVED transition -- AstroHub already had a stored LumaRegistration
   for this guest, and the new delivery's approval_status differs from it,
   landing on DECLINED. This is real evidence, not an inference -- see
   derive_from_observed_transition().

2. A FIRST-SEEN already-declined registration -- AstroHub had no prior
   registration for this guest at all (registration_is_new), and the very
   first delivery we ever see already reports approval_status=declined
   (the Scott Brinkman production case: Luma's own UI timeline showed
   Invited -> Not Going, but our very first webhook delivery for him
   already carried "declined", so no transition was ever observed on our
   side). This is a deliberately CONSERVATIVE, medium-confidence
   operational inference from the registration's own invited_at/
   registered_at timestamps, not a certainty -- see
   derive_from_first_seen_declined().

Neither function ever returns a "guessed" GUEST/HOST for an ambiguous case
-- UNKNOWN is the safe default whenever the evidence doesn't clearly
support one of the two locked rules.
"""

from datetime import datetime

from app.models.client_crm import DeclineOrigin
from app.models.luma import LumaApprovalStatus


def derive_from_observed_transition(previous_approval_status: LumaApprovalStatus) -> DeclineOrigin:
    """Called only when AstroHub actually observed `previous_approval_status
    != declined -> declined` (see this module's own docstring, situation 1).
    Locked mapping:
        pending_approval -> declined  => HOST  (the host/Astronomic rejected
            a registration that was awaiting their own approval)
        invited -> declined           => GUEST (the guest themselves
            rejected an invitation)
    Every other observed previous state (approved, waitlist, session, or
    anything not in this enum) => UNKNOWN -- deliberately never guessed;
    see this module's own docstring and the Stage 5A investigation report's
    own "ambiguous transitions" section for why (e.g. an approved guest
    later declining could be self-cancellation OR a host revoking
    attendance -- Luma's payload gives no way to tell which)."""
    if previous_approval_status == LumaApprovalStatus.PENDING_APPROVAL:
        return DeclineOrigin.HOST
    if previous_approval_status == LumaApprovalStatus.INVITED:
        return DeclineOrigin.GUEST
    return DeclineOrigin.UNKNOWN


def derive_from_first_seen_declined(invited_at: datetime | None, registered_at: datetime | None) -> DeclineOrigin:
    """Called only when this guest's registration is brand new to AstroHub
    (registration_is_new) AND its very first observed approval_status is
    already declined -- see this module's own docstring, situation 2.

    Deliberately CONSERVATIVE, medium-confidence rule, locked exactly as
    specified: `invited_at` set AND `registered_at` null => GUEST (invited,
    never actively registered -- consistent with, though not proof of, a
    self-declined invitation). Every other combination => UNKNOWN,
    INCLUDING `registered_at` set (regardless of invited_at) -- a person
    who actively registered before we ever observed them could have later
    been declined by either party, and this rule must never infer HOST
    from registered_at's mere presence, per the locked product decision."""
    if invited_at is not None and registered_at is None:
        return DeclineOrigin.GUEST
    return DeclineOrigin.UNKNOWN
