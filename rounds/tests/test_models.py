import datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction

from core.models import ImmutableRowError
from people.tests.factories import make_person, make_staff
from audit.models import AuditLog
from rounds.coi import declare, declare_no_conflicts
from rounds.models import (
    COIDeclaration,
    COIResponse,
    LearningObjective,
    RoundsEvent,
    Session,
    SessionPresenter,
    coi_questions,
)

from programs.tests.factories import make_program

from .factories import at, make_event, make_session

pytestmark = pytest.mark.django_db


# --- Events ------------------------------------------------------------------


def test_title_and_credits_default_from_the_program():
    program = make_program("Named Program", series_name="Test Rounds",
                           default_accredited_credits=Decimal("2.50"))
    event = RoundsEvent(program=program, start_at=at(0))
    event.full_clean()
    event.save()
    assert (event.title, event.accredited_credits) == ("Test Rounds", Decimal("2.50"))
    assert make_event(program=program, credits="1.00").accredited_credits == Decimal("1.00")


def test_a_session_ends_an_hour_after_it_starts_by_default():
    event = make_event(minutes=180, sessions=0)
    session = Session(event=event, position=1, title="Talk", start_at=at(60))
    session.full_clean()
    session.save()
    assert session.end_at == at(120)
    assert (session.length_seconds, session.length_minutes) == (3600, 60)


def test_an_event_fills_in_its_end_and_date_from_its_start():
    event = RoundsEvent(program=make_program(), start_at=at(0), accredited_credits=Decimal("3.00"))
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


def test_blank_positions_take_the_next_number():
    event = make_event(minutes=180, sessions=0)
    first = Session.objects.create(event=event, title="One")
    second = Session.objects.create(event=event, title="Two")
    assert (first.position, second.position) == (1, 2)
    SessionPresenter.objects.create(session=first, person=make_person())
    SessionPresenter.objects.create(session=first, person=make_person())
    assert sorted(first.session_presenters.values_list("position", flat=True)) == [1, 2]
    LearningObjective.objects.create(session=first, text="a")
    LearningObjective.objects.create(session=first, position=7, text="b")
    LearningObjective.objects.create(session=first, text="c")
    assert list(first.objectives.values_list("position", flat=True)) == [1, 7, 8]


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


def check_deferred_constraints():
    """Deferred constraints are checked at commit; force the check now."""
    with connection.cursor() as cursor:
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


def test_session_position_is_unique_within_an_event():
    event = make_event(minutes=120, sessions=0)
    make_session(event, position=1, minutes=60)
    with pytest.raises(IntegrityError), transaction.atomic():
        make_session(event, position=1, minutes=60)
        check_deferred_constraints()
    make_session(make_event(sessions=0), position=1)


def test_positions_can_swap_within_one_transaction():
    event = make_event(minutes=120, sessions=0)
    first = make_session(event, position=1, minutes=60)
    second = make_session(event, position=2, minutes=60)
    with transaction.atomic():
        Session.objects.filter(pk=first.pk).update(position=2)
        Session.objects.filter(pk=second.pk).update(position=1)
        check_deferred_constraints()
    first.refresh_from_db()
    assert first.position == 2


# --- Conflict of interest ----------------------------------------------------

NOW = datetime.datetime(2026, 10, 6, 15, 0, tzinfo=datetime.timezone.utc)


def declare_none(person, **kwargs):
    kwargs.setdefault("declared_at", NOW)
    kwargs.setdefault("version", "2026-10")
    return declare_no_conflicts(person, **kwargs)


def declare_some(person, **kwargs):
    answers = {key: (False, "") for key, _ in coi_questions("2026-10")}
    answers["consulting"] = (True, "Advisory board, Acme Devices")
    kwargs.setdefault("declared_at", NOW)
    kwargs.setdefault("version", "2026-10")
    return declare(person, answers, **kwargs)


def test_no_conflicts_writes_an_explicit_no_to_every_question():
    declaration = declare_none(make_person())
    assert declaration.is_complete
    assert declaration.has_conflict is False
    assert declaration.summary == "no conflicts"
    assert [yes for _, yes, _, _ in declaration.rendered()] == [False] * 7
    assert declaration.responses.count() == 7


def test_an_unanswered_declaration_is_not_an_attested_no():
    blank = COIDeclaration.objects.create(person=make_person(), disclosure_text_version="2026-10")
    assert blank.responses.count() == 0
    assert blank.is_complete is False
    assert blank.summary == "incomplete"
    assert [yes for _, yes, _, _ in blank.rendered()] == [None] * 7


def test_an_incomplete_declaration_is_rejected():
    person = make_person()
    answers = {key: (False, "") for key, _ in coi_questions("2026-10")}
    del answers["employment"]
    with pytest.raises(ValidationError) as err:
        declare(person, answers, version="2026-10")
    assert "employment" in err.value.message_dict
    assert COIDeclaration.objects.filter(person=person).count() == 0  # nothing half-written


def test_an_answer_to_an_unknown_question_is_rejected():
    person = make_person()
    answers = {key: (False, "") for key, _ in coi_questions("2026-10")}
    answers["lottery_wins"] = (False, "")
    with pytest.raises(ValidationError) as err:
        declare(person, answers, version="2026-10")
    assert "lottery_wins" in err.value.message_dict


def test_a_yes_without_details_is_rejected():
    person = make_person()
    answers = {key: (False, "") for key, _ in coi_questions("2026-10")}
    answers["speaker_fees"] = (True, "   ")
    with pytest.raises(ValidationError) as err:
        declare(person, answers, version="2026-10")
    assert "speaker_fees_description" in err.value.message_dict  # the yes needs its description
    with pytest.raises(IntegrityError), transaction.atomic():
        COIResponse.objects.create(
            declaration=declare_none(person), question_key="speaker_fees", has_conflict=True, relationship_description=""
        )


def test_a_yes_with_details_is_recorded_and_summarised():
    declaration = declare_some(make_person())
    assert declaration.has_conflict is True
    assert declaration.summary == "1 conflict(s) declared"
    assert ("Consulting or advisory roles", True, "", "Advisory board, Acme Devices") in declaration.rendered()


def test_an_old_declaration_renders_with_its_own_versions_wording(settings):
    settings.COI_QUESTIONS = {
        **settings.COI_QUESTIONS,
        "2027-01": [
            ("research_funding", "Grants, including in kind"),  # reworded
            ("consulting", "Consulting or advisory roles"),
            ("speaker_fees", "Speaker fees or honoraria"),
            ("equity", "Equity or ownership"),
            ("employment", "Employment"),
            ("intellectual_property", "Intellectual property or royalties"),
            ("other", "Other relevant interests"),
            ("gifts", "Gifts or hospitality"),  # new question
        ],
    }
    old = declare_none(make_person(), version="2026-10")
    new = declare_no_conflicts(make_person(), version="2027-01")  # a program moved to it
    assert [text for text, _, _, _ in old.rendered()][0] == "Research funding or grants"
    assert len(old.rendered()) == 7
    assert old.is_complete  # judged against its own version, not the current one
    assert [text for text, _, _, _ in new.rendered()][0] == "Grants, including in kind"
    assert len(new.rendered()) == 8
    assert new.disclosure_text_version == "2027-01"


def test_a_declaration_under_an_unknown_version_is_refused():
    with pytest.raises(LookupError):
        declare_no_conflicts(make_person(), version="1999-01")
    stray = COIDeclaration(person=make_person(), disclosure_text_version="1999-01")
    with pytest.raises(ValidationError):
        stray.full_clean()


def test_a_declaration_is_logged_against_the_person_or_the_staff_member():
    person = make_person()
    declare_none(person)
    entry = AuditLog.objects.filter(action="coi.declared").latest("id")
    assert (entry.actor_person, entry.metadata["entered_by_staff"]) == (person, False)
    staff = make_staff()
    declare_some(person, user=staff)
    entry = AuditLog.objects.filter(action="coi.declared").latest("id")
    assert (entry.actor_user, entry.metadata["conflicts"]) == (staff, ["consulting"])


def test_a_declaration_cannot_be_edited_or_deleted():
    declaration = declare_none(make_person())
    declaration.disclosure_text_version = "2027-01"
    with pytest.raises(ImmutableRowError):
        declaration.save()
    with pytest.raises(ImmutableRowError):
        declaration.delete()
    response = declaration.responses.first()
    response.has_conflict = True
    response.details = "changed my mind"
    with pytest.raises(ImmutableRowError):
        response.save()


# --- Validity: a year from declared_at, rolling -------------------------------


def test_a_declaration_is_valid_for_a_year_from_when_it_was_made():
    declaration = declare_none(make_person(), declared_at=NOW)  # 2026-10-06
    assert declaration.valid_until == datetime.date(2027, 10, 5)
    assert declaration.is_valid_on(datetime.date(2026, 10, 6))
    assert declaration.is_valid_on(datetime.date(2027, 10, 5))
    assert not declaration.is_valid_on(datetime.date(2027, 10, 6))
    assert not declaration.is_valid_on(datetime.date(2026, 10, 5))  # not before it was made


def test_presenter_picks_up_their_current_declaration():
    person = make_person()
    event = make_event()  # 2026-09-15
    expired = declare_none(person, declared_at=NOW - datetime.timedelta(days=400))
    current = declare_none(person, declared_at=NOW - datetime.timedelta(days=30))
    declare_none(make_person(), declared_at=NOW)  # someone else's
    link = SessionPresenter.objects.create(session=event.sessions.get(), person=person)
    assert link.coi_declaration == current
    assert link.coi_declaration != expired


def test_an_expired_declaration_is_not_picked_up():
    """A presenter whose declaration has lapsed must fill a new one."""
    person = make_person()
    event = make_event()  # 2026-09-15
    declare_none(person, declared_at=NOW - datetime.timedelta(days=366 + 21))  # 2025-09-14
    link = SessionPresenter.objects.create(session=event.sessions.get(), person=person)
    assert link.coi_declaration is None
    assert COIDeclaration.objects.current_for(person, datetime.date(2026, 9, 15)) is None
    # The day before it lapsed it still counted.
    assert COIDeclaration.objects.current_for(person, datetime.date(2026, 9, 13)) is not None


def test_a_declaration_made_after_the_event_is_not_picked_up_for_it():
    person = make_person()
    event = make_event()  # 2026-09-15
    declare_none(person, declared_at=NOW)  # 2026-10-06
    assert COIDeclaration.objects.current_for(person, event.date) is None


def test_an_incomplete_declaration_is_not_picked_up():
    person = make_person()
    event = make_event()
    COIDeclaration.objects.create(
        person=person, disclosure_text_version="2026-10", declared_at=NOW - datetime.timedelta(days=30)
    )
    assert COIDeclaration.objects.current_for(person, event.date) is None
    complete = declare_none(person, declared_at=NOW - datetime.timedelta(days=60))
    assert COIDeclaration.objects.current_for(person, event.date) == complete


def test_a_later_declaration_does_not_replace_the_snapshot():
    person = make_person()
    original = declare_none(person, declared_at=NOW - datetime.timedelta(days=30))
    link = SessionPresenter.objects.create(session=make_event().sessions.get(), person=person)
    declare_some(person, declared_at=NOW)
    link.position = 2
    link.save()
    link.refresh_from_db()
    assert link.coi_declaration == original


def test_presenter_cannot_carry_someone_elses_declaration():
    theirs = declare_none(make_person())
    link = SessionPresenter(
        session=make_event().sessions.get(), person=make_person(), coi_declaration=theirs
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
