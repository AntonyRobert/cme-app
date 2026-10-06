from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction

from attendance.aggregation import MinutesSource
from attendance.tests.factories import manual_row, supersede, teams_row
from core.models import ImmutableRowError
from credits.models import EvaluationResponse, EvaluationSubmission
from credits.rules import (
    adjustment_credits,
    computed_credits,
    credit_breakdown,
    creditable_minutes,
    evaluation_gate,
    event_credits,
    round_credits,
)
from people.models import Person
from people.tests.factories import make_person
from rounds.models import LearningObjective
from rounds.tests.factories import at, make_event, make_session

from .factories import adjust, evaluate

pytestmark = pytest.mark.django_db

D = Decimal


@pytest.fixture
def event():
    event = make_event(credits="1.00")
    for _ in range(3):
        make_session(event)
    return event


@pytest.fixture
def person():
    return make_person()


def sessions(event):
    return list(event.sessions.order_by("position"))


# --- Rounding ----------------------------------------------------------------


@pytest.mark.parametrize(
    "hours, expected",
    [
        ("0", "0.00"),
        ("0.24", "0.00"),
        ("0.25", "0.25"),
        ("0.49", "0.25"),
        ("0.50", "0.50"),
        ("0.74", "0.50"),
        ("0.99", "0.75"),
        ("0.9999", "0.75"),
        ("1", "1.00"),
        ("1.24", "1.00"),
        ("1.26", "1.25"),
        ("2.75", "2.75"),
        ("-1", "0.00"),
    ],
)
def test_round_credits_rounds_down_to_the_quarter(hours, expected):
    result = round_credits(D(hours))
    assert result == D(expected)
    assert isinstance(result, Decimal)
    assert result.as_tuple().exponent == -2


def test_round_credits_never_rounds_up():
    for minutes in range(0, 181):
        rounded = round_credits(D(minutes) / 60)
        assert rounded <= D(minutes) / 60
        assert rounded % D("0.25") == 0
        assert D(minutes) / 60 - rounded < D("0.25")


# --- The evaluation gate -----------------------------------------------------


def test_no_evaluation_no_gate(event, person):
    assert evaluation_gate(person, event) is False


def test_one_complete_evaluation_passes(event, person):
    evaluate(person, sessions(event)[1])
    assert evaluation_gate(person, event) is True


def test_an_incomplete_evaluation_does_not_pass(event, person):
    evaluate(person, sessions(event)[0], complete=False)
    assert evaluation_gate(person, event) is False


def test_an_evaluation_of_another_event_does_not_pass(event, person):
    evaluate(person, make_session(make_event()))
    assert evaluation_gate(person, event) is False


def test_someone_elses_evaluation_does_not_pass(event, person):
    evaluate(make_person(), sessions(event)[0])
    assert evaluation_gate(person, event) is False


# --- Credit for an event -----------------------------------------------------


@pytest.mark.parametrize(
    "attended, expected",
    [(60, "1.00"), (59, "0.75"), (45, "0.75"), (44, "0.50"), (15, "0.25"), (14, "0.00"), (0, "0.00")],
)
def test_credit_follows_attended_minutes_rounded_down(event, person, attended, expected):
    if attended:
        teams_row(event, person, 0, attended)
    else:
        manual_row(event, person, minutes=0)
    evaluate(person, sessions(event)[0])
    assert computed_credits(person, event) == D(expected)
    assert event_credits(person, event) == D(expected)


def test_no_credit_without_the_evaluation_however_long_they_stayed(event, person):
    teams_row(event, person, 0, 60)
    breakdown = credit_breakdown(person, event)
    assert breakdown.gate_passed is False
    assert breakdown.time.minutes == 60
    assert breakdown.computed_credits == D("0.00")


def test_no_credit_for_the_evaluation_without_attendance(event, person):
    evaluate(person, sessions(event)[0], minutes=0)
    manual_row(event, person, minutes=0, reason="Signed in, left at once")
    assert event_credits(person, event) == D("0.00")


def test_credit_is_capped_at_what_the_event_is_accredited_for(person):
    event = make_event(minutes=120, credits="1.50")
    teams_row(event, person, 0, 120)
    evaluate(person, make_session(event))
    assert computed_credits(person, event) == D("1.50")


def test_cap_applies_after_rounding(person):
    event = make_event(minutes=60, credits="0.50")
    teams_row(event, person, 0, 59)
    evaluate(person, make_session(event))
    assert computed_credits(person, event) == D("0.50")


def test_credit_is_per_event_not_per_session(event, person):
    """Sitting through all three sessions is one hour, not three credits."""
    teams_row(event, person, 0, 60)
    for session in sessions(event):
        evaluate(person, session)
    assert event_credits(person, event) == D("1.00")


def test_overlapping_connections_do_not_inflate_credit(event, person):
    teams_row(event, person, 0, 30)
    teams_row(event, person, 0, 30)
    evaluate(person, sessions(event)[0])
    assert event_credits(person, event) == D("0.50")


def test_a_correction_changes_the_credit(event, person):
    wrong = teams_row(event, person, 0, 20)
    evaluate(person, sessions(event)[0])
    assert event_credits(person, event) == D("0.25")
    supersede([wrong], manual_row(event, person, minutes=60, reason="Teams lost the rejoin"))
    assert event_credits(person, event) == D("1.00")


def test_running_over_is_fixed_once_on_the_event(person):
    event = make_event(minutes=60, credits="1.50")
    teams_row(event, person, 0, 90)
    evaluate(person, make_session(event))
    assert event_credits(person, event) == D("1.00")
    event.actual_end_at = at(90)
    event.save()
    assert event_credits(person, event) == D("1.50")


def test_credit_is_always_a_decimal_with_two_places(event, person):
    teams_row(event, person, 0, 50)
    evaluate(person, sessions(event)[0])
    adjust(person, event, "0.25")
    breakdown = credit_breakdown(person, event)
    for value in (breakdown.computed_credits, breakdown.adjustment_credits, breakdown.credits):
        assert isinstance(value, Decimal)
        assert value.as_tuple().exponent == -2


def test_credit_follows_a_merge(event):
    survivor, duplicate = make_person(), make_person()
    teams_row(event, duplicate, 0, 60)
    evaluate(survivor, sessions(event)[0], minutes=20)
    # Before the merge only the self-report is theirs: 20 minutes.
    assert event_credits(survivor, event) == D("0.25")
    Person.objects.filter(pk=duplicate.pk).update(merged_into=survivor)
    assert event_credits(survivor, event) == D("1.00")


# --- Self-reported minutes ---------------------------------------------------


def test_recorded_minutes_win_over_the_self_report(event, person):
    teams_row(event, person, 0, 30)
    evaluate(person, sessions(event)[0], minutes=60)
    time = creditable_minutes(person, event)
    assert (time.minutes, time.source) == (30, MinutesSource.TEAMS)
    assert (time.recorded_minutes, time.self_reported_minutes) == (30, 60)


def test_self_report_is_the_fallback_when_there_is_no_attendance_row(event, person):
    for session in sessions(event):
        evaluate(person, session, minutes=20)
    time = creditable_minutes(person, event)
    assert (time.minutes, time.source) == (60, MinutesSource.SELF_REPORTED)
    assert time.recorded_minutes is None
    assert time.needs_review is True
    assert event_credits(person, event) == D("1.00")


def test_self_report_cannot_exceed_the_event(event, person):
    evaluate(person, sessions(event)[0], minutes=600)
    assert creditable_minutes(person, event).minutes == 65  # the credit window


def test_recorded_zero_is_not_replaced_by_the_self_report(event, person):
    teams_row(event, person, -30, -10)  # only ever in the lobby
    evaluate(person, sessions(event)[0], minutes=60)
    time = creditable_minutes(person, event)
    assert (time.minutes, time.source, time.needs_review) == (0, MinutesSource.TEAMS, True)
    assert event_credits(person, event) == D("0.00")


@pytest.mark.parametrize(
    "recorded, claimed, flagged",
    [(60, 60, False), (45, 60, False), (44, 60, True), (10, 60, True), (60, 44, False)],
)
def test_claiming_over_15_minutes_more_than_was_recorded_is_flagged(event, person, recorded, claimed, flagged):
    teams_row(event, person, 0, recorded)
    evaluate(person, sessions(event)[0], minutes=claimed)
    assert creditable_minutes(person, event).needs_review is flagged


def test_evaluating_one_session_of_three_is_not_a_discrepancy(event, person):
    """Self-reports are per session; recorded minutes are for the whole event."""
    teams_row(event, person, 0, 60)
    evaluate(person, sessions(event)[0], minutes=20)
    assert creditable_minutes(person, event).needs_review is False


def test_attendance_without_any_evaluation_is_not_flagged(event, person):
    teams_row(event, person, 0, 60)
    time = creditable_minutes(person, event)
    assert (time.self_reported_minutes, time.needs_review) == (None, False)


def test_nothing_at_all(event, person):
    time = creditable_minutes(person, event)
    assert (time.minutes, time.source, time.needs_review) == (0, None, False)
    assert event_credits(person, event) == D("0.00")


# --- Adjustments -------------------------------------------------------------


def test_adjustments_are_a_ledger_added_to_the_computed_credit(event, person):
    teams_row(event, person, 0, 30)
    evaluate(person, sessions(event)[0])
    adjust(person, event, "0.50")
    adjust(person, event, "-0.25")
    breakdown = credit_breakdown(person, event)
    assert breakdown.computed_credits == D("0.50")
    assert breakdown.adjustment_credits == D("0.25")
    assert breakdown.credits == D("0.75")
    assert adjustment_credits(person, make_event()) == D("0.00")


def test_an_adjustment_can_grant_credit_the_gate_withheld(event, person):
    teams_row(event, person, 0, 60)
    adjust(person, event, "1.00", reason="Evaluation form was down; chair approved")
    breakdown = credit_breakdown(person, event)
    assert (breakdown.computed_credits, breakdown.credits) == (D("0.00"), D("1.00"))


def test_credit_never_goes_below_zero(event, person):
    adjust(person, event, "-2.00")
    assert event_credits(person, event) == D("0.00")


def test_an_adjustment_needs_a_reason_and_a_quarter_step_amount(event, person):
    with pytest.raises(IntegrityError), transaction.atomic():
        adjust(person, event, "0.25", reason="")
    with pytest.raises(IntegrityError), transaction.atomic():
        adjust(person, event, "0.10")
    with pytest.raises(IntegrityError), transaction.atomic():
        adjust(person, event, "0")


def test_adjustments_cannot_be_edited_or_deleted(event, person):
    row = adjust(person, event, "0.25")
    row.delta_credits = D("5.00")
    with pytest.raises(ImmutableRowError):
        row.save()
    with pytest.raises(ImmutableRowError):
        row.delete()


# --- Evaluation rows ---------------------------------------------------------


def test_one_submission_per_person_per_session(event, person):
    evaluate(person, sessions(event)[0])
    with pytest.raises(IntegrityError), transaction.atomic():
        evaluate(person, sessions(event)[0])
    evaluate(person, sessions(event)[1])


def test_attestation_is_required(event, person):
    with pytest.raises(IntegrityError), transaction.atomic():
        EvaluationSubmission.objects.create(
            person=person,
            session=sessions(event)[0],
            self_reported_minutes=20,
            attestation=False,
        )


def test_one_answer_per_question_including_general_questions(event, person):
    session = sessions(event)[0]
    objective = LearningObjective.objects.create(session=session, position=1, text="Explain X")
    submission = evaluate(person, session)
    make = EvaluationResponse.objects.create
    make(submission=submission, objective=objective, question_key="objective_met", rating=4)
    make(submission=submission, question_key="overall", rating=5)
    with pytest.raises(IntegrityError), transaction.atomic():
        make(submission=submission, objective=objective, question_key="objective_met", rating=1)
    with pytest.raises(IntegrityError), transaction.atomic():
        make(submission=submission, question_key="overall", rating=1)


def test_responses_are_reached_through_their_owner(event, person):
    submission = evaluate(person, sessions(event)[0])
    EvaluationResponse.objects.create(submission=submission, question_key="overall", rating=5)
    assert EvaluationResponse.objects.for_person(person).count() == 1
    assert EvaluationResponse.objects.for_person(make_person()).count() == 0
