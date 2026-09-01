"""
Tests for app/services/mail_scheduler.py -- pure, DB-free window resolution
and calendar-day delay math. No stores, no services, no fixtures beyond
plain MailSendWindow construction -- see that module's own docstring for
why these functions are deliberately dependency-free.
"""

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.models.mail import MailSendWindow
from app.services.mail_scheduler import (
    NoLegalSendWindowError,
    compute_eligible_at,
    is_within_window,
    resolve_next_send_time,
)

TZ = "America/Chicago"
_STAMP = datetime(2020, 1, 1, tzinfo=timezone.utc)


def mkwindow(day_of_week: int, start: str, end: str) -> MailSendWindow:
    return MailSendWindow(
        window_id=f"w-{day_of_week}-{start}-{end}",
        mail_campaign_id="c1",
        day_of_week=day_of_week,
        start_time=time.fromisoformat(start),
        end_time=time.fromisoformat(end),
        created_at=_STAMP,
        updated_at=_STAMP,
    )


def local(year, month, day, hour, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(TZ))


# 2026-09-07 is a Monday.
MONDAY = (2026, 9, 7)


class TestResolveNextSendTime:
    def test_before_first_window_pushes_to_window_start(self):
        windows = [mkwindow(0, "09:00", "17:00")]
        not_before = local(*MONDAY, 8, 0)
        result = resolve_next_send_time(windows, TZ, not_before.astimezone(timezone.utc))
        assert result.astimezone(ZoneInfo(TZ)) == local(*MONDAY, 9, 0)

    def test_mid_window_returns_the_same_instant_unchanged(self):
        windows = [mkwindow(0, "09:00", "17:00")]
        not_before = local(*MONDAY, 10, 30)
        result = resolve_next_send_time(windows, TZ, not_before.astimezone(timezone.utc))
        assert result == not_before.astimezone(timezone.utc)

    def test_after_final_window_rolls_to_next_matching_day(self):
        windows = [mkwindow(0, "09:00", "17:00")]  # Monday only
        not_before = local(*MONDAY, 18, 0)
        result = resolve_next_send_time(windows, TZ, not_before.astimezone(timezone.utc))
        result_local = result.astimezone(ZoneInfo(TZ))
        assert result_local.date() == local(*MONDAY, 9, 0).date() + timedelta(days=7)
        assert result_local.hour == 9

    def test_between_two_windows_same_day_uses_the_gap(self):
        windows = [mkwindow(0, "09:00", "12:00"), mkwindow(0, "14:00", "17:00")]
        not_before = local(*MONDAY, 13, 0)
        result = resolve_next_send_time(windows, TZ, not_before.astimezone(timezone.utc))
        result_local = result.astimezone(ZoneInfo(TZ))
        assert result_local.date() == local(*MONDAY, 13, 0).date()
        assert result_local.hour == 14

    def test_multiple_windows_per_day_picks_the_earliest_valid_one(self):
        windows = [mkwindow(0, "14:00", "17:00"), mkwindow(0, "09:00", "12:00")]  # deliberately out of order
        not_before = local(*MONDAY, 7, 0)
        result = resolve_next_send_time(windows, TZ, not_before.astimezone(timezone.utc))
        assert result.astimezone(ZoneInfo(TZ)).hour == 9

    def test_no_window_day_is_skipped_over(self):
        # Monday has no window; Wednesday (weekday 2) does.
        windows = [mkwindow(2, "09:00", "17:00")]
        not_before = local(*MONDAY, 8, 0)
        result = resolve_next_send_time(windows, TZ, not_before.astimezone(timezone.utc))
        result_local = result.astimezone(ZoneInfo(TZ))
        assert result_local.weekday() == 2
        assert result_local.hour == 9

    def test_start_time_boundary_is_inclusive(self):
        windows = [mkwindow(0, "09:00", "17:00")]
        not_before = local(*MONDAY, 9, 0)
        result = resolve_next_send_time(windows, TZ, not_before.astimezone(timezone.utc))
        assert result == not_before.astimezone(timezone.utc)

    def test_end_time_boundary_is_exclusive(self):
        windows = [mkwindow(0, "09:00", "17:00"), mkwindow(1, "09:00", "17:00")]
        not_before = local(*MONDAY, 17, 0)
        result = resolve_next_send_time(windows, TZ, not_before.astimezone(timezone.utc))
        result_local = result.astimezone(ZoneInfo(TZ))
        assert result_local.weekday() == 1  # rolled to Tuesday, not treated as still-Monday
        assert result_local.hour == 9

    def test_empty_windows_raises(self):
        with pytest.raises(NoLegalSendWindowError):
            resolve_next_send_time([], TZ, local(*MONDAY, 9, 0).astimezone(timezone.utc))

    def test_timezone_conversion_across_utc_offset(self):
        # A window defined 09:00-17:00 America/Chicago; a UTC instant that
        # is 14:00 UTC is 09:00 Chicago (UTC-5 in September, no DST here) --
        # should resolve as already-inside-window, unchanged.
        windows = [mkwindow(0, "09:00", "17:00")]
        utc_instant = datetime(2026, 9, 7, 14, 0, tzinfo=timezone.utc)
        result = resolve_next_send_time(windows, TZ, utc_instant)
        assert result == utc_instant


class TestIsWithinWindow:
    def test_inside_window_is_true(self):
        windows = [mkwindow(0, "09:00", "17:00")]
        assert is_within_window(windows, TZ, local(*MONDAY, 12, 0).astimezone(timezone.utc)) is True

    def test_before_window_is_false(self):
        windows = [mkwindow(0, "09:00", "17:00")]
        assert is_within_window(windows, TZ, local(*MONDAY, 8, 0).astimezone(timezone.utc)) is False

    def test_after_window_is_false(self):
        windows = [mkwindow(0, "09:00", "17:00")]
        assert is_within_window(windows, TZ, local(*MONDAY, 18, 0).astimezone(timezone.utc)) is False

    def test_start_boundary_inclusive_end_boundary_exclusive(self):
        windows = [mkwindow(0, "09:00", "17:00")]
        assert is_within_window(windows, TZ, local(*MONDAY, 9, 0).astimezone(timezone.utc)) is True
        assert is_within_window(windows, TZ, local(*MONDAY, 17, 0).astimezone(timezone.utc)) is False

    def test_wrong_weekday_is_false_even_at_the_same_time_of_day(self):
        windows = [mkwindow(0, "09:00", "17:00")]  # Monday only
        tuesday = local(*MONDAY, 12, 0) + timedelta(days=1)
        assert is_within_window(windows, TZ, tuesday.astimezone(timezone.utc)) is False


class TestComputeEligibleAt:
    def test_preserves_wall_clock_time_across_calendar_days(self):
        sent_at = local(*MONDAY, 9, 3).astimezone(timezone.utc)
        result = compute_eligible_at(sent_at, 2, TZ)
        result_local = result.astimezone(ZoneInfo(TZ))
        assert result_local.date() == local(*MONDAY, 9, 3).date() + timedelta(days=2)
        assert result_local.hour == 9 and result_local.minute == 3

    def test_zero_delay_returns_the_same_wall_clock_moment(self):
        sent_at = local(*MONDAY, 9, 3).astimezone(timezone.utc)
        result = compute_eligible_at(sent_at, 0, TZ)
        assert result.astimezone(ZoneInfo(TZ)) == local(*MONDAY, 9, 3)

    def test_spans_a_dst_spring_forward_transition_preserving_wall_clock(self):
        # US Central DST begins 2027-03-14 (2:00am -> 3:00am). A send at
        # 9:03am on 2027-03-12 with a 3-day delay lands on 2027-03-15,
        # after the transition -- wall-clock hour must still read 9:03,
        # not drift by the hour the UTC offset changed by.
        sent_at = local(2027, 3, 12, 9, 3).astimezone(timezone.utc)
        result = compute_eligible_at(sent_at, 3, TZ)
        result_local = result.astimezone(ZoneInfo(TZ))
        assert result_local.date() == local(2027, 3, 12, 9, 3).date() + timedelta(days=3)
        assert result_local.hour == 9 and result_local.minute == 3

    def test_spans_a_dst_fall_back_transition_preserving_wall_clock(self):
        # US Central DST ends 2026-11-01. A send before it, with a delay
        # landing after, must still read the same local wall-clock time.
        sent_at = local(2026, 10, 30, 9, 3).astimezone(timezone.utc)
        result = compute_eligible_at(sent_at, 3, TZ)
        result_local = result.astimezone(ZoneInfo(TZ))
        assert result_local.date() == local(2026, 10, 30, 9, 3).date() + timedelta(days=3)
        assert result_local.hour == 9 and result_local.minute == 3


def test_eager_eligible_at_then_resolver_lands_inside_a_real_window():
    """The two functions compose the way MailSendingService actually uses
    them: compute_eligible_at() gives a floor, resolve_next_send_time()
    projects it into the next real window -- landing outside any window is
    the ORDINARY case, not an edge case."""
    windows = [mkwindow(2, "09:00", "17:00")]  # Wednesday only
    sent_at = local(*MONDAY, 9, 0).astimezone(timezone.utc)  # Monday
    eligible_at = compute_eligible_at(sent_at, 1, TZ)  # floor: Tuesday 9:00, no window that day
    result = resolve_next_send_time(windows, TZ, eligible_at)
    result_local = result.astimezone(ZoneInfo(TZ))
    assert result_local.weekday() == 2  # pushed forward to Wednesday
    assert result_local.hour == 9
