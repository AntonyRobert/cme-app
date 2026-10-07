"""
attended_minutes(): the piece most likely to be subtly wrong.

Times in these tests are minutes after the scheduled start of a 60-minute
event with one session spanning it. Time counts from -5 to 65 (five minutes
of grace at each end), and a session's total can never exceed its own
length. The multi-session cases are further down.
"""
import logging

import datetime

import pytest

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from attendance.aggregation import (
    MinutesSource,
    attended_minutes,
    merged_seconds,
    sessions_attended,
)
from attendance.models import AttendanceRecord
from people.models import Person
from people.tests.factories import make_person, make_staff
from rounds.tests.factories import at, make_event, make_session

from .factories import manual_row, roster_row, supersede, teams_row

pytestmark = pytest.mark.django_db


@pytest.fixture
def event():
    return make_event()


@pytest.fixture
def person():
    return make_person()


def minutes(person, event):
    return attended_minutes(person, event).minutes


# --- The interval arithmetic on its own --------------------------------------


@pytest.mark.parametrize(
    "spans, expected_minutes",
    [
        ([], 0),
        ([(0, 60)], 60),
        ([(0, 15), (20, 35), (40, 60)], 50),  # gaps are not attended
        ([(0, 40), (30, 60)], 60),  # overlap counted once
        ([(0, 30), (30, 60)], 60),  # touching
        ([(0, 60), (10, 20)], 60),  # one inside another
        ([(30, 60), (0, 40)], 60),  # order doesn't matter
        ([(0, 30), (0, 30), (0, 30)], 30),  # identical rows
        ([(0, 10), (5, 15), (12, 30), (50, 60)], 40),  # a chain of overlaps
        ([(10, 10)], 0),  # empty
        ([(20, 10)], 0),  # backwards, as clamping can produce
    ],
)
def test_merged_seconds(spans, expected_minutes):
    intervals = [(at(start), at(end)) for start, end in spans]
    assert merged_seconds(intervals) == expected_minutes * 60


# --- Rejoins -----------------------------------------------------------------


def test_one_row_for_the_whole_event(event, person):
    teams_row(event, person, 0, 60)
    result = attended_minutes(person, event)
    assert (result.minutes, result.source, result.row_count) == (60, MinutesSource.TEAMS, 1)


def test_rejoin_rows_are_added_together(event, person):
    for start in (0, 15, 30, 45):
        teams_row(event, person, start, start + 15)
    assert minutes(person, event) == 60


def test_the_gap_between_two_rows_is_not_counted(event, person):
    """
    0-20 and 30-50 is 40 minutes, not 50. An implementation that "merges"
    by taking earliest join to latest leave would count the gap.
    """
    teams_row(event, person, 0, 20)
    teams_row(event, person, 30, 50)
    assert minutes(person, event) == 40


def test_time_between_rejoins_is_not_attended(event, person):
    teams_row(event, person, 0, 10)
    teams_row(event, person, 20, 30)
    teams_row(event, person, 45, 60)
    assert minutes(person, event) == 35


def test_laptop_and_phone_at_once_are_not_counted_twice(event, person):
    """
    On a laptop for 40 minutes, with a phone also connected for 20 of them.
    That is 40 minutes of attendance, not 60.

    Deliberately shorter than the event: if the right answer were the full
    60 minutes, a naive sum would be capped to 60 and pass by accident.
    """
    teams_row(event, person, 0, 40, raw_display_name="laptop")
    teams_row(event, person, 10, 30, raw_display_name="phone")
    assert minutes(person, event) == 40


def test_laptop_and_phone_at_once_for_the_whole_event(event, person):
    teams_row(event, person, 0, 60, raw_display_name="laptop")
    teams_row(event, person, 10, 50, raw_display_name="phone")
    result = attended_minutes(person, event)
    assert result.minutes == 60
    assert result.capped_seconds == 0  # two connections are not a reason to warn


def test_partly_overlapping_connections(event, person):
    teams_row(event, person, 0, 35)
    teams_row(event, person, 25, 55)
    assert minutes(person, event) == 55


def test_recorded_duration_is_ignored_when_there_are_times(event, person):
    teams_row(event, person, 0, 30, duration_seconds=99999)
    assert minutes(person, event) == 30


def test_seconds_round_down_to_whole_minutes(event, person):
    row = teams_row(event, person, 0, 30)
    AttendanceRecord.objects.filter(pk=row.pk).update(
        leave_at=at(30) + datetime.timedelta(seconds=59)
    )
    assert attended_minutes(person, event).seconds == 30 * 60 + 59
    assert minutes(person, event) == 30


# --- The credit window -------------------------------------------------------


def test_joining_three_minutes_early_counts_from_when_they_joined(event, person):
    """Inside the five-minute grace, the early minutes count."""
    teams_row(event, person, -3, 50)
    assert minutes(person, event) == 53


def test_joining_exactly_at_the_grace_boundary(event, person):
    teams_row(event, person, -5, 50)
    assert minutes(person, event) == 55


def test_joining_twenty_minutes_early_does_not_earn_waiting_room_time(event, person):
    """Clamped to the start of the grace period: 5 early minutes, not 20."""
    teams_row(event, person, -20, 50)
    assert minutes(person, event) == 55


def test_waiting_before_the_start_only_counts_for_the_grace_period(event, person):
    teams_row(event, person, -20, 30)
    assert minutes(person, event) == 35  # 5 minutes of grace + 30


def test_grace_minutes_cannot_push_the_total_past_the_event_length(event, person):
    """Early by five and there to the end is the full hour, not 65 minutes."""
    teams_row(event, person, -5, 60)
    result = attended_minutes(person, event)
    assert result.minutes == 60
    assert result.capped_seconds == 0  # ordinary, not worth a warning


def test_staying_after_the_end_counts_for_the_grace_period_only(event, person):
    """Symmetric with the start: five minutes after the last session, not more."""
    teams_row(event, person, 30, 90)
    assert minutes(person, event) == 35


def test_a_row_entirely_outside_the_window_counts_for_nothing(event, person):
    teams_row(event, person, -30, -10)
    teams_row(event, person, 70, 80)
    result = attended_minutes(person, event)
    assert result.minutes == 0
    assert result.has_rows  # still not a self-report case


def test_a_session_running_over_is_fixed_once_on_its_end_time(event, person):
    teams_row(event, person, 0, 80)
    assert minutes(person, event) == 60
    session = event.sessions.get()
    session.end_at = at(80)
    session.save()
    assert minutes(person, event) == 80


def test_time_past_the_session_end_is_cut_off_there(event, person):
    """The talk ran to 80 minutes; they stayed connected until 95."""
    session = event.sessions.get()
    session.end_at = at(80)
    session.save()
    teams_row(event, person, 0, 95)
    assert minutes(person, event) == 80


def test_time_past_an_early_end_is_cut_off_too(event, person):
    """The talk ended at 50 minutes; Teams kept the room open to the hour."""
    session = event.sessions.get()
    session.end_at = at(50)
    session.save()
    teams_row(event, person, 0, 60)
    assert minutes(person, event) == 50


def test_laptop_and_phone_both_running_past_the_end(event, person):
    """
    Pins that each interval is clamped to the window AND the intervals are
    merged, rather than the total merely being capped afterwards.

    Joined 20 minutes late on a laptop (20-90) and a phone (30-85); the
    talk ended at 70, so time counts to 75 (five minutes of grace). The
    right answer is 55: 20 to 75, once.

    The late join is what makes this test discriminate. Each shortcut gives
    a different wrong number:
      - merge without clamping:            70  (20 to 90, capped at 70)
      - clamp, then sum instead of merge:  70  (55 + 45 = 100, capped at 70)
      - sum raw rows, then cap:            70
    If they had joined at 0, the right answer would also be 70 and every
    one of those would pass for the wrong reason.
    """
    session = event.sessions.get()
    session.end_at = at(70)
    session.save()
    teams_row(event, person, 20, 90, raw_display_name="laptop")
    teams_row(event, person, 30, 85, raw_display_name="phone")
    assert minutes(person, event) == 55


def test_a_late_start_shortens_the_session(event, person):
    teams_row(event, person, 0, 60)
    session = event.sessions.get()
    session.start_at = at(15)
    session.save()
    # Present from 10 (the grace before the late start) to 60, but the talk
    # itself only ran 45 minutes.
    assert minutes(person, event) == 45


# --- Several sessions --------------------------------------------------------


@pytest.fixture
def three_sessions():
    """0-60, 70-130 and 140-200, with ten-minute breaks between."""
    event = make_event(minutes=200, credits="3.00", sessions=0)
    for start in (0, 70, 140):
        make_session(event, start=start, minutes=60)
    return event


def per_session(person, event):
    return [s.minutes for s in attended_minutes(person, event).sessions]


def test_a_break_between_sessions_does_not_count(three_sessions, person):
    """Connected from start to finish: 200 minutes online, 180 attended."""
    teams_row(three_sessions, person, 0, 200)
    result = attended_minutes(person, three_sessions)
    assert per_session(person, three_sessions) == [60, 60, 60]
    assert result.minutes == 180


def test_time_spent_only_in_a_break_counts_for_nothing(three_sessions, person):
    teams_row(three_sessions, person, 61, 69)
    assert per_session(person, three_sessions) == [0, 0, 0]


def test_attending_only_the_middle_session(three_sessions, person):
    teams_row(three_sessions, person, 65, 135)  # joined in the break, left in the break
    assert per_session(person, three_sessions) == [0, 60, 0]
    assert attended_minutes(person, three_sessions).minutes == 60


def test_grace_applies_at_the_ends_of_the_event_not_around_each_session(three_sessions, person):
    # Five minutes early for the first talk counts; five minutes early for
    # the second is break time and does not.
    teams_row(three_sessions, person, -5, 55)
    teams_row(three_sessions, person, 65, 125)
    assert per_session(person, three_sessions) == [60, 55, 0]
    # Likewise after the last talk, but not after the first.
    other = make_person()
    teams_row(three_sessions, other, 5, 65)
    teams_row(three_sessions, other, 145, 205)
    assert per_session(other, three_sessions) == [55, 0, 60]


def test_a_row_spanning_two_sessions_is_split_between_them(three_sessions, person):
    teams_row(three_sessions, person, 30, 100)
    assert per_session(person, three_sessions) == [30, 30, 0]


def test_sessions_attended_is_half_or_more_of_each(three_sessions, person):
    teams_row(three_sessions, person, 0, 60)
    teams_row(three_sessions, person, 70, 100)  # exactly half of the second
    teams_row(three_sessions, person, 140, 169)  # 29 of the third
    result = attended_minutes(person, three_sessions)
    assert [s.attended for s in result.sessions] == [True, True, False]
    assert sessions_attended(person, three_sessions) == list(
        three_sessions.sessions.order_by("start_at")
    )[:2]


def test_an_hours_only_row_belongs_to_the_session_it_names(three_sessions, person):
    first, second, third = three_sessions.sessions.order_by("start_at")
    manual_row(three_sessions, person, minutes=45, session=second)
    assert per_session(person, three_sessions) == [0, 45, 0]


def test_an_hours_only_row_needs_a_session(three_sessions, person):
    with pytest.raises(IntegrityError), transaction.atomic():
        manual_row(three_sessions, person, minutes=45, session=None)
    row = AttendanceRecord(
        source=AttendanceRecord.Source.MANUAL,
        event=three_sessions,
        person=person,
        duration_seconds=45 * 60,
        reason="x",
        created_by=make_staff(),
    )
    with pytest.raises(ValidationError) as err:
        row.full_clean()
    assert "session" in err.value.message_dict


def test_a_timed_row_may_not_name_a_session(three_sessions, person):
    first = three_sessions.sessions.order_by("start_at").first()
    row = AttendanceRecord(
        source=AttendanceRecord.Source.MANUAL,
        event=three_sessions,
        person=person,
        join_at=at(0),
        leave_at=at(30),
        session=first,
        reason="x",
        created_by=make_staff(),
    )
    with pytest.raises(ValidationError) as err:
        row.full_clean()
    assert "session" in err.value.message_dict


def test_an_event_without_sessions_counts_nothing(person):
    event = make_event(sessions=0)
    teams_row(event, person, 0, 60)
    result = attended_minutes(person, event)
    assert (result.minutes, result.has_rows, result.sessions) == (0, True, [])


# --- Superseded rows ---------------------------------------------------------


def test_a_row_with_a_supersession_pointing_at_it_does_not_count(event, person):
    """The replaced row contributes nothing; its replacement contributes everything."""
    replaced = teams_row(event, person, 0, 40)
    replacement = teams_row(event, person, 0, 25)
    assert minutes(person, event) == 40  # before: both active, merged
    supersede([replaced], replacement)
    assert replaced.supersession.new == replacement
    assert minutes(person, event) == 25  # after: only the replacement


def test_a_superseded_row_is_skipped_and_its_correction_counts(event, person):
    wrong = teams_row(event, person, 0, 20)
    right = manual_row(event, person, start=0, end=55, reason="Teams dropped the rest")
    supersede([wrong], right)
    result = attended_minutes(person, event)
    assert result.minutes == 55
    assert result.source == MinutesSource.MANUAL  # no active Teams row is left


def test_one_correction_can_replace_several_rejoin_rows(event, person):
    rejoins = [teams_row(event, person, start, start + 10) for start in (0, 15, 30, 45)]
    assert minutes(person, event) == 40
    correction = manual_row(event, person, minutes=58, reason="Audio only, chair confirms")
    supersede(rejoins, correction)
    assert minutes(person, event) == 58


def test_only_some_rows_superseded(event, person):
    kept = teams_row(event, person, 0, 20)
    wrong = teams_row(event, person, 20, 25)
    right = manual_row(event, person, start=20, end=60)
    supersede([wrong], right)
    assert kept.is_superseded is False
    result = attended_minutes(person, event)
    assert (result.minutes, result.source) == (60, MinutesSource.MIXED)


def test_a_correction_of_a_correction(event, person):
    first = teams_row(event, person, 0, 10)
    second = manual_row(event, person, minutes=30)
    third = manual_row(event, person, minutes=45)
    supersede([first], second)
    supersede([second], third)
    assert minutes(person, event) == 45


def test_superseding_with_a_zero_row_removes_the_attendance(event, person):
    mistaken = teams_row(event, person, 0, 60)
    void = manual_row(event, person, minutes=0, reason="Wrong person: shared login")
    supersede([mistaken], void)
    result = attended_minutes(person, event)
    assert result.minutes == 0
    assert result.has_rows


# --- Manual rows -------------------------------------------------------------


def test_hours_only_manual_row_alone(event, person):
    manual_row(event, person, minutes=45)
    result = attended_minutes(person, event)
    assert (result.minutes, result.source) == (45, MinutesSource.MANUAL)


def test_hours_only_row_adds_on_top_of_timed_rows(event, person):
    teams_row(event, person, 0, 20)
    manual_row(event, person, minutes=25, reason="Phoned in after laptop died")
    result = attended_minutes(person, event)
    assert (result.minutes, result.source) == (45, MinutesSource.MIXED)


def test_hours_only_row_adds_on_top_of_merged_teams_intervals(event, person):
    """
    The path most likely to be "simplified" into a plain sum one day.

    Laptop 0-25 and phone 10-30 overlap: 30 minutes once merged, not 45.
    The hours-only manual row has no interval to merge, so its 20 minutes
    are added on top: 50. A naive sum of all three rows would say 65.
    """
    teams_row(event, person, 0, 25, raw_display_name="laptop")
    teams_row(event, person, 10, 30, raw_display_name="phone")
    manual_row(event, person, minutes=20, reason="Phoned in for the last 20 minutes")
    result = attended_minutes(person, event)
    assert (result.minutes, result.source) == (50, MinutesSource.MIXED)
    assert result.capped_seconds == 0


def test_two_hours_only_rows_are_both_added(event, person):
    manual_row(event, person, minutes=15)
    manual_row(event, person, minutes=20)
    assert minutes(person, event) == 35


def test_total_is_capped_at_the_session_length_and_the_cap_is_logged(person, caplog):
    """
    70 minutes on Teams in a 70-minute talk, plus a 30-minute manual row,
    is 70 minutes, not 100. Nobody attends a talk for longer than it ran,
    and this is the figure a certificate prints. The cap biting usually
    means a duplicate manual row, so it is logged.
    """
    event = make_event(minutes=70, credits="1.00")
    teams_row(event, person, 0, 70)
    manual_row(event, person, minutes=30, reason="Sat in Room B (entered twice by mistake)")
    with caplog.at_level(logging.WARNING, logger="attendance.aggregation"):
        result = attended_minutes(person, event)
    assert result.minutes == 70
    assert result.capped_seconds == 30 * 60
    (warning,) = caplog.records
    assert "capped at the session length" in warning.getMessage()
    assert str(person.pk) in warning.getMessage()
    assert str(event.sessions.get().pk) in warning.getMessage()


def test_no_warning_when_the_total_fits(event, person, caplog):
    teams_row(event, person, 0, 30)
    manual_row(event, person, minutes=30)
    with caplog.at_level(logging.WARNING, logger="attendance.aggregation"):
        result = attended_minutes(person, event)
    assert (result.minutes, result.capped_seconds) == (60, 0)
    assert caplog.records == []


def test_hours_only_rows_alone_are_capped_too(event, person, caplog):
    manual_row(event, person, minutes=45)
    manual_row(event, person, minutes=45)
    with caplog.at_level(logging.WARNING, logger="attendance.aggregation"):
        result = attended_minutes(person, event)
    assert (result.minutes, result.capped_seconds) == (60, 30 * 60)
    assert len(caplog.records) == 1


def test_timed_manual_row_merges_like_any_other(event, person):
    teams_row(event, person, 0, 30)
    manual_row(event, person, start=20, end=50)
    assert minutes(person, event) == 50


# --- Room roster rows --------------------------------------------------------


def test_room_roster_row_copies_the_device_window(event):
    host, guest = make_person(), make_person()
    device = teams_row(event, host, 2, 58, raw_display_name="Conference Room B")
    row = roster_row(device, guest)
    assert (row.join_at, row.leave_at) == (device.join_at, device.leave_at)
    assert row.duration_seconds == 56 * 60
    assert row.event == event
    result = attended_minutes(guest, event)
    assert (result.minutes, result.source) == (56, MinutesSource.MANUAL)
    assert minutes(host, event) == 56  # the host's own row is unaffected


def test_room_roster_row_for_someone_who_walked_in_late(event):
    device = teams_row(event, None, 0, 60, raw_display_name="Conference Room B")
    late = make_person()
    roster_row(device, late, start=25, end=60, reason="Arrived 12:25 per sheet")
    assert minutes(late, event) == 35


def test_room_time_and_own_connection_are_not_double_counted(event, person):
    device = teams_row(event, None, 0, 30, raw_display_name="Conference Room B")
    roster_row(device, person)
    teams_row(event, person, 20, 50)  # went back to their office and joined
    result = attended_minutes(person, event)
    assert (result.minutes, result.source) == (50, MinutesSource.MIXED)  # not 30 + 30


def test_room_roster_row_is_clamped_like_any_other(event, person):
    device = teams_row(event, None, -30, 75, raw_display_name="Conference Room B")
    roster_row(device, person)
    assert minutes(person, event) == 60


def test_several_people_behind_one_device(event):
    device = teams_row(event, None, 0, 60, raw_display_name="Conference Room B")
    people = [make_person() for _ in range(3)]
    for guest in people:
        roster_row(device, guest)
    assert [minutes(guest, event) for guest in people] == [60, 60, 60]


# --- Whose rows --------------------------------------------------------------


def test_unmatched_rows_belong_to_nobody(event, person):
    teams_row(event, None, 0, 60, raw_display_name="iPhone")
    result = attended_minutes(person, event)
    assert (result.minutes, result.source, result.has_rows) == (0, None, False)


def test_other_people_and_other_events_do_not_leak_in(event, person):
    teams_row(event, person, 0, 20)
    teams_row(event, make_person(), 0, 60)
    teams_row(make_event(), person, 0, 60)
    assert minutes(person, event) == 20


def test_rows_left_on_a_merged_duplicate_still_count(event):
    survivor, duplicate = make_person(), make_person()
    teams_row(event, survivor, 0, 20)
    teams_row(event, duplicate, 10, 45)
    Person.objects.filter(pk=duplicate.pk).update(merged_into=survivor)
    duplicate.refresh_from_db()
    assert minutes(survivor, event) == 45
    assert minutes(duplicate, event) == 45
