"""What a certificate prints: attendance and teaching apart, exact, never rounded."""
import datetime
from decimal import Decimal

import pytest

from attendance.tests.factories import teams_row
from certificates.figures import CertificateFigures, LineFigures, certificate_figures
from credits.tests.factories import evaluate
from people.tests.factories import make_person
from rounds.models import SessionPresenter
from rounds.tests.factories import EVENT_START, make_event, make_session

pytestmark = pytest.mark.django_db

D = Decimal
YEAR = (datetime.date(2026, 1, 1), datetime.date(2026, 12, 31))


def three_talks(start=EVENT_START):
    event = make_event(start=start, minutes=180, credits="3.00", sessions=0)
    for _ in range(3):
        make_session(event, minutes=60)
    return event, list(event.sessions.order_by("start_at"))


def test_a_presenter_who_also_attends_gets_both_kinds_on_separate_lines():
    person = make_person()
    first_event, (a1, a2, a3) = three_talks()
    second_event, (b1, b2, b3) = three_talks(EVENT_START + datetime.timedelta(days=14))
    SessionPresenter.objects.create(session=a2, person=person)
    teams_row(first_event, person, 0, 180)
    evaluate(person, a1)
    evaluate(person, a3)
    two_weeks = 14 * 24 * 60  # teams_row counts minutes from the first event's start
    teams_row(second_event, person, two_weeks, two_weeks + 59)
    evaluate(person, b1)

    figures = certificate_figures(person, *YEAR)

    first, second = figures.lines
    assert (first.attendance_credits, first.teaching_credits) == (D("2.00"), D("1.00"))
    assert first.session_titles == [a1.title, a3.title]
    assert first.presented_session_titles == [a2.title]
    assert (first.attended_minutes, first.teaching_minutes) == (120, 60)
    assert (second.attendance_credits, second.teaching_credits) == (D("0.98"), D("0.00"))
    # The year: attendance 2.00 + 0.98 exactly, teaching 1.00.
    assert (figures.attendance_credits, figures.teaching_credits) == (D("2.98"), D("1.00"))
    assert figures.total_credits == D("3.98")
    assert figures.attendance_credits == sum(line.attendance_credits for line in figures.lines)


def test_a_line_has_no_single_blended_credit_figure():
    names = set(LineFigures.__dataclass_fields__)
    assert {"attendance_credits", "teaching_credits"} <= names
    assert "credits" not in names and "total_credits" not in names


def line(attendance, teaching):
    return LineFigures(
        event=None, event_title="", event_date=None, session_titles=[], attended_minutes=0,
        minutes_source=None, attendance_computed=D(attendance), attendance_adjustment=D("0"),
        attendance_credits=D(attendance), presented_session_titles=[], teaching_minutes=0,
        teaching_computed=D(teaching), teaching_adjustment=D("0"), teaching_credits=D(teaching),
    )


def test_each_kind_is_the_exact_sum_and_the_total_is_their_sum():
    figures = CertificateFigures(lines=[line("1.30", "0.50"), line("1.30", "0.00")])
    assert (figures.attendance_credits, figures.teaching_credits) == (D("2.60"), D("0.50"))
    assert figures.total_credits == D("3.10")


def test_events_with_no_credit_of_either_kind_are_left_off():
    person = make_person()
    event, talks = three_talks()
    teams_row(event, person, 0, 180)  # attended, evaluated nothing
    assert certificate_figures(person, *YEAR).lines == []


def test_events_outside_the_period_are_left_off():
    person = make_person()
    event, (a1, _, _) = three_talks()
    SessionPresenter.objects.create(session=a1, person=person)
    assert len(certificate_figures(person, *YEAR).lines) == 1
    assert certificate_figures(person, datetime.date(2027, 1, 1), datetime.date(2027, 12, 31)).lines == []
