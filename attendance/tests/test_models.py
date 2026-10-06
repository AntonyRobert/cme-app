import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from attendance.models import AttendanceRecord, AttendanceSupersession
from core.models import ImmutableRowError
from people.tests.factories import make_person, make_staff
from rounds.tests.factories import at, make_event

from .factories import make_upload, manual_row, roster_row, supersede, teams_row

pytestmark = pytest.mark.django_db

Source = AttendanceRecord.Source
Match = AttendanceRecord.MatchMethod


@pytest.fixture
def event():
    return make_event()


# --- Observations are immutable ----------------------------------------------


@pytest.mark.parametrize(
    "field, value",
    [
        ("raw_display_name", "Someone Else"),
        ("raw_email", "other@mcgill.ca"),
        ("join_at", at(5)),
        ("leave_at", at(59)),
        ("duration_seconds", 1),
        ("reason", "rewritten"),
        ("source", Source.MANUAL),
    ],
)
def test_observation_fields_cannot_be_changed(event, field, value):
    row = teams_row(event, make_person(), 0, 30, raw_email="a@mcgill.ca")
    setattr(row, field, value)
    with pytest.raises(ImmutableRowError):
        row.save()


def test_the_event_and_upload_of_a_row_cannot_be_changed(event):
    row = teams_row(event, make_person(), 0, 30)
    row.event = make_event()
    with pytest.raises(ImmutableRowError):
        row.save()
    row.refresh_from_db()
    row.upload = make_upload(event)
    with pytest.raises(ImmutableRowError):
        row.save()


def test_rows_cannot_be_deleted(event):
    row = teams_row(event, make_person(), 0, 30)
    with pytest.raises(ImmutableRowError):
        row.delete()


def test_the_match_can_be_revised(event):
    row = teams_row(event, None, 0, 30)
    assert row.match_method == Match.UNMATCHED
    person = make_person()
    row.person = person
    row.save()
    row.refresh_from_db()
    assert (row.person, row.match_method) == (person, Match.MANUAL)
    row.person = None
    row.save()
    row.refresh_from_db()
    assert row.match_method == Match.UNMATCHED


def test_uploads_keep_their_identity_but_record_parsing(event):
    upload = make_upload(event)
    upload.row_count = 42
    upload.parser_version = "1"
    upload.save()
    upload.sha256 = "0" * 64
    with pytest.raises(ImmutableRowError):
        upload.save()
    with pytest.raises(ImmutableRowError):
        upload.delete()


def test_the_same_export_cannot_be_uploaded_twice(event):
    first = make_upload(event)
    with pytest.raises(IntegrityError), transaction.atomic():
        type(first).objects.create(
            event=event,
            original_filename="again.csv",
            stored_path="teams/again.csv",
            sha256=first.sha256,
            uploaded_by=make_staff(),
        )


# --- Row shape ---------------------------------------------------------------


def test_a_manual_row_needs_a_reason(event):
    with pytest.raises(IntegrityError), transaction.atomic():
        manual_row(event, make_person(), minutes=30, reason="")
    row = AttendanceRecord(
        source=Source.MANUAL, event=event, duration_seconds=60, created_by=make_staff()
    )
    with pytest.raises(ValidationError) as err:
        row.full_clean()
    assert "reason" in err.value.message_dict


def test_a_teams_row_needs_its_upload_and_a_manual_row_has_none(event):
    upload = make_upload(event)
    with pytest.raises(IntegrityError), transaction.atomic():
        AttendanceRecord.objects.create(
            source=Source.TEAMS_UPLOAD,
            event=event,
            join_at=at(0),
            leave_at=at(10),
            created_by=upload.uploaded_by,
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        manual_row(event, make_person(), minutes=30, upload=upload)


def test_times_come_in_pairs_and_in_order(event):
    person = make_person()
    with pytest.raises(IntegrityError), transaction.atomic():
        manual_row(event, person, minutes=10, start=0)
    with pytest.raises(IntegrityError), transaction.atomic():
        manual_row(event, person, start=30, end=10, duration_seconds=0)


def test_duration_is_derived_from_times_when_not_given(event):
    assert manual_row(event, make_person(), start=10, end=40).duration_seconds == 30 * 60


def test_a_row_with_neither_duration_nor_times_is_refused(event):
    row = AttendanceRecord(
        source=Source.MANUAL, event=event, reason="?", created_by=make_staff()
    )
    with pytest.raises(ValidationError) as err:
        row.full_clean()
    assert "duration_seconds" in err.value.message_dict


def test_only_room_roster_rows_are_attributed(event):
    device = teams_row(event, None, 0, 60)
    with pytest.raises(IntegrityError), transaction.atomic():
        manual_row(event, make_person(), minutes=30, attributed_to=device)
    with pytest.raises(IntegrityError), transaction.atomic():
        AttendanceRecord.objects.create(
            source=Source.ROOM_ROSTER,
            event=event,
            duration_seconds=60,
            reason="no device",
            created_by=make_staff(),
        )


def test_a_room_roster_row_must_point_into_its_own_event(event):
    device = teams_row(make_event(), None, 0, 60)
    row = AttendanceRecord(
        source=Source.ROOM_ROSTER,
        event=event,
        attributed_to=device,
        person=make_person(),
        reason="sheet",
        created_by=make_staff(),
    )
    with pytest.raises(ValidationError) as err:
        row.full_clean()
    assert "attributed_to" in err.value.message_dict


def test_unmatched_queue_is_rows_without_a_person(event):
    teams_row(event, make_person(), 0, 60)
    stray = teams_row(event, None, 0, 60, raw_display_name="iPhone de Marie")
    assert list(AttendanceRecord.objects.unmatched()) == [stray]


def test_match_method_must_agree_with_person(event):
    row = teams_row(event, make_person(), 0, 60)
    with pytest.raises(IntegrityError), transaction.atomic():
        AttendanceRecord.objects.filter(pk=row.pk).update(match_method=Match.UNMATCHED)
    with pytest.raises(IntegrityError), transaction.atomic():
        AttendanceRecord.objects.filter(pk=row.pk).update(person=None)


# --- Supersession ------------------------------------------------------------


def test_superseding_leaves_the_old_row_untouched(event):
    person = make_person()
    old = teams_row(event, person, 0, 20)
    before = AttendanceRecord.objects.filter(pk=old.pk).values().get()
    supersede([old], manual_row(event, person, minutes=50))
    assert AttendanceRecord.objects.filter(pk=old.pk).values().get() == before


def test_active_and_superseded_querysets(event):
    person = make_person()
    old1, old2 = teams_row(event, person, 0, 10), teams_row(event, person, 20, 30)
    untouched = teams_row(event, person, 40, 50)
    new = manual_row(event, person, minutes=30)
    supersede([old1, old2], new)
    assert set(AttendanceRecord.objects.active()) == {untouched, new}
    assert set(AttendanceRecord.objects.superseded()) == {old1, old2}
    assert set(new.supersedes.values_list("old", flat=True)) == {old1.pk, old2.pk}


def test_a_row_cannot_be_superseded_twice(event):
    """Two corrections of one row would both count, inflating the credit."""
    person = make_person()
    old = teams_row(event, person, 0, 20)
    supersede([old], manual_row(event, person, minutes=50))
    with pytest.raises((IntegrityError, ValidationError)), transaction.atomic():
        supersede([old], manual_row(event, person, minutes=55))
    assert AttendanceSupersession.objects.filter(old=old).count() == 1


def test_database_itself_refuses_a_second_supersession(event):
    person = make_person()
    old = teams_row(event, person, 0, 20)
    first, second = manual_row(event, person, minutes=50), manual_row(event, person, minutes=55)
    user = make_staff()
    AttendanceSupersession.objects.bulk_create(
        [AttendanceSupersession(old=old, new=first, created_by=user)]
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        AttendanceSupersession.objects.bulk_create(
            [AttendanceSupersession(old=old, new=second, created_by=user)]
        )


def test_a_row_cannot_supersede_itself(event):
    row = manual_row(event, make_person(), minutes=30)
    with pytest.raises(ValidationError):
        supersede([row], row)
    with pytest.raises(IntegrityError), transaction.atomic():
        AttendanceSupersession.objects.bulk_create(
            [AttendanceSupersession(old=row, new=row, created_by=make_staff())]
        )


def test_supersession_stays_within_one_event(event):
    old = teams_row(event, make_person(), 0, 20)
    elsewhere = manual_row(make_event(), make_person(), minutes=30)
    with pytest.raises(ValidationError):
        supersede([old], elsewhere)


def test_the_replacement_must_be_a_live_row_so_loops_are_impossible(event):
    person = make_person()
    a = teams_row(event, person, 0, 20)
    b = manual_row(event, person, minutes=30)
    c = manual_row(event, person, minutes=40)
    supersede([a], b)
    with pytest.raises(ValidationError):
        supersede([b], a)  # would make a loop
    supersede([b], c)
    with pytest.raises(ValidationError):
        supersede([teams_row(event, person, 30, 40)], b)  # b is no longer live


def test_supersessions_are_append_only(event):
    person = make_person()
    link = supersede([teams_row(event, person, 0, 20)], manual_row(event, person, minutes=50))[0]
    link.new = manual_row(event, person, minutes=1)
    with pytest.raises(ImmutableRowError):
        link.save()
    with pytest.raises(ImmutableRowError):
        link.delete()
