"""
How long someone attended, session by session.

attended_minutes() is the single place attendance is added up. Nothing
else may sum AttendanceRecord rows.
"""
import logging
from dataclasses import dataclass, field

from django.db import models

from rounds.models import CREDIT_WINDOW_GRACE

from .models import AttendanceRecord

logger = logging.getLogger(__name__)

# A session counts as attended when this much of it was attended.
ATTENDED_FRACTION = 0.5


class MinutesSource(models.TextChoices):
    TEAMS = "teams", "Teams"
    MANUAL = "manual", "Manual"
    MIXED = "mixed", "Mixed"
    SELF_REPORTED = "self_reported", "Self-reported"


@dataclass(frozen=True)
class SessionAttendance:
    session: object
    seconds: int
    # Seconds cut off because hours-only rows pushed this session past its
    # length. Not zero usually means a duplicate manual row.
    capped_seconds: int = 0

    @property
    def minutes(self):
        """Whole minutes, rounded down."""
        return self.seconds // 60

    @property
    def fraction(self):
        length = self.session.length_seconds
        return self.seconds / length if length else 0.0

    @property
    def attended(self):
        """At least ATTENDED_FRACTION of the session."""
        return self.fraction >= ATTENDED_FRACTION


@dataclass(frozen=True)
class AttendedTime:
    sessions: list = field(default_factory=list)  # SessionAttendance, in session order
    source: str | None = None  # None when the person has no active rows at all
    row_count: int = 0

    @property
    def seconds(self):
        return sum(s.seconds for s in self.sessions)

    @property
    def minutes(self):
        """Whole minutes across the event, rounded down."""
        return self.seconds // 60

    @property
    def capped_seconds(self):
        return sum(s.capped_seconds for s in self.sessions)

    @property
    def has_rows(self):
        return self.row_count > 0

    def for_session(self, session):
        return next((s for s in self.sessions if s.session.pk == session.pk), None)

    @property
    def sessions_attended(self):
        return [s.session for s in self.sessions if s.attended]


def merged_intervals(intervals):
    """The union of (start, end) datetime intervals, as a sorted list of disjoint ones."""
    merged = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def merged_seconds(intervals):
    """
    Total length in seconds of the union of (start, end) datetime intervals.

    Overlapping and touching intervals are counted once.
    """
    return int(sum((end - start).total_seconds() for start, end in merged_intervals(intervals)))


def overlap_seconds(disjoint, start, end):
    """Seconds of the disjoint, sorted intervals that fall inside (start, end)."""
    total = 0.0
    for s, e in disjoint:
        lo, hi = max(s, start), min(e, end)
        if hi > lo:
            total += (hi - lo).total_seconds()
    return int(total)


def attended_minutes(person, event):
    """
    Recorded attendance of `person` at `event`, session by session.

    Do NOT simplify this to a sum of duration_seconds. Teams writes one row
    per join, and the same person is often connected twice at once (laptop
    and phone), so the rows overlap. Summing them credits time twice, and
    also credits time spent waiting before the first talk or during a break.

    The steps:

    1. Take the event's ACTIVE rows (not superseded by a correction) that
       belong to this person, following merges.
    2. Rows with join/leave times, which includes room-roster rows: take the
       UNION of their intervals. duration_seconds is ignored for these rows.
    3. For each session, measure how much of that union falls inside the
       session's own times. The first session opens CREDIT_WINDOW_GRACE
       early and the last closes CREDIT_WINDOW_GRACE late; time between
       sessions is not educational activity and counts for nothing.
    4. Rows without times (hours-only manual rows) name a session; their
       duration_seconds is added to that session.
    5. Each session is capped at its own length. Nobody attends a talk for
       longer than it ran. When hours-only rows are what pushed a session
       over, that is logged and reported in capped_seconds.

    Self-reported minutes are not considered here; see
    credits.rules.creditable_time for that fallback.
    """
    sessions = list(event.sessions.order_by("start_at", "position"))
    rows = (
        AttendanceRecord.objects.active()
        .for_person(person)
        .filter(event=event)
        .values_list("source", "join_at", "leave_at", "duration_seconds", "session_id")
    )

    intervals = []
    untimed = {}
    sources = set()
    row_count = 0
    for source, join_at, leave_at, duration_seconds, session_id in rows:
        row_count += 1
        sources.add(source)
        if join_at is not None and leave_at is not None:
            intervals.append((join_at, leave_at))
        else:
            untimed[session_id] = untimed.get(session_id, 0) + duration_seconds
    union = merged_intervals(intervals)

    per_session = []
    for index, session in enumerate(sessions):
        start, end = session.start_at, session.end_at
        if index == 0:
            start -= CREDIT_WINDOW_GRACE
        if index == len(sessions) - 1:
            end += CREDIT_WINDOW_GRACE
        length = session.length_seconds
        # Grace can make up for leaving early, but not exceed the session.
        timed = min(overlap_seconds(union, start, end), length)
        hours_only = untimed.get(session.pk, 0)
        seconds = min(timed + hours_only, length)
        capped = timed + hours_only - seconds
        if capped:
            logger.warning(
                "Attendance capped at the session length: person=%s session=%s timed=%ss "
                "hours_only=%ss session=%ss. Probably a duplicate manual row.",
                person.pk,
                session.pk,
                timed,
                hours_only,
                length,
            )
        per_session.append(SessionAttendance(session=session, seconds=seconds, capped_seconds=capped))

    if not sources:
        source = None
    elif sources == {AttendanceRecord.Source.TEAMS_UPLOAD}:
        source = MinutesSource.TEAMS
    elif AttendanceRecord.Source.TEAMS_UPLOAD in sources:
        source = MinutesSource.MIXED
    else:
        source = MinutesSource.MANUAL
    return AttendedTime(sessions=per_session, source=source, row_count=row_count)


def sessions_attended(person, event):
    """
    The event's sessions this person attended at least ATTENDED_FRACTION of,
    by recorded attendance. What the evaluation reminder and the
    certificate line list.
    """
    return attended_minutes(person, event).sessions_attended
