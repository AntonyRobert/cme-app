"""
Evaluation form templates: which form a session uses, immutable versions,
per-objective expansion, completeness from required questions only, and
the admin that edits them.
"""
import datetime

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from credits.evaluation_forms import (
    LEVEL_EVENT,
    LEVEL_PROGRAM,
    LEVEL_SESSION,
    STANDARD_FORM_NAME,
    Kind,
    compute_is_complete,
    create_standard_form,
    duplicate_form,
    new_version,
    rendered_questions,
    resolve_form,
    store_answer,
)
from credits.models import EvaluationForm, EvaluationFormVersion, EvaluationQuestion, EvaluationSubmission
from people.models import AllowedDomain
from people.tests.factories import make_person
from programs.tests.factories import give_role, make_program
from rounds.models import LearningObjective
from rounds.tests.factories import EVENT_START, make_event, make_session

from .factories import answer_all, evaluate, form_for

pytestmark = pytest.mark.django_db


def objectives(session, *texts):
    return [LearningObjective.objects.create(session=session, text=t) for t in texts]


def make_form(program, name, status="active", questions=None):
    form = EvaluationForm.objects.create(program=program, name=name, status=status)
    version = EvaluationFormVersion.objects.create(form=form, number=1)
    for spec in questions or [{"question_key": "overall", "kind": Kind.LIKERT_5, "prompt": "Overall?"}]:
        EvaluationQuestion.objects.create(version=version, **spec)
    return form


# --- Resolution ---------------------------------------------------------------------


def test_resolution_order_is_session_then_event_then_program():
    program = make_program("Resolving")
    event = make_event(program=program, sessions=0)
    session = make_session(event, minutes=60)
    assert resolve_form(session).form is None  # nothing set anywhere

    standard = create_standard_form(program)
    r = resolve_form(session)
    assert (r.form, r.level) == (standard, LEVEL_PROGRAM)

    for_event = make_form(program, "Event form")
    event.evaluation_form = for_event
    event.save()
    r = resolve_form(session)
    assert (r.form, r.level) == (for_event, LEVEL_EVENT)

    for_session = make_form(program, "Session form")
    session.evaluation_form = for_session
    session.save()
    r = resolve_form(session)
    assert (r.form, r.level, r.version.number) == (for_session, LEVEL_SESSION, 1)


def test_a_form_that_is_not_active_is_skipped_and_the_skip_is_reported():
    program = make_program("Skipping")
    standard = create_standard_form(program)
    event = make_event(program=program, sessions=0)
    session = make_session(event, minutes=60)
    retired = make_form(program, "Old form", status="retired")
    session.evaluation_form = retired
    session.save()

    r = resolve_form(session)

    assert (r.form, r.level) == (standard, LEVEL_PROGRAM)
    assert r.skipped == ((LEVEL_SESSION, retired),)


# --- Versioning -------------------------------------------------------------------------


def test_a_submission_keeps_its_version_s_wording_after_the_form_is_edited():
    program = make_program("Wording")
    form = create_standard_form(program)
    event = make_event(program=program, sessions=0)
    session = make_session(event, minutes=60)
    person = make_person()
    october = evaluate(person, session)  # answers v1
    assert october.form_version.number == 1
    v1_prompt = october.form_version.questions.get(question_key="relevance").prompt

    # March: the wording changes. v1 is locked, so the edit goes to v2.
    locked = form.current_version
    with pytest.raises(ValidationError):
        q = locked.questions.get(question_key="relevance")
        q.prompt = "Changed"
        q.full_clean()
    v2 = new_version(form, note="Reworded relevance")
    q = v2.questions.get(question_key="relevance")
    q.prompt = "The content was relevant to my day-to-day work"
    q.save()

    october.refresh_from_db()
    assert october.form_version == locked
    assert [q.prompt for q in rendered_questions(october.form_version, session) if q.key == "relevance"] == [v1_prompt]
    assert resolve_form(session).version == v2  # new submissions answer v2
    later = evaluate(make_person(), session)
    assert later.form_version == v2
    assert locked.submission_count == 1 and v2.submission_count == 1


def test_a_version_without_submissions_can_be_edited_in_place_and_locks_at_the_first():
    program = make_program("Locking")
    form = create_standard_form(program)
    version = form.current_version
    assert not version.is_locked
    q = version.questions.get(question_key="comments")
    q.prompt = "Anything else?"
    q.save()
    EvaluationQuestion.objects.create(version=version, question_key="extra", kind=Kind.FREE_TEXT, prompt="More?")

    event = make_event(program=program, sessions=0)
    evaluate(make_person(), make_session(event, minutes=60))

    assert version.is_locked
    with pytest.raises(ValidationError):
        EvaluationQuestion.objects.create(version=version, question_key="late", kind=Kind.FREE_TEXT, prompt="?")
    with pytest.raises(ValidationError):
        version.questions.get(question_key="extra").delete()


def test_duplicating_a_form_copies_the_current_questions_into_a_draft():
    program = make_program("Copying")
    form = create_standard_form(program)
    copy = duplicate_form(form, name="Grand rounds variant")
    assert (copy.program, copy.status, copy.current_version.number) == (program, "draft", 1)
    assert list(copy.current_version.questions.values_list("question_key", flat=True)) == list(
        form.current_version.questions.values_list("question_key", flat=True)
    )
    with pytest.raises(ValidationError):
        duplicate_form(form, name="Grand rounds variant")  # names are unique per program


# --- Per-objective expansion ---------------------------------------------------------------


@pytest.mark.parametrize("count", [2, 3])
def test_per_objective_expands_into_one_question_per_objective(count):
    program = make_program(f"Objectives {count}")
    form = create_standard_form(program)
    event = make_event(program=program, sessions=0)
    session = make_session(event, minutes=60)
    texts = [f"Objective {n}" for n in range(1, count + 1)]
    objs = objectives(session, *texts)

    questions = rendered_questions(form.current_version, session)

    expanded = [q for q in questions if q.key == "objectives_met"]
    assert len(expanded) == count
    assert [q.objective for q in expanded] == objs
    assert [q.prompt for q in expanded] == [f"This session met the stated objective: {t}" for t in texts]
    assert all(q.kind == Kind.LIKERT_5 and q.required for q in expanded)
    assert [q.key for q in questions] == ["objectives_met"] * count + [
        "relevance", "commercial_bias", "bias_detail", "practice_change", "comments",
    ]
    assert len({q.field_name for q in questions}) == len(questions)  # distinct inputs


def test_a_session_with_no_objectives_asks_no_per_objective_question():
    program = make_program("No objectives")
    form = create_standard_form(program)
    event = make_event(program=program, sessions=0)
    session = make_session(event, minutes=60)
    assert [q.key for q in rendered_questions(form.current_version, session)][0] == "relevance"


# --- Completeness ----------------------------------------------------------------------------


def test_is_complete_is_computed_from_required_questions_only():
    program = make_program("Completeness")
    create_standard_form(program)
    event = make_event(program=program, sessions=0)
    session = make_session(event, minutes=60)
    a, b = objectives(session, "A", "B")
    person = make_person()
    submission = evaluate(person, session, complete=False)
    assert submission.is_complete is False

    questions = {q.field_name: q for q in rendered_questions(submission.form_version, session)}
    store_answer(submission, questions[f"q_objectives_met_{a.pk}"], 5)
    store_answer(submission, questions["q_relevance"], 4)
    store_answer(submission, questions["q_commercial_bias"], 1)
    submission.refresh_from_db()
    assert submission.is_complete is False  # objective B is required and unanswered

    store_answer(submission, questions[f"q_objectives_met_{b.pk}"], 3)
    submission.refresh_from_db()
    assert submission.is_complete is True  # the three optional free-text questions do not count

    # Removing a required answer makes it incomplete again; the flag follows the responses.
    store_answer(submission, questions["q_relevance"], None)
    submission.refresh_from_db()
    assert submission.is_complete is False and compute_is_complete(submission) is False

    # The flag cannot be set by hand: a save recomputes it.
    submission.is_complete = True
    submission.save()
    submission.refresh_from_db()
    assert submission.is_complete is False


def test_an_empty_free_text_is_not_an_answer():
    program = make_program("Blanks")
    create_standard_form(program)
    event = make_event(program=program, sessions=0)
    session = make_session(event, minutes=60)
    submission = evaluate(make_person(), session, complete=False)
    [relevance] = [q for q in rendered_questions(submission.form_version, session) if q.key == "relevance"]
    store_answer(submission, relevance, 4)
    [bias] = [q for q in rendered_questions(submission.form_version, session) if q.key == "commercial_bias"]
    store_answer(submission, bias, 0)
    [detail] = [q for q in rendered_questions(submission.form_version, session) if q.key == "bias_detail"]
    assert store_answer(submission, detail, "   ") is None  # blank leaves no row
    submission.refresh_from_db()
    assert not submission.is_complete  # "no" to bias makes the detail required
    store_answer(submission, bias, 1)
    submission.refresh_from_db()
    assert submission.is_complete  # "yes": the detail is optional again


# --- The attendee form at its stable URL ------------------------------------------------------


def evaluate_url(session):
    return reverse("credits:evaluate", args=[session.pk])


def signed_in_client(email):
    from signin.tests.test_signin import ask_for_link, last_link_token

    client = Client()
    ask_for_link(client, email)
    client.get(reverse("signin:redeem", args=[last_link_token()]))
    return client


@pytest.fixture
def ada():
    AllowedDomain.objects.create(domain="mcgill.ca")
    return make_person(given="Ada", family="Lovelace", email="ada@mcgill.ca")


def open_session(program):
    """A session whose default evaluation week is running now."""
    start = timezone.now() - datetime.timedelta(days=1)
    event = make_event(program=program, start=start, minutes=60, sessions=0)
    return make_session(event, minutes=60)


def test_the_form_renders_and_a_complete_submission_is_recorded_against_the_version(ada):
    program = make_program("Attendee", require_evaluation_for_credit=True)
    form = create_standard_form(program)
    session = open_session(program)
    a, b = objectives(session, "Explain X", "Apply Y")
    client = signed_in_client("ada@mcgill.ca")

    page = client.get(evaluate_url(session))
    assert page.status_code == 200
    html = page.content.decode()
    assert "This session met the stated objective: Explain X" in html
    assert "This session met the stated objective: Apply Y" in html
    assert "(required)" in html

    # Required questions missing: saved as a draft that says what is left.
    partial = client.post(
        evaluate_url(session), {"q_relevance": "4", "minutes": "60", "attestation": "1", "action": "submit"}
    )
    assert partial.status_code == 200
    html = partial.content.decode()
    assert "draft" in html and "3 thing(s) still needed" in html  # two objectives and the bias question
    draft = EvaluationSubmission.objects.get(person=ada, session=session)
    assert not draft.is_complete and draft.responses.count() == 1

    done = client.post(
        evaluate_url(session),
        {
            f"q_objectives_met_{a.pk}": "5",
            f"q_objectives_met_{b.pk}": "4",
            "q_relevance": "4",
            "q_commercial_bias": "0",
            "q_bias_detail": "A product was named twice.",
            "minutes": "55",
            "attestation": "1",
        },
    )
    assert done.status_code == 200
    assert "your evaluation is complete" in done.content.decode()
    submission = EvaluationSubmission.objects.get(person=ada, session=session)
    assert (submission.form_version, submission.is_complete, submission.self_reported_session_minutes) == (
        form.current_version, True, 55,
    )
    answers = {(r.question_key, r.objective_id): r for r in submission.responses.all()}
    assert answers[("objectives_met", a.pk)].rating == 5
    assert answers[("commercial_bias", None)].rating == 0
    assert answers[("bias_detail", None)].free_text == "A product was named twice."
    assert ("comments", None) not in answers  # optional and blank: no row

    # Re-opening the page shows the answers as asked, read-only.
    again = client.get(evaluate_url(session)).content.decode()
    assert "your evaluation is complete" in again and "disabled" in again


def test_the_form_needs_a_signed_in_attendee_and_an_open_window(ada):
    program = make_program("Gates", require_evaluation_for_credit=True)
    create_standard_form(program)
    session = open_session(program)
    anonymous = Client().get(evaluate_url(session))
    assert anonymous.status_code == 302 and anonymous.url.startswith(reverse("signin:start"))

    old_event = make_event(program=program, start=EVENT_START - datetime.timedelta(days=400), minutes=60, sessions=0)
    old = make_session(old_event, minutes=60)
    closed = signed_in_client("ada@mcgill.ca").get(evaluate_url(old))
    assert closed.status_code == 200 and "window for this session has closed" in closed.content.decode()


def test_the_credits_page_links_each_session_to_its_evaluation(ada):
    from attendance.tests.factories import teams_row

    program = make_program("Linking")
    create_standard_form(program)
    session = open_session(program)
    teams_row(session.event, ada, session.start_at, session.end_at)
    html = signed_in_client("ada@mcgill.ca").get(reverse("signin:me")).content.decode()
    assert evaluate_url(session) in html


# --- The admin -----------------------------------------------------------------------------------


def staff(program, role):
    user = get_user_model().objects.create_user(username=f"{role}-{program.slug}", password="x" * 20, is_staff=True)
    give_role(user, program, role)
    client = Client()
    client.force_login(user)
    return client, user


def test_program_admins_edit_forms_in_their_program_and_coordinators_only_look():
    program = make_program("Admin")
    form = create_standard_form(program)
    change = reverse("admin:credits_evaluationform_change", args=[form.pk])

    coordinator, _ = staff(program, "coordinator")
    page = coordinator.get(change)
    assert page.status_code == 200 and not page.context["has_change_permission"]
    assert coordinator.post(reverse("admin:credits_evaluationform_new_version", args=[form.pk]), {}).status_code == 403

    admin, _ = staff(program, "program_admin")
    page = admin.get(change)
    assert page.status_code == 200 and page.context["has_change_permission"]
    html = page.content.decode()
    assert "New version (copy of v1)" in html and "Duplicate this form" in html and "Preview as an attendee" in html

    elsewhere, _ = staff(make_program("Elsewhere"), "program_admin")
    assert elsewhere.get(change).status_code == 302  # out of scope: not theirs to see


def test_the_admin_makes_a_new_version_and_warns_when_the_current_one_is_locked():
    program = make_program("Versions")
    form = create_standard_form(program)
    event = make_event(program=program, sessions=0)
    evaluate(make_person(), make_session(event, minutes=60))
    admin, _ = staff(program, "program_admin")
    change = reverse("admin:credits_evaluationform_change", args=[form.pk])

    html = admin.get(change).content.decode()
    assert "v1 has 1 submission(s) and is locked" in html
    version_page = admin.get(reverse("admin:credits_evaluationformversion_change", args=[form.current_version.pk]))
    assert "Locked: 1 evaluation(s) answered this wording" in version_page.content.decode()

    response = admin.post(reverse("admin:credits_evaluationform_new_version", args=[form.pk]), {"note": "Reword"})
    assert response.status_code == 302
    v2 = form.current_version
    assert (v2.number, v2.note, v2.questions.count()) == (2, "Reword", 6)
    assert not v2.is_locked


def test_the_admin_duplicates_a_form_and_previews_it_against_a_session():
    program = make_program("Preview")
    form = create_standard_form(program)
    event = make_event(program=program, sessions=0)
    session = make_session(event, minutes=60)
    objectives(session, "Explain X", "Apply Y", "Teach Z")
    admin, _ = staff(program, "program_admin")

    response = admin.post(reverse("admin:credits_evaluationform_duplicate", args=[form.pk]), {"name": "Variant"})
    assert response.status_code == 302
    copy = EvaluationForm.objects.get(program=program, name="Variant")
    assert copy.status == "draft" and copy.current_version.questions.count() == 6

    preview = admin.get(reverse("admin:credits_evaluationform_preview", args=[form.pk]) + f"?session={session.pk}")
    html = preview.content.decode()
    assert html.count("This session met the stated objective:") == 3
    assert "8 question(s) once expanded" in html


def test_the_session_page_says_which_form_resolves_and_from_where():
    program = make_program("Session page")
    standard = create_standard_form(program)
    event = make_event(program=program, sessions=0)
    session = make_session(event, minutes=60)
    admin, _ = staff(program, "program_admin")
    html = admin.get(reverse("admin:rounds_session_change", args=[session.pk])).content.decode()
    assert f"{standard}</a> v1 (set on the program)" in html

    retired = make_form(program, "Retired one", status="retired")
    session.evaluation_form = retired
    session.save()
    html = admin.get(reverse("admin:rounds_session_change", args=[session.pk])).content.decode()
    assert "(set on the program)" in html and "Skipped: Retired one on the session is retired." in html


def test_the_standard_form_is_seeded_for_every_program_and_attached_as_default():
    program = make_program("Seeded")
    form = create_standard_form(program)
    program.refresh_from_db()
    assert program.default_evaluation_form == form and form.status == "active"
    keys = list(form.current_version.questions.order_by("position").values_list("question_key", "kind", "required"))
    assert keys == [
        ("objectives_met", "per_objective", True),
        ("relevance", "likert_5", True),
        ("commercial_bias", "yes_no", True),
        ("bias_detail", "free_text", False),
        ("practice_change", "free_text", False),
        ("comments", "free_text", False),
    ]
    assert create_standard_form(program) == form  # idempotent
    assert form.name == STANDARD_FORM_NAME


# --- Conditional requirement and drafts -------------------------------------------------------


def test_required_when_satisfied_and_unsatisfied():
    """bias_detail is required only while commercial_bias is answered no."""
    program = make_program("Conditional")
    create_standard_form(program)
    event = make_event(program=program, sessions=0)
    session = make_session(event, minutes=60)
    submission = evaluate(make_person(), session, complete=False)
    qs = {q.key: q for q in rendered_questions(submission.form_version, session)}
    store_answer(submission, qs["relevance"], 4)
    store_answer(submission, qs["commercial_bias"], 1)
    submission.refresh_from_db()
    assert submission.is_complete  # yes: the detail is not required
    assert qs["bias_detail"].condition == ("commercial_bias", 0)

    store_answer(submission, qs["commercial_bias"], 0)
    submission.refresh_from_db()
    assert not submission.is_complete  # no: now it is
    from credits.evaluation_forms import remaining

    assert [x.key for x in remaining(submission)] == ["bias_detail"]
    store_answer(submission, qs["bias_detail"], "A product was named twice.")
    submission.refresh_from_db()
    assert submission.is_complete


def test_required_is_derived_from_the_kind_and_only_free_text_can_be_conditional():
    program = make_program("Kinds")
    form = create_standard_form(program)
    version = form.current_version
    assert [q.required for q in version.questions.order_by("position")] == [True, True, True, False, False, False]
    likert = version.questions.get(question_key="relevance")
    likert.required_when = {"question_key": "commercial_bias", "value": 0}
    with pytest.raises(ValidationError):
        likert.full_clean()
    text = version.questions.get(question_key="comments")
    for bad in ({"question_key": "nope", "value": 0}, {"question_key": "comments", "value": 0},
                {"question_key": "bias_detail", "value": "x"}, {"value": 0}):
        text.required_when = bad
        with pytest.raises(ValidationError):
            text.full_clean()


def test_the_server_rejects_a_completion_the_client_would_have_allowed(ada):
    """Bypassing the page's script and posting 'no' with no detail leaves a draft, not a completion."""
    program = make_program("Bypass")
    create_standard_form(program)
    session = open_session(program)
    client = signed_in_client("ada@mcgill.ca")
    response = client.post(
        evaluate_url(session),
        {"q_relevance": "5", "q_commercial_bias": "0", "minutes": "60", "attestation": "1", "action": "submit"},
    )
    assert response.status_code == 200
    html = response.content.decode()
    submission = EvaluationSubmission.objects.get(person=ada, session=session)
    assert not submission.is_complete
    assert "1 thing(s) still needed" in html and 'data-required-when="q_commercial_bias"' in html
    # Answering it completes; answering "yes" instead would have, too.
    done = client.post(
        evaluate_url(session),
        {"q_relevance": "5", "q_commercial_bias": "0", "q_bias_detail": "Two slides were an advert.",
         "minutes": "60", "attestation": "1", "action": "submit"},
    )
    assert "your evaluation is complete" in done.content.decode()


def test_a_draft_survives_the_window_expiring_and_completes_in_a_reopened_one(ada, monkeypatch):
    from credits.windows import request_reopening

    program = make_program("Drafts", require_evaluation_for_credit=True)
    create_standard_form(program)
    session = open_session(program)
    client = signed_in_client("ada@mcgill.ca")
    saved = client.post(evaluate_url(session), {"q_relevance": "4", "action": "save"})
    assert saved.status_code == 200 and "Saved." in saved.content.decode()
    draft = EvaluationSubmission.objects.get(person=ada, session=session)
    assert not draft.is_complete and draft.self_reported_session_minutes is None and not draft.attestation

    # The credits page shows it in progress with what is left.
    from attendance.tests.factories import teams_row

    teams_row(session.event, ada, session.start_at, session.end_at)
    me = client.get(reverse("signin:me")).content.decode()
    assert "Finish your evaluation" in me and "in progress, 3 left" in me  # bias, minutes, attestation

    # A month on: the window is gone, the draft is not.
    later = timezone.now() + datetime.timedelta(days=30)
    monkeypatch.setattr("django.utils.timezone.now", lambda: later)
    page = client.get(evaluate_url(session))
    assert "window for this session has closed" in page.content.decode()
    draft.refresh_from_db()
    assert draft.responses.count() == 1
    refused = client.post(evaluate_url(session), {"q_relevance": "4", "q_commercial_bias": "1",
                                                   "minutes": "60", "attestation": "1", "action": "submit"})
    assert "window for this session has closed" in refused.content.decode()
    draft.refresh_from_db()
    assert not draft.is_complete
    me = client.get(reverse("signin:me")).content.decode()
    assert "Draft saved, 3 left; the window has closed." in me

    # Reopened: the same draft completes.
    request_reopening(ada, session, reason="Was away")
    done = client.post(evaluate_url(session), {"q_relevance": "4", "q_commercial_bias": "1",
                                                "minutes": "60", "attestation": "1", "action": "submit"})
    assert "your evaluation is complete" in done.content.decode()
    draft.refresh_from_db()
    assert draft.is_complete and EvaluationSubmission.objects.filter(person=ada, session=session).count() == 1


def test_credit_appears_only_when_the_draft_is_complete(ada):
    from decimal import Decimal

    from attendance.tests.factories import teams_row
    from credits.rules import credit_breakdown

    program = make_program("Credit", require_evaluation_for_credit=True)
    create_standard_form(program)
    session = open_session(program)
    teams_row(session.event, ada, session.start_at, session.end_at)
    client = signed_in_client("ada@mcgill.ca")
    client.post(evaluate_url(session), {"q_relevance": "4", "minutes": "60", "attestation": "1", "action": "save"})
    assert credit_breakdown(ada, session.event).attendance_credits == Decimal("0.00")
    client.post(evaluate_url(session), {"q_relevance": "4", "q_commercial_bias": "1",
                                        "minutes": "60", "attestation": "1", "action": "submit"})
    assert credit_breakdown(ada, session.event).attendance_credits == Decimal("1.00")
