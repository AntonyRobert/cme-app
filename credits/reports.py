"""Read-only summaries built on the credit rules. Nothing here stores a number."""
from dataclasses import dataclass
from decimal import Decimal

from django.db.models import Sum

from attendance.models import AttendanceRecord
from certificates.models import CertificateLine
from people.identity import resolve_root
from people.models import Person
from rounds.models import RoundsEvent

from .models import CreditAdjustment, EvaluationSubmission
from .rules import ZERO, credit_breakdown


def event_people(event):
    """Everyone with attendance, an evaluation or an adjustment for this event."""
    ids = set(
        AttendanceRecord.objects.active()
        .filter(event=event, person__isnull=False)
        .values_list("person", flat=True)
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
    certified_credits: Decimal  # on valid (not revoked, not superseded) certificates

    @property
    def earned_credits(self):
        return self.breakdown.credits

    @property
    def difference(self):
        return self.earned_credits - self.certified_credits


def person_events(person):
    """Every event this person has attendance, an evaluation or an adjustment for."""
    ids = set(
        AttendanceRecord.objects.active().for_person(person).values_list("event", flat=True)
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
    certified = dict(
        CertificateLine.objects.for_person(person)
        .filter(certificate__revoked_at__isnull=True, certificate__superseded_by__isnull=True)
        .values_list("event")
        .annotate(total=Sum("credits"))
    )
    return [
        EventStanding(
            event=event,
            breakdown=credit_breakdown(person, event),
            certified_credits=(certified.get(event.pk) or ZERO).quantize(ZERO),
        )
        for event in person_events(person)
    ]
