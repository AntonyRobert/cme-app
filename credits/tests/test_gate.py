"""
The evaluation gate is a per-program switch, off by default: the Royal
College standard requires offering the opportunity to evaluate, not
enforcing completion. Credit follows confirmed attendance alone unless a
program turns the gate on, and then the gate and the window rules resume.
"""
import datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from attendance.signoff import confirm_event
from attendance.tests.factories import teams_row
from certificates.figures import certificate_figures
from credits.evaluation_forms import create_standard_form
from credits.models import EvaluationSubmission
from credits.rules import credit_breakdown, evaluation_required
from credits.windows import (
    STATE_CAN_REQUEST,
    STATE_EVENT_CLOSED,
    STATE_OPEN,
    ReopeningRefused,
    request_reopening,
    submission_allowed,
    window_state,
)
from people.models import AllowedDomain
from people.tests.factories import make_person
from programs.tests.factories import make_program, make_signer
from rounds.tests.factories import EVENT_START, make_event, make_session

from .factories import evaluate

pytestmark = pytest.mark.django_db

D = Decimal
YEAR = (datetime.date(2026, 1, 1), datetime.date(2026, 12, 31))


def program_and_session(name, **settings):
    program = make_program(name, **settings)
    create_standard_form(program)
    event = make_event(program=program, minutes=60, credits="1.00", sessions=0)
    session = make_session(event, minutes=60)
    return program, event, session


# --- Off by default: credit from attendance alone ----------------------------------------------


def test_the_gate_is_off_by_default_and_credit_follows_attendance_alone():
    program, event, session = program_and_session("Ungated")
    person = make_person()
    assert evaluation_required(program) is False
    teams_row(event, person, 0, 60)

    b = credit_breakdown(person, event)

    assert b.sessions_evaluated == []
    assert b.attendance_credits == D("1.00")  # proposed, no evaluation anywhere
    [s] = b.sessions
    assert s.unlocked and s.credited_minutes == 60
    assert s.blocks_certificate  # unsigned minutes that would carry credit: sign-off is still the gate
    confirm_event(event, user=make_signer(program))
    b = credit_breakdown(person, event)
    assert b.attendance_confirmed_credits == D("1.00")
    figures = certificate_figures(person, program, *YEAR)
    assert figures.attendance_credits == D("1.00")


def test_unsigned_minutes_block_a_certificate_without_any_evaluation_when_the_gate_is_off():
    from certificates.figures import NotSignedOff, certificate_blockers

    program, event, session = program_and_session("Ungated blockers")
    person = make_person()
    teams_row(event, person, 0, 60)
    assert [b.event for b in certificate_blockers(person, program, *YEAR)] == [event]
    with pytest.raises(NotSignedOff):
        certificate_figures(person, program, *YEAR)


def test_with_the_gate_off_there_is_no_window_until_the_event_closes():
    program, event, session = program_and_session("No windows")
    person = make_person()
    a_year_on = timezone.now() + datetime.timedelta(days=365)
    assert submission_allowed(person, session, at=a_year_on)
    assert window_state(person, session, at=a_year_on) == STATE_OPEN
    with pytest.raises(ReopeningRefused):
        request_reopening(person, session)  # nothing to reopen: it never closed
    event.status = "closed"
    event.save()
    assert not submission_allowed(person, session)
    assert window_state(person, session) == STATE_EVENT_CLOSED


# --- Turned on: the gate and the windows resume unchanged -------------------------------------


def test_turning_the_gate_on_restores_the_gate_and_the_window_rules():
    program, event, session = program_and_session("Gated", require_evaluation_for_credit=True)
    person = make_person()
    teams_row(event, person, 0, 60)

    b = credit_breakdown(person, event)
    assert b.attendance_credits == D("0.00")  # attended, not evaluated: nothing
    assert b.sessions[0].blocks_certificate is False  # and nothing to sign off for credit

    evaluate(person, session)
    assert credit_breakdown(person, event).attendance_credits == D("1.00")

    # The windows: a week, then a request, as before.
    other = make_person()
    a_month_on = timezone.now() + datetime.timedelta(days=30)
    assert not submission_allowed(other, session, at=a_month_on)
    assert window_state(other, session, at=a_month_on) == STATE_CAN_REQUEST
    assert request_reopening(other, session, reason="Was away") is not None


def test_the_switch_is_per_program_so_two_events_in_a_program_follow_one_rule():
    program, first, s1 = program_and_session("One rule")
    second = make_event(program=program, start=EVENT_START + datetime.timedelta(days=14), minutes=60, credits="1.00", sessions=0)
    s2 = make_session(second, minutes=60)
    person = make_person()
    teams_row(first, person, 0, 60)
    teams_row(second, person, 14 * 24 * 60, 14 * 24 * 60 + 60)
    assert credit_breakdown(person, first).attendance_credits == credit_breakdown(person, second).attendance_credits == D("1.00")
    program.require_evaluation_for_credit = True
    program.save()
    first.refresh_from_db(); second.refresh_from_db()
    assert credit_breakdown(person, first).attendance_credits == credit_breakdown(person, second).attendance_credits == D("0.00")


# --- The credits page: an invitation, not a warning ---------------------------------------------


@pytest.fixture
def ada():
    AllowedDomain.objects.create(domain="mcgill.ca")
    return make_person(given="Ada", family="Lovelace", email="ada@mcgill.ca")


def signed_in_client(email):
    from signin.tests.test_signin import ask_for_link, last_link_token

    client = Client()
    ask_for_link(client, email)
    client.get(reverse("signin:redeem", args=[last_link_token()]))
    return client


def test_the_credits_page_invites_rather_than_warns_when_the_gate_is_off(ada):
    program, event, session = program_and_session("Invitation")
    teams_row(event, ada, 0, 60)
    html = signed_in_client("ada@mcgill.ca").get(reverse("signin:me")).content.decode()
    assert "Help us improve" in html and "Evaluate this session" in html
    assert "Your credit stands on its own" in html
    assert "Evaluations to complete" not in html and "released when its evaluation" not in html
    assert 'class="pending"' not in html.split("<h2>Emergency")[0] if "<h2>Emergency" in html else True
    assert "1.00" in html  # the figure, with nothing attached to it


def test_the_credits_page_keeps_the_stronger_wording_when_the_gate_is_on(ada):
    program, event, session = program_and_session("Stern", require_evaluation_for_credit=True)
    teams_row(event, ada, 0, 60)
    html = signed_in_client("ada@mcgill.ca").get(reverse("signin:me")).content.decode()
    assert "Evaluations to complete" in html and "released when its evaluation is complete" in html
    assert "Help us improve" not in html


def test_with_the_gate_off_a_complete_evaluation_stays_editable_until_the_event_closes(ada):
    program, event, session = program_and_session("Editable")
    client = signed_in_client("ada@mcgill.ca")
    url = reverse("credits:evaluate", args=[session.pk])
    client.post(url, {"q_relevance": "3", "q_commercial_bias": "1", "minutes": "60", "attestation": "1", "action": "submit"})
    submission = EvaluationSubmission.objects.get(person=ada, session=session)
    assert submission.is_complete
    page = client.get(url).content.decode()
    assert "you can still change it until the event closes" in page and "disabled" not in page
    client.post(url, {"q_relevance": "5", "q_commercial_bias": "1", "minutes": "60", "attestation": "1", "action": "submit"})
    assert submission.responses.get(question_key="relevance").rating == 5
    event.status = "closed"
    event.save()
    page = client.get(url).content.decode()
    assert "shown below as it was asked" in page and "disabled" in page  # final once the event closes
    other = make_person(email="grace@mcgill.ca")
    assert "can no longer be changed" in signed_in_client("grace@mcgill.ca").get(url).content.decode()


# --- The overall-activity evaluation -------------------------------------------------------------


def test_the_overall_activity_evaluation_is_offered_per_event_from_the_program_form(ada):
    from credits.activity import activities_to_evaluate
    from credits.models import EvaluationForm, EvaluationFormVersion, EvaluationQuestion

    program, event, session = program_and_session("Activity")
    form = EvaluationForm.objects.create(program=program, name="Overall activity", status="active")
    version = EvaluationFormVersion.objects.create(form=form, number=1)
    EvaluationQuestion.objects.create(version=version, question_key="overall", kind="likert_5", prompt="Overall, the day was worthwhile")
    EvaluationQuestion.objects.create(version=version, question_key="objectives_met", kind="per_objective", prompt="Met: {objective}")
    program.activity_evaluation_form = form
    program.save()
    teams_row(event, ada, 0, 60)

    assert activities_to_evaluate(ada) == [event]
    client = signed_in_client("ada@mcgill.ca")
    html = client.get(reverse("signin:me")).content.decode()
    assert "Evaluate the overall activity" in html

    url = reverse("credits:evaluate_event", args=[event.pk])
    page = client.get(url).content.decode()
    assert "the day as a whole" in page and "Overall, the day was worthwhile" in page
    assert "Met:" not in page  # per-objective questions have nothing to expand to at this level
    assert "How many minutes" not in page  # no attendance claim on an activity evaluation
    client.post(url, {"q_overall": "5", "action": "submit"})
    submission = EvaluationSubmission.objects.get(person=ada, event=event, session=None)
    assert submission.is_complete and submission.is_activity and submission.form_version == version
    assert activities_to_evaluate(ada) == []
    assert credit_breakdown(ada, event).attendance_credits == D("1.00")  # unchanged: it never gates

    with pytest.raises(ValidationError):
        EvaluationSubmission(person=ada, form_version=version).full_clean()  # neither session nor event


# --- The accreditation statement ------------------------------------------------------------------


def test_the_accreditation_statement_is_part_of_the_figures_and_prints_on_the_certificate():
    from certificates.render import certificate_html
    from certificates.tests.test_models import issue, line

    program, event, session = program_and_session(
        "Statement", accreditation_statement="This activity is an Accredited Group Learning Activity (Section 1) as defined by the Maintenance of Certification Program of the Royal College of Physicians and Surgeons of Canada, approved by McGill CPD."
    )
    person = make_person()
    teams_row(event, person, 0, 60)
    confirm_event(event, user=make_signer(program))
    figures = certificate_figures(person, program, *YEAR)
    assert figures.accreditation_statement.startswith("This activity is an Accredited Group Learning Activity")

    certificate = issue(person, program=program, accreditation_statement=figures.accreditation_statement)
    line(certificate, event)
    program.accreditation_statement = "Changed later."
    program.save()
    html = certificate_html(certificate)
    assert "Accredited Group Learning Activity" in html and "Changed later" not in html
    assert person.full_name in html and "Verification code" in html
