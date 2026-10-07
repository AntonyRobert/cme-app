from decimal import Decimal

from credits.models import CreditAdjustment, EvaluationSubmission
from people.tests.factories import make_staff


def form_for(session):
    """The version this session's evaluation answers; the standard form is seeded if nothing resolves."""
    from credits.evaluation_forms import create_standard_form, resolve_form

    resolution = resolve_form(session)
    if resolution.form is None:
        create_standard_form(session.event.program)
        resolution = resolve_form(session)
    return resolution.version


def answer_all(submission, *, required_only=True):
    """Answer the questions of the submission's version: 4 on a scale, yes, the first choice, a line of text."""
    from credits.evaluation_forms import Kind, rendered_questions, store_answer

    for q in rendered_questions(submission.form_version, submission.session):
        if required_only and not q.required:
            continue
        if q.kind == Kind.LIKERT_5:
            value = 4
        elif q.kind == Kind.YES_NO:
            value = 1
        elif q.kind == Kind.SINGLE_CHOICE:
            value = q.question.choices[0]
        elif q.kind == Kind.MULTI_CHOICE:
            value = q.question.choices[:1]
        else:
            value = "Fine."
        store_answer(submission, q, value)
    submission.refresh_from_db()
    return submission


def evaluate(person, session, *, minutes=20, complete=True, **extra):
    """
    A submission; complete means every required question of the resolved
    form is answered (is_complete is computed, never set).
    """
    submission = EvaluationSubmission.objects.create(
        person=person,
        session=session,
        form_version=extra.pop("form_version", None) or form_for(session),
        self_reported_session_minutes=minutes,
        attestation=True,
        **extra,
    )
    if complete:
        answer_all(submission)
    return submission


def adjust(person, event, delta, reason="Goodwill, approved by the chair", user=None, kind="attendance"):
    return CreditAdjustment.objects.create(
        person=person,
        event=event,
        kind=kind,
        delta_credits=Decimal(delta),
        reason=reason,
        created_by=user or make_staff(),
    )
