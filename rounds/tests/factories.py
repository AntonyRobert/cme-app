import datetime
import itertools
from decimal import Decimal

from rounds.models import RoundsEvent, Session

_counter = itertools.count(1)

UTC = datetime.timezone.utc
# A Tuesday noon-to-one in Montreal during daylight time, in UTC.
EVENT_START = datetime.datetime(2026, 9, 15, 16, 0, tzinfo=UTC)


def at(minutes):
    """A time `minutes` after the scheduled start of the default test event."""
    return EVENT_START + datetime.timedelta(minutes=minutes)


def make_event(start=EVENT_START, minutes=60, credits="1.00", **extra):
    return RoundsEvent.objects.create(
        date=start.date(),
        start_at=start,
        end_at=start + datetime.timedelta(minutes=minutes),
        accredited_credits=Decimal(credits),
        **extra,
    )


def make_session(event, position=None, minutes=20, **extra):
    position = position or event.sessions.count() + 1
    return Session.objects.create(
        event=event,
        position=position,
        title=extra.pop("title", f"Session {next(_counter)}"),
        duration_minutes=minutes,
        **extra,
    )
