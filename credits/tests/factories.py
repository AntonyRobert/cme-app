from decimal import Decimal

from credits.models import CreditAdjustment, EvaluationSubmission
from people.tests.factories import make_staff


def evaluate(person, session, *, minutes=20, complete=True, **extra):
    return EvaluationSubmission.objects.create(
        person=person,
        session=session,
        self_reported_session_minutes=minutes,
        attestation=True,
        is_complete=complete,
        **extra,
    )


def adjust(person, event, delta, reason="Goodwill, approved by the chair", user=None, kind="attendance"):
    return CreditAdjustment.objects.create(
        person=person,
        event=event,
        kind=kind,
        delta_credits=Decimal(delta),
        reason=reason,
        created_by=user or make_staff(),
    )
