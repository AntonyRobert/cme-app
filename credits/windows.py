"""
When an evaluation may be submitted.

The default window is EVALUATION_WINDOW_DAYS after the event date. After
that, an attendee may request a reopening, which is granted automatically
(up to EVALUATION_REOPENINGS_MAX times) and never past the end of the
program's accreditation year that contains the event. Program admins can
override both limits; every grant is audit-logged.
"""
import datetime
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone

from audit.log import record

from .models import EvaluationSubmission, EvaluationWindow

STATE_EVALUATED = "evaluated"  # a complete evaluation exists
STATE_OPEN = "open"  # the default week is still running
STATE_REOPENED = "reopened"  # a granted window is running
STATE_CAN_REQUEST = "can_request"  # closed, and a reopening may be requested
STATE_LIMIT_REACHED = "limit_reached"  # closed; no more self-service reopenings
STATE_PAST_YEAR_END = "past_year_end"  # closed; the accreditation year has ended


class ReopeningRefused(Exception):
    """The reopening was not granted. The message is safe to show the attendee."""


def _zone():
    return ZoneInfo(settings.TIME_ZONE)


def accreditation_year_end(session):
    """The last day of the program's accreditation year that contains the event."""
    event = session.event
    return event.program.accreditation_year_end(event.date)


def default_window(session):
    """(opens_at, closes_at) of the ordinary window for a session."""
    event = session.event
    closes_on = event.date + datetime.timedelta(days=settings.EVALUATION_WINDOW_DAYS + 1)
    closes_at = datetime.datetime.combine(closes_on, datetime.time(0, 0), tzinfo=_zone())
    return session.start_at, closes_at


def default_window_open(session, at=None):
    at = at or timezone.now()
    opens_at, closes_at = default_window(session)
    return opens_at <= at < closes_at


def open_reopening(person, session, at=None):
    """The currently running granted window, or None."""
    at = at or timezone.now()
    return (
        EvaluationWindow.objects.for_person(person)
        .filter(session=session, closed_at__isnull=True, opened_at__lte=at, expires_at__gt=at)
        .order_by("-expires_at")
        .first()
    )


def submission_allowed(person, session, at=None):
    """May this person submit an evaluation of this session right now?"""
    at = at or timezone.now()
    return default_window_open(session, at) or open_reopening(person, session, at) is not None


def is_evaluated(person, session):
    return (
        EvaluationSubmission.objects.for_person(person)
        .filter(session=session, is_complete=True)
        .exists()
    )


def reopenings_used(person, session):
    return EvaluationWindow.objects.for_person(person).filter(session=session).count()


def request_reopening(person, session, *, reason="", user=None, override=False, request=None):
    """
    Open the form again for a week. Self-service unless `override`, which
    needs a staff user and skips the limits.
    """
    at = timezone.now()
    if override and user is None:
        raise ValueError("An override needs the staff user who made it.")
    if is_evaluated(person, session):
        raise ReopeningRefused("This session has already been evaluated.")
    if not override:
        year_end = accreditation_year_end(session)
        if timezone.localdate(at) > year_end:
            raise ReopeningRefused(
                f"The accreditation year for this event ended on {year_end:%d %B %Y}. "
                "Ask the program office."
            )
        if reopenings_used(person, session) >= settings.EVALUATION_REOPENINGS_MAX:
            raise ReopeningRefused(
                "This form has been reopened as many times as it can be. Ask the program office."
            )
    window = EvaluationWindow.objects.create(
        person=person,
        session=session,
        opened_at=at,
        expires_at=at + datetime.timedelta(days=settings.EVALUATION_WINDOW_DAYS),
        reason=reason,
        granted_by=user if override else None,
    )
    actor = {"user": user} if user is not None else {"person": person}
    record(
        "evaluation.window_granted",
        window,
        request=request,
        metadata={
            "person": str(person.pk),
            "session": str(session.pk),
            "expires_at": window.expires_at,
            "override": override,
            "reason": reason,
        },
        **actor,
    )
    return window


def close_windows(person, session, at=None):
    """Called when a complete evaluation is submitted."""
    at = at or timezone.now()
    return (
        EvaluationWindow.objects.for_person(person)
        .filter(session=session, closed_at__isnull=True)
        .update(closed_at=at)
    )


def window_state(person, session, at=None):
    """One of the STATE_* values, for the credits page."""
    at = at or timezone.now()
    if is_evaluated(person, session):
        return STATE_EVALUATED
    if default_window_open(session, at):
        return STATE_OPEN
    if open_reopening(person, session, at) is not None:
        return STATE_REOPENED
    if timezone.localdate(at) > accreditation_year_end(session):
        return STATE_PAST_YEAR_END
    if reopenings_used(person, session) >= settings.EVALUATION_REOPENINGS_MAX:
        return STATE_LIMIT_REACHED
    return STATE_CAN_REQUEST


def sessions_needing_evaluation(person, event, at=None):
    """
    [(session, state)] for the sessions this person attended but has not
    completely evaluated. What the credits page lists. A session they
    presented is never on it: presenters don't evaluate their own talk.
    """
    from attendance.aggregation import sessions_attended

    from .rules import presented_session_ids

    presented = presented_session_ids(person, event)
    return [
        (session, state)
        for session in sessions_attended(person, event)
        if session.pk not in presented
        and (state := window_state(person, session, at)) != STATE_EVALUATED
    ]
