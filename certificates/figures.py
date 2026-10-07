"""
What a year-end certificate would print, computed but not issued.

Issuing (the PDF, saving the snapshot, the verification page) is not
built yet. This is the calculation it will snapshot, kept separate so it
can be checked now: one line per event with attendance and teaching kept
apart, and an exact total per kind. Never one blended figure, never
rounded.
"""
from dataclasses import dataclass, field
from decimal import Decimal

from credits.reports import person_events
from credits.rules import credit_breakdown

from .rules import certificate_total


@dataclass(frozen=True)
class LineFigures:
    event: object
    event_title: str
    event_date: object
    session_titles: list  # sessions attended (half or more), not presented
    attended_minutes: int  # minutes in sessions they did not present
    minutes_source: str | None
    attendance_computed: Decimal
    attendance_adjustment: Decimal
    attendance_credits: Decimal
    presented_session_titles: list
    teaching_minutes: int
    teaching_computed: Decimal
    teaching_adjustment: Decimal
    teaching_credits: Decimal


@dataclass(frozen=True)
class CertificateFigures:
    lines: list = field(default_factory=list)

    @property
    def attendance_credits(self):
        """The year's attendance credit: the exact sum of the lines."""
        return certificate_total(line.attendance_credits for line in self.lines)

    @property
    def teaching_credits(self):
        """The year's teaching credit: the exact sum of the lines."""
        return certificate_total(line.teaching_credits for line in self.lines)

    @property
    def total_credits(self):
        """The sum of the two kinds. All three printed figures add up exactly."""
        return self.attendance_credits + self.teaching_credits


def certificate_figures(person, period_start, period_end):
    """The figures a certificate for this person and period would print."""
    lines = []
    for event in person_events(person).filter(date__gte=period_start, date__lte=period_end):
        b = credit_breakdown(person, event)
        if not (b.attendance_credits or b.teaching_credits):
            continue
        lines.append(
            LineFigures(
                event=event,
                event_title=event.title,
                event_date=event.date,
                session_titles=[s.title for s in b.sessions_attended],
                attended_minutes=sum(s.minutes for s in b.sessions if not s.presented),
                minutes_source=b.source,
                attendance_computed=b.attendance_computed,
                attendance_adjustment=b.attendance_adjustment,
                attendance_credits=b.attendance_credits,
                presented_session_titles=[s.title for s in b.sessions_presented],
                teaching_minutes=b.teaching_minutes,
                teaching_computed=b.teaching_computed,
                teaching_adjustment=b.teaching_adjustment,
                teaching_credits=b.teaching_credits,
            )
        )
    return CertificateFigures(lines=lines)
