import datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from core.models import ImmutableRowError
from people.tests.factories import make_person
from rounds.models import (
    CREDIT_WINDOW_GRACE,
    COIDeclaration,
    SessionPresenter,
    end_of_academic_year,
)

from .factories import EVENT_START, at, make_event, make_session

pytestmark = pytest.mark.django_db


def declare(person, has_conflict=False, details="", **extra):
    return COIDeclaration.objects.create(
        person=person,
        has_conflict=has_conflict,
        details=details,
        disclosure_text_version="2026-1",
        **extra,
    )


# --- Events ------------------------------------------------------------------


def test_title_defaults_to_the_series_name(settings):
    settings.SERIES_NAME = "Test Rounds"
    assert make_event().title == "Test Rounds"


def test_credit_window_uses_the_schedule_with_grace_at_the_start():
    event = make_event()
    assert event.credit_window() == (EVENT_START - CREDIT_WINDOW_GRACE, at(60))


def test_actual_times_override_the_schedule():
    event = make_event(actual_start_at=at(10), actual_end_at=at(80))
    assert event.credit_window() == (at(10) - CREDIT_WINDOW_GRACE, at(80))


def test_only_one_actual_time_may_be_set():
    event = make_event(actual_end_at=at(75))
    assert event.credit_window() == (EVENT_START - CREDIT_WINDOW_GRACE, at(75))


@pytest.mark.parametrize("credits", ["0.10", "1.30", "-0.25"])
def test_accredited_credits_must_be_a_non_negative_quarter_multiple(credits):
    with pytest.raises(IntegrityError), transaction.atomic():
        make_event(credits=credits)
    event = make_event()
    event.accredited_credits = Decimal(credits)
    with pytest.raises(ValidationError):
        event.full_clean()


@pytest.mark.parametrize("credits", ["0.00", "0.25", "1.00", "1.75"])
def test_quarter_multiples_are_accepted(credits):
    make_event(credits=credits).full_clean()


def test_event_cannot_end_before_it_starts():
    with pytest.raises(IntegrityError), transaction.atomic():
        make_event(minutes=-10)
    event = make_event(actual_end_at=at(-30))
    with pytest.raises(ValidationError):
        event.full_clean()


def test_session_position_is_unique_within_an_event():
    event = make_event()
    make_session(event, position=1)
    with pytest.raises(IntegrityError), transaction.atomic():
        make_session(event, position=1)
    make_session(make_event(), position=1)


# --- Conflict of interest ----------------------------------------------------


@pytest.mark.parametrize(
    "today, expected",
    [
        (datetime.date(2026, 10, 6), datetime.date(2027, 6, 30)),
        (datetime.date(2027, 6, 30), datetime.date(2027, 6, 30)),
        (datetime.date(2027, 7, 1), datetime.date(2028, 6, 30)),
        (datetime.date(2027, 1, 15), datetime.date(2027, 6, 30)),
    ],
)
def test_end_of_academic_year(today, expected):
    assert end_of_academic_year(today) == expected


def test_a_conflict_needs_details():
    person = make_person()
    with pytest.raises(IntegrityError), transaction.atomic():
        declare(person, has_conflict=True, details="")
    declare(person, has_conflict=True, details="Consultant for Acme Devices")


def test_a_declaration_cannot_be_edited_or_deleted():
    declaration = declare(make_person())
    declaration.has_conflict = True
    declaration.details = "changed my mind"
    with pytest.raises(ImmutableRowError):
        declaration.save()
    with pytest.raises(ImmutableRowError):
        declaration.delete()


def test_presenter_picks_up_their_current_declaration():
    person = make_person()
    declare(person, valid_until=datetime.date(2025, 6, 30))  # expired
    current = declare(person, valid_until=datetime.date(2027, 6, 30))
    declare(make_person(), valid_until=datetime.date(2027, 6, 30))  # someone else's
    session = make_session(make_event())
    link = SessionPresenter.objects.create(session=session, person=person)
    assert link.coi_declaration == current


def test_presenter_without_a_valid_declaration_is_left_blank():
    person = make_person()
    declare(person, valid_until=datetime.date(2025, 6, 30))
    link = SessionPresenter.objects.create(session=make_session(make_event()), person=person)
    assert link.coi_declaration is None


def test_a_later_declaration_does_not_replace_the_snapshot():
    person = make_person()
    original = declare(person, valid_until=datetime.date(2027, 6, 30))
    link = SessionPresenter.objects.create(session=make_session(make_event()), person=person)
    declare(person, has_conflict=True, details="New grant", valid_until=datetime.date(2027, 6, 30))
    link.position = 2
    link.save()
    link.refresh_from_db()
    assert link.coi_declaration == original


def test_presenter_cannot_carry_someone_elses_declaration():
    theirs = declare(make_person())
    link = SessionPresenter(
        session=make_session(make_event()), person=make_person(), coi_declaration=theirs
    )
    with pytest.raises(ValidationError):
        link.full_clean()


def test_a_session_can_have_co_presenters_but_not_the_same_one_twice():
    session = make_session(make_event())
    first = make_person()
    SessionPresenter.objects.create(session=session, person=first, position=1)
    SessionPresenter.objects.create(session=session, person=make_person(), position=2)
    with pytest.raises(IntegrityError), transaction.atomic():
        SessionPresenter.objects.create(session=session, person=first, position=3)
    assert session.presenters.count() == 2
