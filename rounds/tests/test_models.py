import datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from core.models import ImmutableRowError
from people.tests.factories import make_person
from rounds.models import (
    COIDeclaration,
    RoundsEvent,
    Session,
    SessionPresenter,
    end_of_academic_year,
)

from .factories import at, make_event, make_session

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


def test_a_session_ends_an_hour_after_it_starts_by_default():
    event = make_event(minutes=180, sessions=0)
    session = Session(event=event, position=1, title="Talk", start_at=at(60))
    session.full_clean()
    session.save()
    assert session.end_at == at(120)
    assert (session.length_seconds, session.length_minutes) == (3600, 60)


def test_an_event_fills_in_its_end_and_date_from_its_start():
    event = RoundsEvent(start_at=at(0), accredited_credits=Decimal("3.00"))
    event.full_clean()
    event.save()
    assert event.end_at == at(180)
    assert event.date == at(0).date()


def test_a_session_with_no_start_follows_the_previous_one():
    event = make_event(minutes=180, sessions=0)
    first = Session(event=event, position=1, title="One")
    first.full_clean()
    first.save()
    second = Session(event=event, position=2, title="Two")
    second.full_clean()
    second.save()
    assert (first.start_at, first.end_at) == (at(0), at(60))
    assert (second.start_at, second.end_at) == (at(60), at(120))


def test_a_session_must_fall_inside_its_event():
    event = make_event(minutes=60, sessions=0)
    for start, end in ((-10, 30), (30, 70), (-5, 65)):
        session = Session(event=event, position=1, title="Talk", start_at=at(start), end_at=at(end))
        with pytest.raises(ValidationError) as err:
            session.full_clean()
        assert "start_at" in err.value.message_dict
    Session(event=event, position=1, title="Talk", start_at=at(0), end_at=at(60)).full_clean()


def test_a_session_must_end_after_it_starts():
    event = make_event(minutes=60, sessions=0)
    with pytest.raises(IntegrityError), transaction.atomic():
        Session.objects.create(event=event, position=1, title="Talk", start_at=at(30), end_at=at(10))


def test_sessions_of_one_event_may_not_overlap():
    event = make_event(minutes=180, sessions=0)
    make_session(event, start=0, minutes=60)
    make_session(event, start=60, minutes=60)  # touching is fine
    clash = Session(event=event, position=3, title="Talk", start_at=at(100), end_at=at(160))
    with pytest.raises(ValidationError) as err:
        clash.full_clean()
    assert "Overlaps" in str(err.value)
    Session(event=event, position=3, title="Talk", start_at=at(120), end_at=at(180)).full_clean()


def test_sessions_are_ordered_by_time():
    event = make_event(minutes=180, sessions=0)
    late = make_session(event, position=1, start=120, minutes=60)
    early = make_session(event, position=2, start=0, minutes=60)
    assert list(event.sessions.all()) == [early, late]


def test_a_closed_event_stays_closed():
    event = make_event(status=RoundsEvent.Status.CLOSED)
    event.status = RoundsEvent.Status.DRAFT
    with pytest.raises(ValidationError) as err:
        event.full_clean()
    assert "status" in err.value.message_dict
    with pytest.raises(ImmutableRowError):
        event.save()
    event.refresh_from_db()
    assert event.is_closed
    event.title = "Renamed"  # other edits are still fine
    event.save()


def test_an_open_event_can_move_forward_and_then_close():
    event = make_event(status=RoundsEvent.Status.DRAFT)
    for status in (RoundsEvent.Status.PUBLISHED, RoundsEvent.Status.HELD, RoundsEvent.Status.CLOSED):
        event.status = status
        event.full_clean()
        event.save()


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
        make_event(minutes=-10, sessions=0)
    event = make_event()
    event.end_at = at(-30)
    with pytest.raises(ValidationError):
        event.full_clean()


def test_session_position_is_unique_within_an_event():
    event = make_event(minutes=120, sessions=0)
    make_session(event, position=1, minutes=60)
    with pytest.raises(IntegrityError), transaction.atomic():
        make_session(event, position=1, minutes=60)
    make_session(make_event(sessions=0), position=1)


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
