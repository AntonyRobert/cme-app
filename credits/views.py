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
    rendered_questions,
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
    errors = {}
    for q in questions:
        value = answers.get(q.field_name)
        if q.required and (value is None or value == "" or value == []):
            errors[q.field_name] = "This question is required."
            continue
        if value in (None, "", []):
            continue
        valid = [str(v) for v, _ in q.options]
        if q.kind == Kind.MULTI_CHOICE:
            if any(v not in valid for v in value):
                errors[q.field_name] = "Choose from the options given."
        elif q.options and str(value) not in valid:
            errors[q.field_name] = "Choose from the options given."
    return errors


@public_object(
    "any signed-in attendee may evaluate a session while its window is open; the "
    "submission is written for request.person only, and credit still needs attendance"
)
@require_http_methods(["GET", "POST"])
@signed_in
def evaluate(request, session_id):
    session = (
        Session.objects.select_related("event__program").filter(pk=session_id).first()
    )
    if session is None:
        raise Http404
    person = request.person
    submission = EvaluationSubmission.objects.filter(person=person, session=session).first()
    context = {
        "session": session,
        "presenters": ", ".join(str(p.person) for p in session.session_presenters.select_related("person")),
        "submission": submission,
        "errors": {},
    }

    if submission is not None:
        version = submission.form_version  # October's wording, whatever March did
    else:
        resolution = resolve_form(session)
        version = resolution.version
        if version is None:
            context["closed"] = "This session has no evaluation form set up yet. Ask the program office."
            return render(request, "credits/evaluate.html", context)
    questions = rendered_questions(version, session)
    context["questions"] = questions

    if submission is not None and submission.is_complete:
        context["answers"] = _current_answers(submission, questions)
        context["minutes"] = submission.self_reported_session_minutes
        return render(request, "credits/evaluate.html", context)

    if not submission_allowed(person, session):
        context["closed"] = (
            "The evaluation window for this session has closed. You can ask for another "
            "week from your credits page."
        )
        return render(request, "credits/evaluate.html", context)

    if request.method == "GET":
        context["answers"] = _current_answers(submission, questions)
        context["minutes"] = submission.self_reported_session_minutes if submission else None
        context["attested"] = submission is not None
        return render(request, "credits/evaluate.html", context)

    answers = _posted_answers(request, questions)
    errors = _validate(questions, answers)
    minutes = request.POST.get("minutes", "").strip()
    if not minutes.isdigit() or not (0 <= int(minutes) <= session.length_minutes):
        errors["minutes"] = f"Whole minutes between 0 and {session.length_minutes}."
    if not request.POST.get("attestation"):
        errors["attestation"] = "Please confirm the minutes are accurate."
    context.update({"answers": answers, "minutes": minutes, "attested": bool(request.POST.get("attestation"))})
    if errors:
        context["errors"] = errors
        return render(request, "credits/evaluate.html", context, status=400)

    with transaction.atomic():
        if submission is None:
            submission = EvaluationSubmission(
                person=person,
                session=session,
                form_version=version,
                self_reported_session_minutes=int(minutes),
                attestation=True,
            )
            submission.full_clean()
            submission.save()
        else:
            submission.self_reported_session_minutes = int(minutes)
            submission.save()
        for q in questions:
            store_answer(submission, q, answers.get(q.field_name))
    submission.refresh_from_db()
    context.update({"submission": submission, "errors": {}})
    return render(request, "credits/evaluate.html", context)
