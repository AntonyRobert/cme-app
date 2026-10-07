"""
What a year-end certificate would print, computed but not issued.

Issuing (the PDF, saving the snapshot, the verification page) is not
built yet. This is the calculation it will snapshot, kept separate so it
can be checked now: one line per event with attendance and teaching kept
apart, and an exact total per kind. Never one blended figure, never
rounded. Attendance figures are CONFIRMED minutes only; an event with
unconfirmed attendance refuses (NotSignedOff) rather than printing a
figure nobody signed off.
"""
from dataclasses import dataclass, field
from decimal import Decimal

from credits.reports import person_events
from credits.rules import credit_breakdown

from .rules import certificate_total


@dataclass(frozen=True)
class Blocker:
    """One event whose attendance must be signed off before a certificate can print."""

    event: object
    sessions: list  # the sessions awaiting a decision

    @property
    def signoff_url(self):
        from django.urls import reverse

        return reverse("admin:rounds_roundsevent_signoff", args=[self.event.pk])

    def __str__(self):
        titles = ", ".join(s.title for s in self.sessions)
        return f"{self.event.date:%Y-%m-%d} {self.event.title} ({titles})"


class NotSignedOff(Exception):
    """A certificate cannot be issued while attendance in the period is unconfirmed."""

    def __init__(self, person, blockers):
        self.person, self.blockers = person, list(blockers)
        listed = "; ".join(str(b) for b in self.blockers)
        super().__init__(
            f"{person} has attendance not yet signed off at {len(self.blockers)} event(s): "
            f"{listed}. Sign them off before issuing."
        )

    @property
    def event(self):
        return self.blockers[0].event


def certificate_blockers(person, program, period_start, period_end):
    """
    [Blocker] for the events in the period whose attendance for this person
    is not signed off, by date. Empty means a certificate can be issued. A
    forgotten event in March blocks December's certificate, so the list
    names every one, not the first.
    """
    blockers = []
    for event in person_events(person).filter(
        program=program, date__gte=period_start, date__lte=period_end
    ):
        b = credit_breakdown(person, event)
        if b.blocking_certificate:
            blockers.append(Blocker(event=event, sessions=b.blocking_certificate))
    return blockers


@dataclass(frozen=True)
class LineFigures:
    event: object
    event_title: str
    event_date: object
    session_titles: list  # sessions attended (half or more), not presented
    attended_minutes: int  # minutes in sessions they did not present
    minutes_source: str | None
    attendance_rate_per_hour: Decimal  # the program's rate at the time, snapshotted
    attendance_computed: Decimal
    attendance_adjustment: Decimal
    attendance_credits: Decimal
    presented_session_titles: list
    teaching_minutes: int
    teaching_rate_per_hour: Decimal
    teaching_computed: Decimal
    teaching_adjustment: Decimal
    teaching_credits: Decimal


@dataclass(frozen=True)
class CertificateFigures:
    lines: list = field(default_factory=list)
    # Names the accredited CPD provider; snapshotted onto the issued certificate.
    accreditation_statement: str = ""

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


def certificate_figures(person, program, period_start, period_end):
    """
    The figures a certificate for this person, program and period would
    print. One certificate per program: events of other programs are not
    on it.
    """
    blockers = certificate_blockers(person, program, period_start, period_end)
    if blockers:
        raise NotSignedOff(person, blockers)
    lines = []
    for event in person_events(person).filter(
        program=program, date__gte=period_start, date__lte=period_end
    ):
        b = credit_breakdown(person, event)
        if not (b.attendance_confirmed_credits or b.teaching_credits):
            continue
        lines.append(
            LineFigures(
                event=event,
                event_title=event.title,
                event_date=event.date,
                session_titles=[s.title for s in b.sessions_attended],
                attended_minutes=sum(
                    s.confirmed_minutes or 0 for s in b.sessions if not s.presented
                ),
                minutes_source=b.source,
                attendance_rate_per_hour=b.attendance_rate_per_hour,
                attendance_computed=b.attendance_confirmed_computed,
                attendance_adjustment=b.attendance_adjustment,
                attendance_credits=b.attendance_confirmed_credits,
                presented_session_titles=[s.title for s in b.sessions_presented],
                teaching_minutes=b.teaching_minutes,
                teaching_rate_per_hour=b.teaching_rate_per_hour,
                teaching_computed=b.teaching_computed,
                teaching_adjustment=b.teaching_adjustment,
                teaching_credits=b.teaching_credits,
            )
        )
    return CertificateFigures(accreditation_statement=program.accreditation_statement, lines=lines)
