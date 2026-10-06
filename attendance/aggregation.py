"""
How long someone attended an event.

attended_minutes() is the single place attendance is added up. Nothing
else may sum AttendanceRecord rows.
"""
from dataclasses import dataclass

from django.db import models

from .models import AttendanceRecord


class MinutesSource(models.TextChoices):
    TEAMS = "teams", "Teams"
    MANUAL = "manual", "Manual"
    MIXED = "mixed", "Mixed"
    SELF_REPORTED = "self_reported", "Self-reported"


@dataclass(frozen=True)
class AttendedTime:
    seconds: int
    source: str | None  # None when the person has no active rows at all
    row_count: int

    @property
    def minutes(self):
        """Whole minutes, rounded down."""
        return self.seconds // 60

    @property
    def has_rows(self):
        return self.row_count > 0


def merged_seconds(intervals):
    """
    Total length in seconds of the union of (start, end) datetime intervals.

    Overlapping and touching intervals are counted once.
    """
    total = 0.0
    current_start = current_end = None
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if current_end is None or start > current_end:
            if current_end is not None:
                total += (current_end - current_start).total_seconds()
            current_start, current_end = start, end
        elif end > current_end:
            current_end = end
    if current_end is not None:
        total += (current_end - current_start).total_seconds()
    return int(total)


def attended_minutes(person, event):
    """
    Recorded attendance of `person` at `event`, as an AttendedTime.

    Do NOT simplify this to a sum of duration_seconds. Teams writes one row
    per join, and the same person is often connected twice at once (laptop
    and phone), so the rows overlap. Summing them credits time twice, and
    also credits time spent waiting before the event began.

    The steps:

    1. Take the event's ACTIVE rows (not superseded by a correction) that
       belong to this person, following merges.
    2. Rows with join/leave times, which includes room-roster rows: clamp
       each to the event's credit window, then take the UNION of the
       intervals. duration_seconds is ignored for these rows.
    3. Rows without times (manual hours-only rows): add duration_seconds
       on top.
    4. Cap the total at the length of the credit window.

    Self-reported minutes are not considered here; see
    credits.rules.creditable_minutes for that fallback.
    """
    rows = (
        AttendanceRecord.objects.active()
        .for_person(person)
        .filter(event=event)
        .values_list("source", "join_at", "leave_at", "duration_seconds")
    )
    window_start, window_end = event.credit_window()

    intervals = []
    untimed_seconds = 0
    sources = set()
    row_count = 0
    for source, join_at, leave_at, duration_seconds in rows:
        row_count += 1
        sources.add(source)
        if join_at is not None and leave_at is not None:
            intervals.append((max(join_at, window_start), min(leave_at, window_end)))
        else:
            untimed_seconds += duration_seconds

    window_seconds = int((window_end - window_start).total_seconds())
    seconds = min(merged_seconds(intervals) + untimed_seconds, window_seconds)

    if not sources:
        source = None
    elif sources == {AttendanceRecord.Source.TEAMS_UPLOAD}:
        source = MinutesSource.TEAMS
    elif AttendanceRecord.Source.TEAMS_UPLOAD in sources:
        source = MinutesSource.MIXED
    else:
        source = MinutesSource.MANUAL
    return AttendedTime(seconds=seconds, source=source, row_count=row_count)
