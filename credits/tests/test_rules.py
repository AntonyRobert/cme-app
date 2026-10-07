from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction

from attendance.aggregation import MinutesSource, sessions_attended
from attendance.tests.factories import manual_row, supersede, teams_row
from core.models import ImmutableRowError
from credits.models import EvaluationResponse, EvaluationSubmission
from credits.rules import (
    REVIEW_CLAIMS_MORE,
    REVIEW_OVER_SESSION_LENGTH,
    REVIEW_SELF_REPORTED_ONLY,
    adjustment_credits,
    computed_credits,
    credit_breakdown,
    evaluation_gate,
    credits_for_minutes,
    event_credits,
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
    """Three one-hour sessions, 0-60, 60-120 and 120-180, worth 3.00 credits."""
    event = make_event(minutes=180, credits="3.00", sessions=0)
    for _ in range(3):
        make_session(event, minutes=60)
    return event


@pytest.fixture
def person():
    return make_person()


def sessions(event):
    return list(event.sessions.order_by("start_at"))


def standing(person, event):
    """(credited minutes, computed credits, credits) in one tuple."""
    b = credit_breakdown(person, event)
    return b.credited_minutes, b.attendance_computed, b.attendance_credits


# --- Credit is hours attended ------------------------------------------------


@pytest.mark.parametrize(
    "minutes, expected",
    [
        (0, "0.00"),
        (1, "0.01"),
        (5, "0.08"),
        (14, "0.23"),
        (15, "0.25"),
        (30, "0.50"),
        (45, "0.75"),
        (59, "0.98"),
        (60, "1.00"),
        (65, "1.08"),
        (90, "1.50"),
        (180, "3.00"),
        (-1, "0.00"),
    ],
)
def test_credits_for_minutes_is_minutes_over_sixty(minutes, expected):
    result = credits_for_minutes(minutes)
    assert result == D(expected)
    assert isinstance(result, Decimal)
    assert result.as_tuple().exponent == -2


def test_the_hundredths_are_cut_never_rounded_up():
    for minutes in range(0, 181):
        credits = credits_for_minutes(minutes)
        assert credits <= D(minutes) / 60
        assert D(minutes) / 60 - credits < D("0.01")


# --- Minutes count as recorded, however few, once the form is filled in -----


def test_five_minutes_of_a_session_count_for_five_minutes_once_evaluated(event, person):
    teams_row(event, person, 0, 5)
    assert standing(person, event) == (0, D("0.00"), D("0.00"))  # not evaluated yet
    evaluate(person, sessions(event)[0])
    assert standing(person, event) == (5, D("0.08"), D("0.08"))  # 5/60 of a credit


def test_a_few_minutes_of_one_talk_add_to_a_whole_other_talk(event, person):
    """60 + 5 = 65 minutes: 1.08 credits."""
    teams_row(event, person, 0, 60)
    teams_row(event, person, 60, 65)
    for session in sessions(event)[:2]:
        evaluate(person, session)
    assert standing(person, event) == (65, D("1.08"), D("1.08"))


def test_59_minutes_of_a_60_minute_session_is_59_sixtieths_of_a_credit(person):
    event = make_event(minutes=60, credits="1.00")
    teams_row(event, person, 1, 60)
    evaluate(person, sessions(event)[0])
    assert standing(person, event) == (59, D("0.98"), D("0.98"))


def test_joining_early_makes_up_for_leaving_early(person):
    """The grace before the start is real minutes: 11:55 to 12:55 is the hour."""
    event = make_event(minutes=60, credits="1.00")
    teams_row(event, person, -5, 55)
    evaluate(person, sessions(event)[0])
    assert standing(person, event) == (60, D("1.00"), D("1.00"))


def test_grace_does_not_exceed_the_session(person):
    event = make_event(minutes=60, credits="1.00")
    teams_row(event, person, -5, 65)
    evaluate(person, sessions(event)[0])
    assert standing(person, event)[0] == 60


# --- The gate: per session ---------------------------------------------------


def test_gate_is_per_session(event, person):
    first, second, third = sessions(event)
    evaluate(person, second)
    assert evaluation_gate(person, second) is True
    assert evaluation_gate(person, first) is False
    assert evaluation_gate(person, third) is False


def test_an_incomplete_evaluation_does_not_unlock(event, person):
    evaluate(person, sessions(event)[0], complete=False)
    assert evaluation_gate(person, sessions(event)[0]) is False


def test_someone_elses_evaluation_does_not_unlock(event, person):
    evaluate(make_person(), sessions(event)[0])
    assert evaluation_gate(person, sessions(event)[0]) is False


def test_evaluating_one_session_does_not_claim_credit_for_three(event, person):
    """The reason the gate is per session."""
    teams_row(event, person, 0, 180)
    evaluate(person, sessions(event)[0])
    b = credit_breakdown(person, event)
    assert b.minutes == 180  # all attended
    assert [s.credited_minutes for s in b.sessions] == [60, 0, 0]
    assert (b.attendance_computed, b.attendance_credits) == (D("1.00"), D("1.00"))


def test_credit_grows_as_each_session_is_evaluated(event, person):
    teams_row(event, person, 0, 180)
    first, second, third = sessions(event)
    assert event_credits(person, event) == D("0.00")
    evaluate(person, first)
    assert event_credits(person, event) == D("1.00")
    evaluate(person, third)
    assert event_credits(person, event) == D("2.00")
    evaluate(person, second)
    assert event_credits(person, event) == D("3.00")


def test_credit_appears_after_a_late_evaluation(event, person):
    """Credit is a moving target: nothing about attendance changes, only the gate."""
    teams_row(event, person, 0, 60)
    before = credit_breakdown(person, event)
    assert (before.minutes, before.attendance_credits) == (60, D("0.00"))
    evaluate(person, sessions(event)[0], submitted_at=event.end_at.replace(year=2030))
    after = credit_breakdown(person, event)
    assert (after.minutes, after.attendance_credits) == (60, D("1.00"))


def test_no_credit_for_an_evaluated_session_that_was_not_attended(event, person):
    teams_row(event, person, 0, 60)
    evaluate(person, sessions(event)[2])  # evaluated the one they skipped
    assert standing(person, event) == (0, D("0.00"), D("0.00"))


def test_part_of_a_session_earns_part_credit_once_evaluated(event, person):
    teams_row(event, person, 60, 90)  # half of the second session
    evaluate(person, sessions(event)[1])
    assert standing(person, event) == (30, D("0.50"), D("0.50"))


# --- Credit follows the minutes ---------------------------------------------


def test_three_20_minute_sessions_make_one_credit(person):
    event = make_event(minutes=60, credits="1.00", sessions=0)
    for _ in range(3):
        make_session(event, minutes=20)
    teams_row(event, person, 0, 60)
    for session in sessions(event):
        evaluate(person, session)
    assert standing(person, event) == (60, D("1.00"), D("1.00"))


@pytest.mark.parametrize(
    "attended, expected",
    [(60, "1.00"), (59, "0.98"), (45, "0.75"), (44, "0.73"), (15, "0.25"), (14, "0.23"), (5, "0.08")],
)
def test_credit_follows_attended_minutes(person, attended, expected):
    event = make_event(minutes=60, credits="1.00")
    if attended:
        teams_row(event, person, 0, attended)
    else:
        manual_row(event, person, minutes=0)
    evaluate(person, sessions(event)[0])
    assert computed_credits(person, event) == D(expected)


def test_credit_is_capped_at_what_the_event_is_accredited_for(event, person):
    event.accredited_credits = D("2.00")
    event.save()
    teams_row(event, person, 0, 180)
    for session in sessions(event):
        evaluate(person, session)
    assert computed_credits(person, event) == D("2.00")


def test_overlapping_connections_do_not_inflate_credit(person):
    event = make_event(minutes=60, credits="1.00")
    teams_row(event, person, 0, 30)
    teams_row(event, person, 0, 30)
    evaluate(person, sessions(event)[0])
    assert event_credits(person, event) == D("0.50")


def test_a_correction_changes_the_credit(person):
    event = make_event(minutes=60, credits="1.00")
    wrong = teams_row(event, person, 0, 20)
    evaluate(person, sessions(event)[0])
    assert event_credits(person, event) == D("0.33")
    supersede([wrong], manual_row(event, person, minutes=60, reason="Teams lost the rejoin"))
    assert event_credits(person, event) == D("1.00")


def test_a_session_running_over_is_fixed_once_on_the_session(person):
    event = make_event(minutes=120, credits="1.50", sessions=0)
    session = make_session(event, start=0, minutes=60)
    teams_row(event, person, 0, 90)
    evaluate(person, session)
    assert event_credits(person, event) == D("1.00")
    session.end_at = at(90)
    session.save()
    assert event_credits(person, event) == D("1.50")


def test_credit_is_always_a_decimal_with_two_places(person):
    event = make_event(minutes=60, credits="1.00")
    teams_row(event, person, 0, 50)
    evaluate(person, sessions(event)[0])
    adjust(person, event, "0.25")
    b = credit_breakdown(person, event)
    for value in (b.attendance_computed, b.attendance_adjustment, b.attendance_credits):
        assert isinstance(value, Decimal)
        assert value.as_tuple().exponent == -2


def test_credit_follows_a_merge(event):
    survivor, duplicate = make_person(), make_person()
    teams_row(event, duplicate, 0, 60)
    evaluate(survivor, sessions(event)[0], minutes=20)
    # Before the merge only the self-report is theirs: 20 minutes.
    assert event_credits(survivor, event) == D("0.33")
    Person.objects.filter(pk=duplicate.pk).update(merged_into=survivor)
    assert event_credits(survivor, event) == D("1.00")


# --- Sessions attended -------------------------------------------------------


def test_sessions_attended_needs_half_the_session(event, person):
    first, second, third = sessions(event)
    teams_row(event, person, 0, 29)  # 29 of 60: under half
    teams_row(event, person, 60, 90)  # exactly half
    teams_row(event, person, 120, 180)
    assert sessions_attended(person, event) == [second, third]
    assert credit_breakdown(person, event).sessions_attended == [second, third]


def test_under_half_a_session_is_not_counted_as_attended(event, person):
    """So nobody is chased for an evaluation of a talk they caught the end of."""
    teams_row(event, person, 0, 60)
    teams_row(event, person, 100, 120)  # last 20 minutes of the second session
    assert sessions_attended(person, event) == [sessions(event)[0]]
    # They can still evaluate it and earn the 20 minutes, if they want to.
    evaluate(person, sessions(event)[1])
    assert credit_breakdown(person, event).credited_minutes == 20


def test_sessions_attended_counts_hours_only_rows(event, person):
    manual_row(event, person, minutes=40, session=sessions(event)[1])
    assert sessions_attended(person, event) == [sessions(event)[1]]


def test_sessions_attended_ignores_self_reports(event, person):
    evaluate(person, sessions(event)[0], minutes=60)
    assert sessions_attended(person, event) == []


# --- Self-reported minutes ---------------------------------------------------


def test_recorded_minutes_win_over_the_self_report(event, person):
    teams_row(event, person, 0, 30)
    evaluate(person, sessions(event)[0], minutes=60)
    b = credit_breakdown(person, event)
    first = b.sessions[0]
    assert (first.minutes, first.source, first.self_reported_minutes) == (30, MinutesSource.TEAMS, 60)
    assert b.attendance_credits == D("0.50")


def test_self_report_is_the_fallback_when_there_is_no_attendance_row(event, person):
    for session in sessions(event):
        evaluate(person, session, minutes=60)
    b = credit_breakdown(person, event)
    assert b.source == MinutesSource.SELF_REPORTED
    assert [s.minutes for s in b.sessions] == [60, 60, 60]
    assert b.review_reasons == (REVIEW_SELF_REPORTED_ONLY,)
    assert b.attendance_credits == D("3.00")


def test_self_report_cannot_exceed_the_session(event, person):
    evaluate(person, sessions(event)[0], minutes=600)
    assert credit_breakdown(person, event).sessions[0].minutes == 60


def test_self_report_only_covers_the_sessions_they_evaluated(event, person):
    evaluate(person, sessions(event)[1], minutes=60)
    b = credit_breakdown(person, event)
    assert [s.minutes for s in b.sessions] == [0, 60, 0]
    assert b.attendance_credits == D("1.00")


def test_recorded_zero_is_not_replaced_by_the_self_report(event, person):
    teams_row(event, person, -30, -10)  # only ever in the lobby
    evaluate(person, sessions(event)[0], minutes=60)
    b = credit_breakdown(person, event)
    assert (b.sessions[0].minutes, b.source) == (0, MinutesSource.TEAMS)
    assert b.review_reasons == (REVIEW_CLAIMS_MORE,)
    assert b.attendance_credits == D("0.00")


@pytest.mark.parametrize(
    "recorded, claimed, flagged",
    [(60, 60, False), (45, 60, False), (44, 60, True), (10, 60, True), (60, 44, False)],
)
def test_claiming_over_15_minutes_more_than_was_recorded_is_flagged(
    event, person, recorded, claimed, flagged
):
    teams_row(event, person, 0, recorded)
    evaluate(person, sessions(event)[0], minutes=claimed)
    assert credit_breakdown(person, event).needs_review is flagged


def test_the_comparison_is_per_session(event, person):
    """60 minutes of session two is not "more than recorded" for session one."""
    teams_row(event, person, 60, 120)
    evaluate(person, sessions(event)[1], minutes=60)
    assert credit_breakdown(person, event).needs_review is False


def test_rows_adding_up_to_more_than_a_session_are_flagged(event, person):
    teams_row(event, person, 0, 60)
    manual_row(event, person, minutes=30, reason="Entered twice by mistake")
    evaluate(person, sessions(event)[0])
    b = credit_breakdown(person, event)
    assert b.sessions[0].minutes == 60
    assert b.review_reasons == (REVIEW_OVER_SESSION_LENGTH,)
    assert b.attendance_credits == D("1.00")


def test_attendance_without_any_evaluation_is_not_flagged(event, person):
    teams_row(event, person, 0, 60)
    b = credit_breakdown(person, event)
    assert (b.self_reported_minutes, b.needs_review) == (None, False)


def test_nothing_at_all(event, person):
    b = credit_breakdown(person, event)
    assert (b.minutes, b.source, b.needs_review, b.attendance_credits) == (0, None, False, D("0.00"))


# --- Adjustments -------------------------------------------------------------


def test_adjustments_are_a_ledger_added_to_the_computed_credit(person):
    event = make_event(minutes=60, credits="1.00")
    teams_row(event, person, 0, 30)
    evaluate(person, sessions(event)[0])
    adjust(person, event, "0.50")
    adjust(person, event, "-0.25")
    b = credit_breakdown(person, event)
    assert (b.attendance_computed, b.attendance_adjustment, b.attendance_credits) == (D("0.50"), D("0.25"), D("0.75"))
    assert adjustment_credits(person, make_event()) == D("0.00")


def test_an_adjustment_can_grant_credit_the_gate_withheld(person):
    event = make_event(minutes=60, credits="1.00")
    teams_row(event, person, 0, 60)
    adjust(person, event, "1.00", reason="Evaluation form was down; chair approved")
    b = credit_breakdown(person, event)
    assert (b.attendance_computed, b.attendance_credits) == (D("0.00"), D("1.00"))


def test_credit_never_goes_below_zero(event, person):
    adjust(person, event, "-2.00")
    assert event_credits(person, event) == D("0.00")


def test_an_adjustment_needs_a_reason_and_cannot_be_zero(event, person):
    with pytest.raises(IntegrityError), transaction.atomic():
        adjust(person, event, "0.25", reason="")
    with pytest.raises(IntegrityError), transaction.atomic():
        adjust(person, event, "0")
    adjust(person, event, "0.02", reason="Top up to the hour; chair approved")


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
        from .factories import form_for

        EvaluationSubmission.objects.create(
            person=person,
            session=sessions(event)[0],
            form_version=form_for(sessions(event)[0]),
            self_reported_session_minutes=20,
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
    before = EvaluationResponse.objects.for_person(person).count()  # the required answers
    EvaluationResponse.objects.create(submission=submission, question_key="overall", rating=5)
    assert EvaluationResponse.objects.for_person(person).count() == before + 1
    assert EvaluationResponse.objects.for_person(make_person()).count() == 0
