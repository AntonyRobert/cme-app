"""Read-only summaries built on the credit rules. Nothing here stores a number."""
from attendance.models import AttendanceRecord
from people.identity import resolve_root
from people.models import Person

from .models import CreditAdjustment, EvaluationSubmission
from .rules import credit_breakdown


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
