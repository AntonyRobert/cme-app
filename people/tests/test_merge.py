import datetime
from decimal import Decimal

import pytest

from attendance.models import AttendanceRecord
from attendance.tests.factories import teams_row
from audit.models import AuditLog
from certificates.models import Certificate
from credits.models import CreditAdjustment, EvaluationSubmission
from credits.rules import event_credits
from credits.tests.factories import adjust, evaluate
from people.identity import resolve_root
from people.merge import (
    MergeCollision,
    MergeError,
    find_collisions,
    merge_people,
    person_links,
)
from people.models import Person, PersonEmail
from rounds.coi import declare_no_conflicts
from rounds.models import SessionPresenter
from rounds.tests.factories import make_event, make_session

from .factories import make_person, make_staff

pytestmark = pytest.mark.django_db


@pytest.fixture
def staff():
    return make_staff("merger")


@pytest.fixture
def pair():
    survivor = make_person(given="Marie", family="Tremblay", email="marie.tremblay@mcgill.ca")
    duplicate = make_person(given="Marie", family="Tremblay", email="mtremblay@muhc.mcgill.ca")
    return survivor, duplicate


def emails_of(person):
    return dict(PersonEmail.objects.filter(person=person).values_list("email", "is_primary"))


# --- What a merge does -------------------------------------------------------


def test_emails_move_to_the_survivor_and_lose_primary(pair, staff):
    survivor, duplicate = pair
    PersonEmail.objects.create(person=duplicate, email="marie@example.org")
    result = merge_people(survivor, duplicate, user=staff)
    assert emails_of(survivor) == {
        "marie.tremblay@mcgill.ca": True,
        "mtremblay@muhc.mcgill.ca": False,
        "marie@example.org": False,
    }
    assert emails_of(duplicate) == {}
    assert result.moved_emails == ["marie@example.org", "mtremblay@muhc.mcgill.ca"]


def test_a_survivor_without_a_primary_email_inherits_one(staff):
    survivor = make_person()
    duplicate = make_person(email="only@mcgill.ca")
    merge_people(survivor, duplicate, user=staff)
    assert emails_of(survivor) == {"only@mcgill.ca": True}


def test_the_duplicate_becomes_a_tombstone(pair, staff):
    survivor, duplicate = pair
    merge_people(survivor, duplicate, user=staff)
    duplicate.refresh_from_db()
    assert duplicate.merged_into == survivor
    assert resolve_root(duplicate) == survivor
    assert Person.objects.filter(pk=duplicate.pk).exists()  # never deleted


def test_every_kind_of_row_follows_the_merge(pair, staff):
    survivor, duplicate = pair
    event = make_event()
    first, second = make_session(event), make_session(event)
    attendance = teams_row(event, duplicate, 0, 60)
    evaluation = evaluate(duplicate, first)
    declaration = declare_no_conflicts(duplicate)
    presenting = SessionPresenter.objects.create(session=second, person=duplicate)
    adjustment = adjust(duplicate, event, "0.25")
    certificate = Certificate.objects.create(
        person=duplicate,
        certificate_type="cme",
        period_start=datetime.date(2026, 1, 1),
        period_end=datetime.date(2026, 12, 31),
        total_credits=Decimal("1.00"),
        recipient_name="Marie Tremblay",
        template_version="1",
        issued_by=staff,
    )

    result = merge_people(survivor, duplicate, user=staff)

    for row in (attendance, evaluation, declaration, presenting, adjustment, certificate):
        row.refresh_from_db()
        assert row.person == survivor, type(row).__name__
    assert result.repointed_count == 6
    assert certificate.recipient_name == "Marie Tremblay"  # the snapshot is untouched


def test_observation_fields_survive_the_repointing(pair, staff):
    survivor, duplicate = pair
    row = teams_row(make_event(), duplicate, 0, 60, raw_email="MTremblay@MUHC.McGill.ca")
    frozen = AttendanceRecord.objects.filter(pk=row.pk).values(*AttendanceRecord.FROZEN_FIELDS).get()
    merge_people(survivor, duplicate, user=staff)
    assert (
        AttendanceRecord.objects.filter(pk=row.pk).values(*AttendanceRecord.FROZEN_FIELDS).get()
        == frozen
    )


def test_credit_comes_together_after_a_merge(pair, staff):
    """The reason merges exist: Teams on one address, the form on another."""
    survivor, duplicate = pair
    event = make_event()
    session = event.sessions.get()
    teams_row(event, duplicate, 0, 60)
    evaluate(survivor, session, minutes=0)
    assert event_credits(survivor, event) == Decimal("0.00")
    merge_people(survivor, duplicate, user=staff)
    assert event_credits(survivor, event) == Decimal("1.00")


def test_earlier_tombstones_are_pointed_at_the_new_root(staff):
    root, middle, oldest = make_person(), make_person(), make_person()
    merge_people(middle, oldest, user=staff)
    merge_people(root, middle, user=staff)
    oldest.refresh_from_db()
    assert oldest.merged_into == root  # one hop, not a chain


def test_the_survivor_can_take_over_the_licence_number(staff):
    survivor = make_person()
    duplicate = make_person(licence_number="01234", licence_jurisdiction="CMQ")
    merge_people(survivor, duplicate, user=staff)
    survivor.refresh_from_db()
    assert survivor.licence_number is None  # not copied automatically
    survivor.licence_number, survivor.licence_jurisdiction = "01234", "CMQ"
    survivor.full_clean()
    survivor.save()


def test_a_linked_staff_account_moves_with_the_merge(staff):
    survivor, duplicate = make_person(), make_person()
    account = make_staff()
    duplicate.staff_user = account
    duplicate.save()
    merge_people(survivor, duplicate, user=staff)
    survivor.refresh_from_db()
    duplicate.refresh_from_db()
    assert (survivor.staff_user, duplicate.staff_user) == (account, None)


def test_the_audit_entry_lists_everything_needed_to_undo_it(pair, staff):
    survivor, duplicate = pair
    row = teams_row(make_event(), duplicate, 0, 60)
    merge_people(survivor, duplicate, user=staff)
    entry = AuditLog.objects.get(action="person.merged")
    assert entry.actor_user == staff
    assert entry.object_id == str(survivor.pk)
    assert entry.metadata["duplicate_id"] == str(duplicate.pk)
    assert entry.metadata["moved_emails"] == ["mtremblay@muhc.mcgill.ca"]
    assert entry.metadata["repointed"] == {"attendance.attendancerecord.person": [str(row.pk)]}


def test_audit_entries_keep_pointing_at_whoever_acted(pair, staff):
    survivor, duplicate = pair
    from audit.log import record

    entry = record("coi.declared", duplicate, person=duplicate)
    merge_people(survivor, duplicate, user=staff)
    entry.refresh_from_db()
    assert entry.actor_person == duplicate


# --- What a merge refuses ----------------------------------------------------


def test_two_evaluations_of_the_same_session_stop_the_merge(pair, staff):
    survivor, duplicate = pair
    event = make_event()
    session = make_session(event)
    kept = evaluate(survivor, session, minutes=20)
    other = evaluate(duplicate, session, minutes=55)
    teams_row(event, duplicate, 0, 60)

    with pytest.raises(MergeCollision) as err:
        merge_people(survivor, duplicate, user=staff)

    (collision,) = err.value.collisions
    assert (collision.survivor_row, collision.duplicate_row) == (kept, other)
    assert "evaluation submission" in str(err.value)
    # Nothing moved: not the evaluation, not the attendance, not the emails.
    assert EvaluationSubmission.objects.filter(person=duplicate).count() == 1
    assert AttendanceRecord.objects.filter(person=duplicate).count() == 1
    assert emails_of(duplicate) == {"mtremblay@muhc.mcgill.ca": True}
    duplicate.refresh_from_db()
    assert duplicate.merged_into is None
    assert not AuditLog.objects.filter(action="person.merged").exists()


def test_evaluations_of_different_sessions_do_not_collide(pair, staff):
    survivor, duplicate = pair
    event = make_event()
    evaluate(survivor, make_session(event))
    evaluate(duplicate, make_session(event))
    assert find_collisions(survivor, duplicate) == []
    merge_people(survivor, duplicate, user=staff)
    assert EvaluationSubmission.objects.filter(person=survivor).count() == 2


def test_presenting_the_same_session_twice_stops_the_merge(pair, staff):
    survivor, duplicate = pair
    session = make_session(make_event())
    SessionPresenter.objects.create(session=session, person=survivor, position=1)
    SessionPresenter.objects.create(session=session, person=duplicate, position=2)
    with pytest.raises(MergeCollision):
        merge_people(survivor, duplicate, user=staff)


def test_several_adjustments_for_one_event_are_not_a_collision(pair, staff):
    survivor, duplicate = pair
    event = make_event()
    adjust(survivor, event, "0.25")
    adjust(duplicate, event, "0.25")
    merge_people(survivor, duplicate, user=staff)
    assert CreditAdjustment.objects.filter(person=survivor).count() == 2


def test_impossible_merges_are_refused(pair, staff):
    survivor, duplicate = pair
    with pytest.raises(MergeError):
        merge_people(survivor, survivor, user=staff)
    merge_people(survivor, duplicate, user=staff)
    with pytest.raises(MergeError):  # already merged
        merge_people(make_person(), duplicate, user=staff)
    with pytest.raises(MergeError):  # into a tombstone
        merge_people(duplicate, make_person(), user=staff)


def test_two_staff_linked_records_are_refused(staff):
    survivor, duplicate = make_person(), make_person()
    survivor.staff_user, duplicate.staff_user = make_staff(), make_staff()
    survivor.save()
    duplicate.save()
    with pytest.raises(MergeError):
        merge_people(survivor, duplicate, user=staff)


# --- Guard: a new table that points at Person must be thought about ----------


def test_every_link_to_a_person_is_accounted_for():
    """
    If this fails you added a table that points at Person. Decide whether
    its rows should follow a merge (the default) or stay put (add it to
    NOT_REPOINTED in people/merge.py), then update this list.
    """
    assert [(model._meta.label_lower, name) for model, name in person_links()] == [
        ("attendance.attendancerecord", "person"),
        ("certificates.certificate", "person"),
        ("credits.creditadjustment", "person"),
        ("credits.evaluationsubmission", "person"),
        ("credits.evaluationwindow", "person"),
        ("people.personemail", "person"),
        ("rounds.coideclaration", "person"),
        ("rounds.sessionpresenter", "person"),
    ]
