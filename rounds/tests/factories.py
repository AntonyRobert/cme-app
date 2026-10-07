import datetime
import itertools
from decimal import Decimal

from programs.tests.factories import make_program
from rounds.models import RoundsEvent, Session

_counter = itertools.count(1)

UTC = datetime.timezone.utc
# A Tuesday noon-to-one in Montreal during daylight time, in UTC.
EVENT_START = datetime.datetime(2026, 9, 15, 16, 0, tzinfo=UTC)


def at(minutes):
    """A time `minutes` after the scheduled start of the default test event."""
    return EVENT_START + datetime.timedelta(minutes=minutes)


def make_event(start=EVENT_START, minutes=60, credits="1.00", sessions=1, program=None, **extra):
    """
    An event of `minutes`. By default it has one session spanning the whole
    event, so attendance tests can think in event minutes. Pass sessions=0
    and add your own with make_session for a multi-session event. Without a
    program, the shared default program is used.
    """
    event = RoundsEvent.objects.create(
        program=program or make_program(),
        date=start.date(),
        start_at=start,
        end_at=start + datetime.timedelta(minutes=minutes),
        accredited_credits=Decimal(credits),
        **extra,
    )
    if sessions:
        make_session(event, start=0, minutes=minutes)
    return event


def make_session(event, position=None, start=None, minutes=None, **extra):
    """
    A session starting `start` minutes after the event start (default: when
    the previous one ended) and lasting `minutes` (default: to the event's
    end, or 60 if that is not possible).
    """
    existing = list(event.sessions.order_by("start_at"))
    position = position or len(existing) + 1
    if start is None:
        start_at = existing[-1].end_at if existing else event.start_at
    else:
        start_at = event.start_at + datetime.timedelta(minutes=start)
    if minutes is None:
        end_at = event.end_at if event.end_at > start_at else start_at + datetime.timedelta(hours=1)
    else:
        end_at = start_at + datetime.timedelta(minutes=minutes)
    return Session.objects.create(
        event=event,
        position=position,
        title=extra.pop("title", f"Session {next(_counter)}"),
        start_at=start_at,
        end_at=end_at,
        **extra,
    )
