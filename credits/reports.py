"""Read-only summaries built on the credit rules. Nothing here stores a number."""
from dataclasses import dataclass
from decimal import Decimal

from django.db.models import Sum

from attendance.models import AttendanceRecord
from certificates.models import CertificateLine
from people.identity import resolve_root
from people.models import Person
from rounds.models import RoundsEvent, SessionPresenter

from .models import CreditAdjustment, EvaluationSubmission
from .rules import ZERO, credit_breakdown


def event_people(event):
    """Everyone who attended, presented, evaluated or was adjusted for this event."""
    ids = set(
        AttendanceRecord.objects.active()
        .filter(event=event, person__isnull=False)
        .values_list("person", flat=True)
    )
    ids |= set(
        SessionPresenter.objects.filter(session__event=event).values_list("person", flat=True)
    )
    ids |= set(
        EvaluationSubmission.objects.filter(session__event=event).values_list("person", flat=True)
    )
    ids |= set(CreditAdjustment.objects.filter(event=event).values_list("person", flat=True))
    roots = {}
    for person in Person.objects.filter(pk__in=ids).select_related("merged_into"):
        root = resolve_root(person)
        roots[root.pk] = root
    return sorted(roots.values(), key=lambda p: (p.family_name.lower(), p.given_name.lower()))


def event_credit_rows(event):
    """(person, CreditBreakdown) for everyone involved in the event, by name."""
    return [(person, credit_breakdown(person, event)) for person in event_people(event)]


@dataclass(frozen=True)
class EventStanding:
    """One person's credit for one event, earned now versus already certified."""

    event: RoundsEvent
    breakdown: object
    # On valid (not revoked, not superseded) certificates, by kind.
    certified_attendance: Decimal
    certified_teaching: Decimal

    @property
    def earned_attendance(self):
        return self.breakdown.attendance_credits

    @property
    def earned_teaching(self):
        return self.breakdown.teaching_credits

    @property
    def confirmed_attendance(self):
        """Attendance credit on signed-off minutes: what a certificate would print today."""
        return self.breakdown.attendance_confirmed_credits

    @property
    def awaiting_signoff(self):
        return bool(self.breakdown.awaiting_signoff)

    @property
    def uncertified_attendance(self):
        return self.earned_attendance - self.certified_attendance

    @property
    def uncertified_teaching(self):
        return self.earned_teaching - self.certified_teaching


def person_events(person):
    """Every event this person attended, presented at, evaluated or was adjusted for."""
    ids = set(
        AttendanceRecord.objects.active().for_person(person).values_list("event", flat=True)
    )
    ids |= set(
        SessionPresenter.objects.for_person(person).values_list("session__event", flat=True)
    )
    ids |= set(EvaluationSubmission.objects.for_person(person).values_list("session__event", flat=True))
    ids |= set(CreditAdjustment.objects.for_person(person).values_list("event", flat=True))
    return RoundsEvent.objects.filter(pk__in=ids).order_by("date")


def person_standing(person):
    """
    [EventStanding] by event date. Credit is a moving target: a late
    evaluation earns credit after a certificate was issued. Where earned
    and certified differ, the answer is a reissue, on request.
    """
    certified = {
        row["event"]: row
        for row in CertificateLine.objects.for_person(person)
        .filter(certificate__revoked_at__isnull=True, certificate__superseded_by__isnull=True)
        .values("event")
        .annotate(attendance=Sum("attendance_credits"), teaching=Sum("teaching_credits"))
    }
    standings = []
    for event in person_events(person):
        row = certified.get(event.pk, {})
        standings.append(
            EventStanding(
                event=event,
                breakdown=credit_breakdown(person, event),
                certified_attendance=(row.get("attendance") or ZERO).quantize(ZERO),
                certified_teaching=(row.get("teaching") or ZERO).quantize(ZERO),
            )
        )
    return standings


@dataclass(frozen=True)
class ProgramStanding:
    """One person's credit in one program: what the credits page shows per program."""

    program: object
    standings: list  # EventStanding, by date

    @property
    def earned_attendance(self):
        return sum((s.earned_attendance for s in self.standings), ZERO)

    @property
    def earned_teaching(self):
        return sum((s.earned_teaching for s in self.standings), ZERO)

    @property
    def confirmed_attendance(self):
        return sum((s.confirmed_attendance for s in self.standings), ZERO)

    @property
    def certified_attendance(self):
        return sum((s.certified_attendance for s in self.standings), ZERO)

    @property
    def certified_teaching(self):
        return sum((s.certified_teaching for s in self.standings), ZERO)


def person_standing_by_program(person):
    """
    [ProgramStanding], one per program the person has anything in, by
    program name. A total per program, never one number across programs.
    """
    by_program = {}
    for standing in person_standing(person):
        by_program.setdefault(standing.event.program, []).append(standing)
    return [
        ProgramStanding(program=program, standings=standings)
        for program, standings in sorted(by_program.items(), key=lambda item: item[0].name)
    ]
