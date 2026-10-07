"""
The seed data doubles as an end-to-end check: every figure below comes out
of the real aggregation and credit rules.

Both seeded events run noon to three with three one-hour sessions; the
second event's third session ran five minutes over.
"""
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from attendance.aggregation import MinutesSource
from attendance.models import AttendanceRecord
from attendance.services import match_record
from audit.models import AuditLog
from credits.models import EvaluationWindow
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
    """(minutes per session, source, credited minutes, credits, needs review)"""
    b = credit_breakdown(who(family), event)
    return [s.minutes for s in b.sessions], b.source, b.credited_minutes, b.credits, b.needs_review


def test_seed_creates_what_the_admin_needs(seeded):
    first, second = seeded
    assert Person.objects.count() == 12
    assert first.sessions.count() == second.sessions.count() == 3
    assert AttendanceRecord.objects.unmatched().count() == 6
    assert AttendanceRecord.objects.filter(source="room_roster").count() == 2
    assert AttendanceRecord.objects.superseded().count() == 2
    assert SessionPresenter.objects.filter(coi_declaration__isnull=True).count() == 1
    assert AuditLog.objects.filter(action="attendance.manual_row_created").count() == 4
    assert EvaluationWindow.objects.count() == 1


def test_seed_refuses_a_database_that_already_has_data(seeded):
    with pytest.raises(CommandError):
        call_command("seed_demo", verbosity=0, allow_non_debug=True)


@pytest.mark.parametrize(
    "family, expected",
    [
        # one connection, every session evaluated
        ("Tremblay", ([60, 60, 60], MinutesSource.TEAMS, 180, D("3.00"), False)),
        # three rejoins; session 1 is 54 minutes, one short of counting in full
        ("Côté", ([54, 60, 57], MinutesSource.TEAMS, 54, D("0.75"), False)),
        # laptop and phone at once, counted once; two sessions evaluated
        ("Haddad", ([60, 60, 60], MinutesSource.TEAMS, 120, D("2.00"), False)),
        # lobby time clamped to five minutes; left during session 2
        ("Nguyen", ([60, 33, 0], MinutesSource.TEAMS, 60, D("1.00"), False)),
        # joined a quarter past; only an incomplete evaluation; an adjustment
        ("Okafor", ([45, 60, 60], MinutesSource.TEAMS, 0, D("0.75"), False)),
        # room roster: the device's 12:03-14:57, 57 minutes counting as the hour
        ("Lavoie", ([57, 60, 57], MinutesSource.MANUAL, 60, D("1.00"), False)),
        # arrived 13:15, no evaluation
        ("Roy", ([0, 45, 57], MinutesSource.MANUAL, 0, D("0.00"), False)),
        # superseded laptop rows; a 55-minute manual row for the talk she gave
        ("Sharma", ([0, 55, 60], MinutesSource.MIXED, 60, D("1.00"), False)),
        # no attendance row at all: self-report, flagged
        ("Morin", ([60, 60, 60], MinutesSource.SELF_REPORTED, 180, D("3.00"), True)),
    ],
)
def test_first_event_credit(seeded, family, expected):
    assert summary(family, seeded[0]) == expected


@pytest.mark.parametrize(
    "family, expected",
    [
        # stayed for the overrun; session 3 is 65 minutes
        ("Côté", ([60, 60, 65], MinutesSource.TEAMS, 60, D("1.00"), False)),
        # evaluated the 65-minute session: 65 minutes rounds down to 1.00
        ("Gagnon", ([60, 60, 65], MinutesSource.TEAMS, 65, D("1.00"), False)),
        ("Nguyen", ([60, 60, 50], MinutesSource.TEAMS, 60, D("1.00"), False)),
        # Teams for 20 minutes, then an hours-only row for the same session
        ("Okafor", ([60, 0, 0], MinutesSource.MIXED, 60, D("1.00"), False)),
        # recorded 30, claims 60
        ("Lavoie", ([30, 0, 0], MinutesSource.TEAMS, 30, D("0.50"), True)),
    ],
)
def test_second_event_credit(seeded, family, expected):
    assert summary(family, seeded[1]) == expected


def test_the_overrun_is_recorded_on_the_session(seeded):
    second = seeded[1]
    third = second.sessions.order_by("start_at").last()
    assert third.length_minutes == 65
    third.end_at = third.start_at + (third.end_at - third.start_at) * 60 // 65
    third.save()
    assert summary("Gagnon", second)[0] == [60, 60, 60]


def test_a_merge_brings_minutes_and_evaluation_together(seeded):
    second = seeded[1]
    main, duplicate = Person.objects.filter(family_name="Tremblay").order_by("created_at")
    before = credit_breakdown(main, second)
    assert (before.source, before.needs_review) == (MinutesSource.SELF_REPORTED, True)
    assert credit_breakdown(duplicate, second).credits == D("0.00")

    merge_people(main, duplicate, user=make_staff())

    after = credit_breakdown(main, second)
    assert ([s.minutes for s in after.sessions], after.source) == ([60, 60, 65], MinutesSource.TEAMS)
    assert (after.credits, after.needs_review) == (D("1.00"), False)


def test_matching_one_unmatched_rejoin_row_clears_all_three(seeded):
    second = seeded[1]
    bouchard = who("Bouchard")
    assert credit_breakdown(bouchard, second).minutes == 0
    row = AttendanceRecord.objects.unmatched().filter(event=second).first()
    result = match_record(row, bouchard, user=make_staff())
    assert result.also_matched == 2
    assert result.email_added == "lea.bouchard@hospital.example"
    b = credit_breakdown(bouchard, second)
    assert [s.minutes for s in b.sessions] == [60, 54, 64]  # gaps between rejoins excluded
