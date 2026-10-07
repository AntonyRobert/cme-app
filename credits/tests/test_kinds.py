"""
Teaching and attendance credit: tracked separately, never double-counted.

The event: three one-hour talks, 0-60, 60-120 and 120-180 minutes.
"""
from decimal import Decimal

import pytest

from attendance.tests.factories import teams_row
from credits.rules import credit_breakdown
from credits.windows import sessions_needing_evaluation
from people.models import Person
from people.tests.factories import make_person
from rounds.models import SessionPresenter
from rounds.tests.factories import make_event, make_session

from .factories import adjust, evaluate

pytestmark = pytest.mark.django_db

D = Decimal


@pytest.fixture
def event():
    event = make_event(minutes=180, credits="3.00", sessions=0)
    for _ in range(3):
        make_session(event, minutes=60)
    return event


@pytest.fixture
def person():
    return make_person()


def talks(event):
    return list(event.sessions.order_by("start_at"))


def present(person, session):
    return SessionPresenter.objects.create(session=session, person=person)


def kinds(person, event):
    b = credit_breakdown(person, event)
    return b.attendance_credits, b.teaching_credits, b.total_credits


def test_presenting_one_talk_and_attending_two_is_one_teaching_and_two_attendance(event, person):
    first, second, third = talks(event)
    present(person, second)
    teams_row(event, person, 0, 180)
    evaluate(person, first)
    evaluate(person, third)
    assert kinds(person, event) == (D("2.00"), D("1.00"), D("3.00"))
    b = credit_breakdown(person, event)
    assert b.sessions_presented == [second]
    assert b.sessions_attended == [first, third]


def test_their_own_talk_is_never_also_attendance(event, person):
    """Connected all three hours and even evaluated their own talk: still 2 + 1, not 3 + 1."""
    first, second, third = talks(event)
    present(person, first)
    teams_row(event, person, 0, 180)
    for talk in (first, second, third):
        evaluate(person, talk)
    b = credit_breakdown(person, event)
    assert [s.credited_minutes for s in b.sessions] == [0, 60, 60]
    assert [s.teaching_minutes for s in b.sessions] == [60, 0, 0]
    assert kinds(person, event) == (D("2.00"), D("1.00"), D("3.00"))


def test_teaching_is_the_session_length_not_teams_minutes(event, person):
    first = talks(event)[0]
    present(person, first)
    teams_row(event, person, 40, 60)  # Teams caught 20 minutes of their own talk
    assert credit_breakdown(person, event).teaching_credits == D("1.00")


def test_a_presenter_teams_never_saw_still_earns_teaching(event, person):
    """A guest who presented from the room, with no Teams row at all."""
    present(person, talks(event)[0])
    assert kinds(person, event) == (D("0.00"), D("1.00"), D("1.00"))


def test_teaching_needs_no_evaluation(event, person):
    present(person, talks(event)[1])
    teams_row(event, person, 60, 120)
    b = credit_breakdown(person, event)
    assert b.sessions_evaluated == []
    assert b.teaching_credits == D("1.00")


def test_a_presenter_is_not_asked_to_evaluate_their_own_talk(event, person):
    first, second, third = talks(event)
    present(person, first)
    teams_row(event, person, 0, 180)
    assert [s for s, _ in sessions_needing_evaluation(person, event)] == [second, third]


def test_presenting_raises_no_review_flags_on_their_own_talk(event, person):
    first = talks(event)[0]
    present(person, first)
    teams_row(event, person, 50, 60)
    evaluate(person, first, minutes=60)  # would be "claims more than recorded" for an attendee
    assert credit_breakdown(person, event).needs_review is False


def test_co_presenters_each_earn_the_whole_session(event):
    first = talks(event)[0]
    a, b = make_person(), make_person()
    present(a, first)
    present(b, first)
    assert credit_breakdown(a, event).teaching_credits == D("1.00")
    assert credit_breakdown(b, event).teaching_credits == D("1.00")


def test_a_longer_talk_earns_more_teaching(person):
    event = make_event(minutes=90, credits="1.50", sessions=0)
    talk = make_session(event, minutes=90)
    present(person, talk)
    assert credit_breakdown(person, event).teaching_credits == D("1.50")


def test_rates_are_a_setting_per_kind(event, person, settings):
    settings.CREDIT_RATES_PER_HOUR = {"attendance": "1.0", "teaching": "2.0"}
    first, second, _ = talks(event)
    present(person, first)
    teams_row(event, person, 60, 120)
    evaluate(person, second)
    assert kinds(person, event) == (D("1.00"), D("2.00"), D("3.00"))


def test_the_accreditation_cap_applies_to_attendance_only(event, person):
    event.accredited_credits = D("1.00")
    event.save()
    first, second, third = talks(event)
    present(person, first)
    teams_row(event, person, 0, 180)
    evaluate(person, second)
    evaluate(person, third)
    assert kinds(person, event) == (D("1.00"), D("1.00"), D("2.00"))


def test_adjustments_change_only_their_own_kind(event, person):
    present(person, talks(event)[0])
    adjust(person, event, "0.50", reason="Prepared the case material", kind="teaching")
    adjust(person, event, "0.25", reason="Goodwill", kind="attendance")
    assert kinds(person, event) == (D("0.25"), D("1.50"), D("1.75"))


def test_an_organizer_earns_attendance_like_anyone_and_still_needs_evaluations(event, person):
    first, second, _ = talks(event)
    teams_row(event, person, 0, 180, raw_participant_role="Organizer")
    assert kinds(person, event) == (D("0.00"), D("0.00"), D("0.00"))
    evaluate(person, first)
    evaluate(person, second)
    assert kinds(person, event) == (D("2.00"), D("0.00"), D("2.00"))


def test_presenting_follows_a_merge(event):
    survivor, duplicate = make_person(), make_person()
    present(duplicate, talks(event)[0])
    Person.objects.filter(pk=duplicate.pk).update(merged_into=survivor)
    assert credit_breakdown(survivor, event).teaching_credits == D("1.00")
