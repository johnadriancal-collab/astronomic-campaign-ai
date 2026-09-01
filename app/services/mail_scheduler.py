"""
Pure, DB-free scheduling math for the Astronomic Mail sending engine (Phase
A). Nothing in this module reads or writes any store -- every function
takes plain values in and returns plain values out, so the full window/DST/
boundary matrix can be tested directly and cheaply (see tests/
test_mail_scheduler.py), and so MailSendingService (Stage 5) can call the
SAME functions both when it EAGERLY computes `next_send_at` at
materialization time and when it re-validates a claim is still inside a
legal window at actual send time -- there is deliberately only one
implementation of "is this instant legal," never two that could drift
apart (see Correction D in the approved Phase A spec: eager `next_send_at`
plus a mandatory final runtime re-check, never one or the other alone).

All datetimes in and out are timezone-aware UTC (`datetime.now(timezone.utc)`
-- matching every other timestamp in this codebase, see
mail_campaign_service.py). `timezone_name` is always the campaign's IANA
zone (already validated by validate_mail_timezone() before a campaign can
reach READY) -- conversion to/from campaign-local time happens entirely
inside these functions via zoneinfo; nothing outside this module should
ever construct a campaign-local datetime by hand.
"""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.models.mail import MailSendWindow

# Upper bound on how many campaign-local calendar days forward
# resolve_next_send_time() will walk before giving up. 7 unique weekdays is
# the theoretical max distance from any starting day-of-week to any other,
# so 8 guarantees every weekday has been checked at least once even when
# `not_before` lands mid-day on a day whose own window(s) have already
# passed. This is a search bound, not a business rule -- it exists only so
# a caller error (e.g. windows for zero days) fails fast with
# NoLegalSendWindowError instead of looping.
_MAX_DAYS_TO_SEARCH = 8


class NoLegalSendWindowError(Exception):
    """Raised when no campaign-local send window exists within
    _MAX_DAYS_TO_SEARCH days of `not_before`. In practice this should never
    happen for an ACTIVE campaign -- mark_ready()/activate_campaign() both
    require at least one valid, non-empty MailSendWindow before a campaign
    can reach READY/ACTIVE (see _compute_readiness_warnings()) -- so seeing
    this raised in production would indicate that invariant was somehow
    violated after activation, not a normal runtime condition to design
    graceful handling around."""

    def __init__(self, not_before: datetime):
        self.not_before = not_before
        super().__init__(f"No legal send window found within {_MAX_DAYS_TO_SEARCH} days of {not_before.isoformat()}")


def _windows_by_weekday(windows: list[MailSendWindow]) -> dict[int, list[MailSendWindow]]:
    by_day: dict[int, list[MailSendWindow]] = {}
    for window in windows:
        by_day.setdefault(window.day_of_week, []).append(window)
    for day_windows in by_day.values():
        day_windows.sort(key=lambda w: w.start_time)
    return by_day


def resolve_next_send_time(windows: list[MailSendWindow], timezone_name: str, not_before: datetime) -> datetime:
    """The next legal UTC instant >= `not_before` that falls inside one of
    `windows`, evaluated in `timezone_name`-local time. If `not_before`
    itself already falls inside a window, returns `not_before` unchanged
    (an already-due send is never pushed later just because this function
    ran) -- callers that want "the start of the next window strictly after
    now" (there is no such caller in Phase A) would need different logic.

    Window boundaries: start_time is inclusive, end_time is exclusive --
    matching validate_send_windows()'s "touching boundaries are not an
    overlap" rule (a window ending at 12:00 and one starting at 12:00 are
    back-to-back, never double-counted, never a gap).

    Raises NoLegalSendWindowError if `windows` is empty or no window is
    found within _MAX_DAYS_TO_SEARCH days -- see that exception's
    docstring for why this is a defensive invariant check, not an expected
    runtime branch.
    """
    if not windows:
        raise NoLegalSendWindowError(not_before)

    tz = ZoneInfo(timezone_name)
    local_not_before = not_before.astimezone(tz)
    by_weekday = _windows_by_weekday(windows)

    for day_offset in range(_MAX_DAYS_TO_SEARCH):
        candidate_date = local_not_before.date() + timedelta(days=day_offset)
        day_windows = by_weekday.get(candidate_date.weekday(), [])
        for window in day_windows:
            window_start_local = datetime.combine(candidate_date, window.start_time, tzinfo=tz)
            window_end_local = datetime.combine(candidate_date, window.end_time, tzinfo=tz)

            if day_offset == 0:
                if local_not_before >= window_end_local:
                    continue  # this window already ended today
                candidate_local = max(local_not_before, window_start_local)
            else:
                # A future day relative to not_before -- not_before cannot
                # already be "inside" it, so the earliest legal instant is
                # always this window's own start.
                candidate_local = window_start_local

            return candidate_local.astimezone(timezone.utc)

    raise NoLegalSendWindowError(not_before)


def is_within_window(windows: list[MailSendWindow], timezone_name: str, at: datetime) -> bool:
    """Whether the exact instant `at` falls inside one of `windows`,
    evaluated in `timezone_name`-local time. This is the runtime re-check
    MailSendingService must call immediately before a step may cross
    CLAIMED->SENDING -- a stale `next_send_at <= now` computed at
    materialization time is NEVER sufficient on its own: a worker can run
    late, well past a window's end, and must not send outside it just
    because it finally got around to a row. Same inclusive-start/
    exclusive-end convention as resolve_next_send_time()."""
    tz = ZoneInfo(timezone_name)
    local_at = at.astimezone(tz)
    weekday = local_at.date().weekday()
    local_time_of_day = local_at.timetz().replace(tzinfo=None)

    for window in windows:
        if window.day_of_week != weekday:
            continue
        if window.start_time <= local_time_of_day < window.end_time:
            return True
    return False


def compute_eligible_at(sent_at: datetime, delay_days: int, timezone_name: str) -> datetime:
    """The earliest instant a follow-up step becomes eligible to send,
    given the prior step's `sent_at` -- calendar days in campaign-local
    time, preserving wall-clock time-of-day, per the approved Phase A
    decision (a Step sent at 9:03am campaign-local with a 2-day delay
    becomes eligible at 9:03am campaign-local two calendar days later, NOT
    exactly 48 hours later). This is date arithmetic (`date + timedelta`),
    not a raw `timedelta(days=...)` added to the instant, specifically so
    it survives a DST transition inside the delay window without drifting
    the wall-clock hour.

    Returned value is only the ELIGIBILITY floor, not the final
    `next_send_at` -- the caller must still pass this through
    resolve_next_send_time() to land it inside an actual legal window
    (delay_days elapsing on a day/time outside every window is the normal
    case, not an edge case).

    A `delay_days` that lands on a campaign-local wall-clock time that is
    skipped or repeated by a DST transition (e.g. 2:30am during a
    spring-forward) is resolved however Python's zoneinfo/PEP 495 resolves
    it by default (fold=0) -- Phase A does not add extra handling for this
    narrow case, since resolve_next_send_time() immediately re-projects
    the result into the nearest legal window regardless."""
    tz = ZoneInfo(timezone_name)
    local_sent_at = sent_at.astimezone(tz)
    target_date: date = local_sent_at.date() + timedelta(days=delay_days)
    local_eligible_at = datetime.combine(target_date, local_sent_at.time(), tzinfo=tz)
    return local_eligible_at.astimezone(timezone.utc)
