"""
attended_minutes(): the piece most likely to be subtly wrong.

Times in these tests are minutes after the scheduled start of a 60-minute
event, so the credit window is -5 to 60.
"""
import datetime

import pytest

from attendance.aggregation import MinutesSource, attended_minutes, merged_seconds
from attendance.models import AttendanceRecord
from people.models import Person
from people.tests.factories import make_person
from rounds.tests.factories import at, make_event

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


def test_time_between_rejoins_is_not_attended(event, person):
    teams_row(event, person, 0, 10)
    teams_row(event, person, 20, 30)
    teams_row(event, person, 45, 60)
    assert minutes(person, event) == 35


def test_laptop_and_phone_at_once_are_not_counted_twice(event, person):
    teams_row(event, person, 0, 60, raw_display_name="laptop")
    teams_row(event, person, 10, 50, raw_display_name="phone")
    assert minutes(person, event) == 60


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


def test_waiting_before_the_start_only_counts_for_the_grace_period(event, person):
    teams_row(event, person, -20, 30)
    assert minutes(person, event) == 35  # 5 minutes of grace + 30


def test_staying_after_the_end_does_not_count(event, person):
    teams_row(event, person, 30, 90)
    assert minutes(person, event) == 30


def test_a_row_entirely_outside_the_window_counts_for_nothing(event, person):
    teams_row(event, person, -30, -10)
    teams_row(event, person, 70, 80)
    result = attended_minutes(person, event)
    assert result.minutes == 0
    assert result.has_rows  # still not a self-report case


def test_actual_end_time_extends_the_window_for_everyone(event, person):
    teams_row(event, person, 0, 80)
    assert minutes(person, event) == 60
    event.actual_end_at = at(80)
    event.save()
    assert minutes(person, event) == 80


def test_actual_start_time_moves_the_window(event, person):
    teams_row(event, person, 0, 60)
    event.actual_start_at = at(15)
    event.save()
    assert minutes(person, event) == 50  # from 10 (grace) to 60


# --- Superseded rows ---------------------------------------------------------


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


def test_total_is_capped_at_the_window_length(event, person):
    teams_row(event, person, -5, 60)
    manual_row(event, person, minutes=40)
    assert minutes(person, event) == 65  # the window: 5 grace + 60


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
    device = teams_row(event, None, 0, 40, raw_display_name="Conference Room B")
    roster_row(device, person)
    teams_row(event, person, 30, 60)  # went back to their office and joined
    result = attended_minutes(person, event)
    assert (result.minutes, result.source) == (60, MinutesSource.MIXED)


def test_room_roster_row_is_clamped_like_any_other(event, person):
    device = teams_row(event, None, -30, 75, raw_display_name="Conference Room B")
    roster_row(device, person)
    assert minutes(person, event) == 65


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
