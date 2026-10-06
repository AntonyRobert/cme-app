"""
The seed data doubles as an end-to-end check: every figure below comes out
of the real aggregation and credit rules.
"""
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from attendance.aggregation import MinutesSource
from attendance.models import AttendanceRecord
from attendance.services import match_record
from audit.models import AuditLog
from credits.rules import credit_breakdown
from people.merge import merge_people
from people.models import Person
from people.tests.factories import make_staff
from rounds.models import RoundsEvent, SessionPresenter

pytestmark = pytest.mark.django_db

D = Decimal


@pytest.fixture
def seeded(settings, tmp_path):
    settings.UPLOAD_ROOT = tmp_path
    call_command("seed_demo", verbosity=0, allow_non_debug=True)
    first, second = RoundsEvent.objects.order_by("date")
    return first, second


def who(family, **extra):
    return Person.objects.filter(family_name=family, **extra).order_by("created_at").first()


def summary(family, event):
    b = credit_breakdown(who(family), event)
    return b.time.minutes, b.time.source, b.gate_passed, b.credits, b.time.needs_review


def test_seed_creates_what_the_admin_needs(seeded):
    first, second = seeded
    assert Person.objects.count() == 12
    assert first.sessions.count() == second.sessions.count() == 3
    assert AttendanceRecord.objects.unmatched().count() == 6
    assert AttendanceRecord.objects.filter(source="room_roster").count() == 2
    assert AttendanceRecord.objects.superseded().count() == 2
    assert SessionPresenter.objects.filter(coi_declaration__isnull=True).count() == 1
    assert AuditLog.objects.filter(action="attendance.manual_row_created").count() == 4


def test_seed_refuses_a_database_that_already_has_data(seeded):
    with pytest.raises(CommandError):
        call_command("seed_demo", verbosity=0, allow_non_debug=True)


@pytest.mark.parametrize(
    "family, expected",
    [
        ("Tremblay", (62, MinutesSource.TEAMS, True, D("1.00"), False)),  # one connection
        ("Côté", (57, MinutesSource.TEAMS, True, D("0.75"), False)),  # three rejoins, rounds down
        ("Haddad", (60, MinutesSource.TEAMS, True, D("1.00"), False)),  # laptop + phone overlap
        ("Nguyen", (36, MinutesSource.TEAMS, True, D("0.50"), False)),  # lobby time clamped
        ("Okafor", (55, MinutesSource.TEAMS, False, D("0.75"), False)),  # gate fails; adjustment
        ("Lavoie", (58, MinutesSource.MANUAL, True, D("0.75"), False)),  # room roster
        ("Roy", (34, MinutesSource.MANUAL, False, D("0.00"), False)),  # late, no evaluation
        ("Sharma", (55, MinutesSource.MANUAL, True, D("0.75"), False)),  # superseded rows
        ("Morin", (60, MinutesSource.SELF_REPORTED, True, D("1.00"), True)),  # self-report only
    ],
)
def test_first_event_credit(seeded, family, expected):
    assert summary(family, seeded[0]) == expected


@pytest.mark.parametrize(
    "family, expected",
    [
        ("Côté", (70, MinutesSource.TEAMS, True, D("1.00"), False)),  # overrun, capped
        ("Nguyen", (65, MinutesSource.TEAMS, True, D("1.00"), False)),
        ("Okafor", (50, MinutesSource.MIXED, True, D("0.75"), False)),  # Teams + hours-only row
        ("Lavoie", (30, MinutesSource.TEAMS, True, D("0.50"), True)),  # claims 60
    ],
)
def test_second_event_credit(seeded, family, expected):
    assert summary(family, seeded[1]) == expected


def test_without_the_overrun_the_second_event_would_stop_at_60_minutes(seeded):
    second = seeded[1]
    second.actual_end_at = None
    second.save()
    assert summary("Côté", second)[0] == 60  # he joined at noon, so no grace minutes


def test_merging_the_duplicate_brings_minutes_and_evaluation_together(seeded):
    second = seeded[1]
    main, duplicate = Person.objects.filter(family_name="Tremblay").order_by("created_at")
    before = credit_breakdown(main, second)
    assert (before.time.source, before.time.needs_review) == (MinutesSource.SELF_REPORTED, True)
    assert credit_breakdown(duplicate, second).credits == D("0.00")

    merge_people(main, duplicate, user=make_staff())

    after = credit_breakdown(main, second)
    assert (after.time.minutes, after.time.source) == (70, MinutesSource.TEAMS)
    assert (after.credits, after.time.needs_review) == (D("1.00"), False)


def test_matching_one_unmatched_rejoin_row_clears_all_three(seeded):
    second = seeded[1]
    bouchard = who("Bouchard")
    assert credit_breakdown(bouchard, second).time.minutes == 0
    row = AttendanceRecord.objects.unmatched().filter(event=second).first()
    result = match_record(row, bouchard, user=make_staff())
    assert result.also_matched == 2
    assert result.email_added == "lea.bouchard@hospital.example"
    assert credit_breakdown(bouchard, second).time.minutes == 66  # 22 + 23 + 21, gaps excluded
