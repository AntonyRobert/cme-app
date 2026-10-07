"""
The attendee's evaluation form for one session, at a stable URL an email
can link to: /evaluate/<session id>/.
"""
from django.db import transaction
from django.http import Http404
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from core.authz import public_object
from rounds.models import Session
from signin.views import signed_in

from .evaluation_forms import (
    Kind,
    remaining,
    rendered_questions,
    resolve_activity_form,
    resolve_form,
    response_for,
    responses_by_question,
    store_answer,
)
from .models import EvaluationSubmission
from .windows import submission_allowed


def _current_answers(submission, questions):
    """{field name: value} from stored responses, for re-rendering."""
    if submission is None:
        return {}
    responses = responses_by_question(submission)
    answers = {}
    for q in questions:
        r = response_for(q, responses)
        if r is None:
            continue
        if r.rating is not None:
            answers[q.field_name] = r.rating
        elif r.selected:
            answers[q.field_name] = r.selected if q.kind == Kind.MULTI_CHOICE else r.selected[0]
        elif r.free_text:
            answers[q.field_name] = r.free_text
    return answers


def _posted_answers(request, questions):
    answers = {}
    for q in questions:
        if q.kind == Kind.MULTI_CHOICE:
            answers[q.field_name] = request.POST.getlist(q.field_name)
        else:
            answers[q.field_name] = request.POST.get(q.field_name, "").strip()
    return answers


def _validate(questions, answers):
    """Only the shape of what was given; what is missing is the draft's business."""
    errors = {}
    for q in questions:
        value = answers.get(q.field_name)
        if value in (None, "", []):
            continue
        valid = [str(v) for v, _ in q.options]
        if q.kind == Kind.MULTI_CHOICE:
            if any(v not in valid for v in value):
                errors[q.field_name] = "Choose from the options given."
        elif q.options and str(value) not in valid:
            errors[q.field_name] = "Choose from the options given."
    return errors


def _left(submission):
    """What a draft still needs, as (fields, count) for the page."""
    items = remaining(submission)
    fields = {(x if isinstance(x, str) else x.field_name) for x in items}
    return fields, len(items)


def _evaluate(request, target):
    """Shared by the session and the overall-activity forms. `target` is a Session or a RoundsEvent."""
    from rounds.models import Session

    from .rules import evaluation_required

    is_session = isinstance(target, Session)
    session = target if is_session else None
    event = target.event if is_session else target
    person = request.person
    lookup = {"person": person, "session": session} if is_session else {"person": person, "event": event, "session": None}
    submission = EvaluationSubmission.objects.filter(**lookup).first()
    gated = is_session and evaluation_required(event.program)
    context = {
        "session": session,
        "event": event,
        "is_session": is_session,
        "gated": gated,
        "presenters": ", ".join(str(p.person) for p in session.session_presenters.select_related("person")) if is_session else "",
        "submission": submission,
        "errors": {},
    }

    if submission is not None:
        version = submission.form_version  # October's wording, whatever March did
    else:
        resolution = resolve_form(session) if is_session else resolve_activity_form(event.program)
        version = resolution.version
        if version is None:
            context["closed"] = "There is no evaluation form set up for this yet. Ask the program office."
            return render(request, "credits/evaluate.html", context)
    questions = rendered_questions(version, session)
    context["questions"] = questions

    # Open? A session evaluation follows the program's rule (window, or until the event
    # closes); the overall-activity one is open until the event closes.
    if is_session:
        allowed = submission_allowed(person, session)
    else:
        allowed = not event.is_closed
    # With the gate on, a complete evaluation is final; without it, it can be changed until the event closes.
    final = submission is not None and submission.is_complete and (gated or not allowed)
    context["readonly"] = final

    if final:
        context["answers"] = _current_answers(submission, questions)
        context["minutes"] = submission.self_reported_session_minutes
        return render(request, "credits/evaluate.html", context)

    if not allowed:
        context["closed"] = (
            "The evaluation window for this session has closed. You can ask for another "
            "week from your credits page."
            if gated
            else "This event is closed, so its evaluations can no longer be changed."
        )
        return render(request, "credits/evaluate.html", context)

    if request.method == "GET":
        context["answers"] = _current_answers(submission, questions)
        context["minutes"] = submission.self_reported_session_minutes if submission else None
        context["attested"] = submission is not None and submission.attestation
        if submission is not None:
            context["left_fields"], context["left"] = _left(submission)
        return render(request, "credits/evaluate.html", context)

    # Any POST saves what was given as a draft; "submit" also points at what is missing.
    answers = _posted_answers(request, questions)
    errors = _validate(questions, answers)
    minutes = request.POST.get("minutes", "").strip()
    if is_session and minutes and not (minutes.isdigit() and 0 <= int(minutes) <= session.length_minutes):
        errors["minutes"] = f"Whole minutes between 0 and {session.length_minutes}."
    attested = bool(request.POST.get("attestation"))
    context.update({"answers": answers, "minutes": minutes, "attested": attested})
    if errors:
        context["errors"] = errors
        return render(request, "credits/evaluate.html", context, status=400)

    with transaction.atomic():
        if submission is None:
            submission = EvaluationSubmission(form_version=version, **lookup)
            submission.full_clean()
            submission.save()
        if is_session:
            submission.self_reported_session_minutes = int(minutes) if minutes else None
            submission.attestation = attested
        submission.save()
        for q in questions:
            store_answer(submission, q, answers.get(q.field_name))
    submission.refresh_from_db()
    context.update({"submission": submission, "errors": {}})
    context["answers"] = _current_answers(submission, questions)
    context["readonly"] = submission.is_complete and gated
    if not submission.is_complete:
        context["left_fields"], context["left"] = _left(submission)
        context["submitted"] = request.POST.get("action") == "submit"
    context["saved"] = True
    return render(request, "credits/evaluate.html", context)


@public_object(
    "any signed-in attendee may evaluate a session while its form is open; the "
    "submission is written for request.person only"
)
@require_http_methods(["GET", "POST"])
@signed_in
def evaluate(request, session_id):
    session = Session.objects.select_related("event__program").filter(pk=session_id).first()
    if session is None:
        raise Http404
    return _evaluate(request, session)


@public_object(
    "any signed-in attendee may evaluate an event's overall activity until it closes; the "
    "submission is written for request.person only"
)
@require_http_methods(["GET", "POST"])
@signed_in
def evaluate_event(request, event_id):
    from rounds.models import RoundsEvent

    event = RoundsEvent.objects.select_related("program").filter(pk=event_id).first()
    if event is None:
        raise Http404
    return _evaluate(request, event)
