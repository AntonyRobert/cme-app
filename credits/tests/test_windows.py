"""
When an evaluation may be submitted: the default week, reopenings, the
accreditation-year boundary.

The test event is on 2026-09-15 (a Tuesday) at noon Montreal time.
"""
import datetime
from zoneinfo import ZoneInfo

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from audit.models import AuditLog
from core.models import ImmutableRowError
from credits.models import EvaluationSubmission, EvaluationWindow
from credits.windows import (
    STATE_CAN_REQUEST,
    STATE_EVALUATED,
    STATE_LIMIT_REACHED,
    STATE_OPEN,
    STATE_PAST_YEAR_END,
    STATE_REOPENED,
    ReopeningRefused,
    accreditation_year_end,
    close_windows,
    default_window,
    request_reopening,
    sessions_needing_evaluation,
    submission_allowed,
    window_state,
)
from people.tests.factories import make_person, make_staff
from rounds.tests.factories import EVENT_START, make_event, make_session

from .factories import evaluate

pytestmark = pytest.mark.django_db

MONTREAL = ZoneInfo("America/Montreal")


def local(year, month, day, hour=12, minute=0):
    return datetime.datetime(year, month, day, hour, minute, tzinfo=MONTREAL)


@pytest.fixture
def session():
    return make_event().sessions.get()


@pytest.fixture
def person():
    return make_person()


@pytest.fixture
def clock(monkeypatch):
    """Set "now" for the windows module and Django's timezone."""

    def set_now(when):
        monkeypatch.setattr("django.utils.timezone.now", lambda: when)

    return set_now


# --- The default week --------------------------------------------------------


def test_default_window_runs_from_the_session_to_a_week_after_the_event_date(session):
    opens_at, closes_at = default_window(session)
    assert opens_at == session.start_at
    assert closes_at == local(2026, 9, 23, 0, 0)  # the night of the 22nd, a week on


def test_submission_inside_the_default_week(session, person):
    assert submission_allowed(person, session, at=local(2026, 9, 15, 13))  # right after
    assert submission_allowed(person, session, at=local(2026, 9, 22, 23, 59))  # last minute


def test_submission_outside_the_default_week(session, person):
    assert not submission_allowed(person, session, at=local(2026, 9, 15, 11))  # before it starts
    assert not submission_allowed(person, session, at=local(2026, 9, 23, 0, 0))  # a week and a day
    assert not submission_allowed(person, session, at=local(2026, 11, 1))


def test_the_window_length_is_a_setting(session, person, settings):
    settings.EVALUATION_WINDOW_DAYS = 14
    assert submission_allowed(person, session, at=local(2026, 9, 29))
    assert not submission_allowed(person, session, at=local(2026, 9, 30))


def test_the_model_refuses_a_submission_when_the_window_is_closed(session, person, clock):
    clock(local(2026, 10, 20))
    submission = EvaluationSubmission(
        person=person, session=session, self_reported_session_minutes=60, attestation=True
    )
    with pytest.raises(ValidationError) as err:
        submission.full_clean()
    assert "window" in str(err.value)
    clock(local(2026, 9, 16))
    submission.full_clean()


# --- Reopening ---------------------------------------------------------------


def test_a_request_is_granted_for_another_week_and_logged(session, person, clock):
    clock(local(2026, 10, 1))
    window = request_reopening(person, session, reason="Was on call that week")
    assert window.opened_at == local(2026, 10, 1)
    assert window.expires_at == local(2026, 10, 8)
    assert (window.granted_by, window.closed_at) == (None, None)
    entry = AuditLog.objects.get(action="evaluation.window_granted")
    assert (entry.actor_type, entry.actor_person) == ("attendee", person)
    assert entry.metadata["override"] is False


def test_acceptance_with_an_active_reopening(session, person, clock):
    clock(local(2026, 10, 1))
    request_reopening(person, session)
    assert submission_allowed(person, session, at=local(2026, 10, 4))
    assert submission_allowed(person, session, at=local(2026, 10, 7, 23, 59))
    assert not submission_allowed(person, session, at=local(2026, 10, 8, 12, 1))
    assert not submission_allowed(person, session, at=local(2026, 9, 30))  # before it was granted


def test_a_reopening_is_for_one_person_and_one_session(session, person, clock):
    clock(local(2026, 10, 1))
    request_reopening(person, session)
    other_session = make_session(make_event(start=EVENT_START - datetime.timedelta(days=14)))
    assert not submission_allowed(make_person(), session, at=local(2026, 10, 2))
    assert not submission_allowed(person, other_session, at=local(2026, 10, 2))


def test_the_window_closes_when_a_complete_evaluation_is_submitted(session, person, clock):
    clock(local(2026, 10, 1))
    window = request_reopening(person, session)
    clock(local(2026, 10, 2))
    evaluate(person, session, complete=False)  # a draft does not close it
    window.refresh_from_db()
    assert window.closed_at is None
    submission = EvaluationSubmission.objects.get(person=person, session=session)
    submission.is_complete = True
    submission.save()
    window.refresh_from_db()
    assert window.closed_at == local(2026, 10, 2)
    assert not submission_allowed(person, session, at=local(2026, 10, 3))
    assert window_state(person, session, at=local(2026, 10, 3)) == STATE_EVALUATED


def test_close_windows_touches_only_that_person_and_session(session, person, clock):
    clock(local(2026, 10, 1))
    mine = request_reopening(person, session)
    theirs = request_reopening(make_person(), session)
    assert close_windows(person, session) == 1
    theirs.refresh_from_db()
    assert theirs.closed_at is None
    mine.refresh_from_db()
    assert mine.closed_at is not None


def test_three_reopenings_then_the_program_office(session, person, clock, settings):
    settings.EVALUATION_REOPENINGS_MAX = 3
    for day in (1, 10, 20):
        clock(local(2026, 10, day))
        request_reopening(person, session)
    clock(local(2026, 11, 1))
    with pytest.raises(ReopeningRefused) as err:
        request_reopening(person, session)
    assert "program office" in str(err.value)
    assert window_state(person, session) == STATE_LIMIT_REACHED
    assert EvaluationWindow.objects.filter(person=person).count() == 3


def test_an_already_evaluated_session_is_not_reopened(session, person, clock):
    clock(local(2026, 9, 16))
    evaluate(person, session)
    clock(local(2026, 10, 1))
    with pytest.raises(ReopeningRefused):
        request_reopening(person, session)


# --- The accreditation year boundary ----------------------------------------


@pytest.mark.parametrize(
    "year_end, day, expected",
    [
        ((12, 31), datetime.date(2026, 9, 15), datetime.date(2026, 12, 31)),
        ((12, 31), datetime.date(2026, 12, 31), datetime.date(2026, 12, 31)),
        ((12, 31), datetime.date(2027, 1, 1), datetime.date(2027, 12, 31)),
        ((6, 30), datetime.date(2026, 9, 15), datetime.date(2027, 6, 30)),
        ((6, 30), datetime.date(2027, 6, 30), datetime.date(2027, 6, 30)),
        ((6, 30), datetime.date(2027, 7, 1), datetime.date(2028, 6, 30)),
    ],
)
def test_accreditation_year_end_is_a_setting(settings, year_end, day, expected):
    settings.ACCREDITATION_YEAR_END = year_end
    assert accreditation_year_end(day) == expected


def test_no_self_service_reopening_past_the_accreditation_year(session, person, clock, settings):
    settings.ACCREDITATION_YEAR_END = (12, 31)
    clock(local(2026, 12, 31, 23, 0))
    request_reopening(person, session)  # the last day still counts
    clock(local(2027, 1, 2))
    assert window_state(person, session) == STATE_REOPENED  # the one granted on the 31st
    clock(local(2027, 1, 10))
    with pytest.raises(ReopeningRefused) as err:
        request_reopening(person, session)
    assert "accreditation year" in str(err.value)
    assert window_state(person, session) == STATE_PAST_YEAR_END


def test_program_admin_can_override_both_limits_and_it_is_logged(session, person, clock, settings):
    settings.EVALUATION_REOPENINGS_MAX = 1
    staff = make_staff("program-admin")
    clock(local(2026, 10, 1))
    request_reopening(person, session)
    clock(local(2027, 3, 1))  # past the year end, and past the limit
    with pytest.raises(ReopeningRefused):
        request_reopening(person, session)
    window = request_reopening(person, session, reason="College audit", user=staff, override=True)
    assert window.granted_by == staff
    assert submission_allowed(person, session, at=local(2027, 3, 4))
    entry = AuditLog.objects.filter(action="evaluation.window_granted").latest("id")
    assert (entry.actor_user, entry.metadata["override"]) == (staff, True)


def test_an_override_needs_a_staff_user(session, person):
    with pytest.raises(ValueError):
        request_reopening(person, session, override=True)


# --- What the credits page shows --------------------------------------------


def test_window_state_walks_through_the_cases(session, person, clock, settings):
    settings.EVALUATION_REOPENINGS_MAX = 1
    assert window_state(person, session, at=local(2026, 9, 16)) == STATE_OPEN
    assert window_state(person, session, at=local(2026, 10, 1)) == STATE_CAN_REQUEST
    clock(local(2026, 10, 1))
    request_reopening(person, session)
    assert window_state(person, session, at=local(2026, 10, 3)) == STATE_REOPENED
    assert window_state(person, session, at=local(2026, 10, 20)) == STATE_LIMIT_REACHED
    assert window_state(person, session, at=local(2027, 2, 1)) == STATE_PAST_YEAR_END


def test_sessions_needing_evaluation_lists_attended_but_unevaluated_sessions(person, clock):
    from attendance.tests.factories import teams_row

    event = make_event(minutes=180, credits="3.00", sessions=0)
    first, second, third = (make_session(event, minutes=60) for _ in range(3))
    teams_row(event, person, 0, 120)  # first and second, not third
    clock(local(2026, 9, 16))
    evaluate(person, first)
    assert sessions_needing_evaluation(person, event) == [(second, STATE_OPEN)]
    assert sessions_needing_evaluation(person, event, at=local(2026, 10, 1)) == [
        (second, STATE_CAN_REQUEST)
    ]


# --- The rows themselves -----------------------------------------------------


def test_windows_are_frozen_except_for_closing(session, person, clock):
    clock(local(2026, 10, 1))
    window = request_reopening(person, session)
    window.expires_at = local(2030, 1, 1)
    with pytest.raises(ImmutableRowError):
        window.save()
    with pytest.raises(ImmutableRowError):
        window.delete()
    with pytest.raises(IntegrityError), transaction.atomic():
        EvaluationWindow.objects.create(
            person=person, session=session, opened_at=local(2026, 10, 1), expires_at=local(2026, 9, 1)
        )
