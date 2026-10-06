import hashlib
import os

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile

from attendance.models import AttendanceRecord, AttendanceSupersession
from attendance.services import (
    DuplicateUpload,
    log_manual_row,
    match_record,
    store_upload,
    supersede_rows,
)
from audit.models import AuditLog
from people.models import Person, PersonEmail
from people.tests.factories import make_person, make_staff
from rounds.tests.factories import make_event

from .factories import manual_row, teams_row

pytestmark = pytest.mark.django_db

Match = AttendanceRecord.MatchMethod


@pytest.fixture
def staff():
    return make_staff("coordinator")


@pytest.fixture
def event():
    return make_event()


def actions():
    return list(AuditLog.objects.order_by("id").values_list("action", flat=True))


# --- Matching ----------------------------------------------------------------


def test_matching_sets_the_interpretation_and_logs_it(event, staff):
    row = teams_row(event, None, 0, 60, raw_display_name="iPhone de Marie")
    person = make_person()
    match_record(row, person, user=staff)
    row.refresh_from_db()
    assert (row.person, row.match_method, row.matched_by) == (person, Match.MANUAL, staff)
    assert row.matched_at is not None
    assert row.raw_display_name == "iPhone de Marie"
    entry = AuditLog.objects.get(action="attendance.matched")
    assert entry.metadata["to_person"] == str(person.pk)
    assert entry.metadata["from_person"] is None


def test_confirming_a_match_remembers_the_address_and_clears_the_other_rows(event, staff):
    """One decision should deal with all four of someone's rejoin rows."""
    address = "Marie.Tremblay@MUHC.McGill.ca"
    rows = [teams_row(event, None, s, s + 10, raw_email=address) for s in (0, 15, 30, 45)]
    later = teams_row(make_event(), None, 0, 60, raw_email=address.lower())
    stranger = teams_row(event, None, 0, 60, raw_email="someone.else@mcgill.ca")
    person = make_person()

    result = match_record(rows[0], person, user=staff)

    assert result.email_added == "marie.tremblay@muhc.mcgill.ca"
    assert result.also_matched == 4
    assert PersonEmail.objects.get(email="marie.tremblay@muhc.mcgill.ca").person == person
    for row in rows[1:] + [later]:
        row.refresh_from_db()
        assert (row.person, row.match_method, row.matched_by) == (person, Match.EMAIL_ALIAS, staff)
    stranger.refresh_from_db()
    assert stranger.person is None
    assert rows[1].raw_email == address  # still exactly as Teams wrote it
    assert actions() == ["attendance.matched", "person.email_added", "attendance.matched"]


def test_an_address_owned_by_someone_else_is_not_reassigned(event, staff):
    owner = make_person(email="shared@mcgill.ca")
    row = teams_row(event, None, 0, 60, raw_email="shared@mcgill.ca")
    sibling = teams_row(event, None, 0, 60, raw_email="shared@mcgill.ca")
    other = make_person()
    result = match_record(row, other, user=staff)
    assert (result.email_added, result.also_matched) == (None, 0)
    assert PersonEmail.objects.get(email="shared@mcgill.ca").person == owner
    sibling.refresh_from_db()
    assert sibling.person is None


def test_a_teams_upn_that_is_not_an_address_is_not_remembered(event, staff):
    row = teams_row(event, None, 0, 60, raw_email="not an email")
    result = match_record(row, make_person(), user=staff)
    assert result.email_added is None
    assert PersonEmail.objects.count() == 0


def test_rematching_and_unmatching_are_logged_as_such(event, staff):
    first, second = make_person(), make_person()
    row = teams_row(event, None, 0, 60)
    match_record(row, first, user=staff)
    match_record(row, second, user=staff)
    match_record(row, None, user=staff)
    row.refresh_from_db()
    assert (row.person, row.match_method) == (None, Match.UNMATCHED)
    assert actions() == ["attendance.matched", "attendance.rematched", "attendance.unmatched"]
    rematch = AuditLog.objects.get(action="attendance.rematched")
    assert rematch.metadata["from_person"] == str(first.pk)
    assert rematch.metadata["to_person"] == str(second.pk)


def test_matching_to_a_merged_record_lands_on_the_survivor(event, staff):
    survivor, duplicate = make_person(), make_person()
    Person.objects.filter(pk=duplicate.pk).update(merged_into=survivor)
    duplicate.refresh_from_db()
    row = teams_row(event, None, 0, 60)
    match_record(row, duplicate, user=staff)
    row.refresh_from_db()
    assert row.person == survivor


# --- Manual rows and supersession --------------------------------------------


def test_manual_rows_are_logged_with_their_reason(event, staff):
    row = manual_row(event, make_person(), minutes=45, reason="Phoned in; chair confirms", user=staff)
    log_manual_row(row, user=staff)
    entry = AuditLog.objects.get(action="attendance.manual_row_created")
    assert entry.object_id == str(row.pk)
    assert entry.metadata["reason"] == "Phoned in; chair confirms"
    assert entry.metadata["duration_seconds"] == 45 * 60


def test_supersede_rows_links_all_of_them_and_logs_once(event, staff):
    person = make_person()
    rejoins = [teams_row(event, person, s, s + 10) for s in (0, 20, 40)]
    correction = manual_row(event, person, minutes=55)
    links = supersede_rows(rejoins, correction, user=staff)
    assert len(links) == 3
    assert set(AttendanceRecord.objects.active()) == {correction}
    entry = AuditLog.objects.get(action="attendance.superseded")
    assert sorted(entry.metadata["replaced_rows"]) == sorted(str(r.pk) for r in rejoins)


def test_supersede_rows_is_all_or_nothing(event, staff):
    person = make_person()
    fine = teams_row(event, person, 0, 10)
    elsewhere = teams_row(make_event(), person, 0, 10)
    correction = manual_row(event, person, minutes=55)
    with pytest.raises(ValidationError):
        supersede_rows([fine, elsewhere], correction, user=staff)
    assert AttendanceSupersession.objects.count() == 0
    assert not AuditLog.objects.filter(action="attendance.superseded").exists()


def test_superseding_an_already_superseded_row_is_refused(event, staff):
    person = make_person()
    old = teams_row(event, person, 0, 10)
    supersede_rows([old], manual_row(event, person, minutes=30), user=staff)
    with pytest.raises(ValidationError):
        supersede_rows([old], manual_row(event, person, minutes=40), user=staff)


# --- Storing the raw export --------------------------------------------------


@pytest.fixture
def upload_root(settings, tmp_path):
    settings.UPLOAD_ROOT = tmp_path
    return tmp_path


def test_the_export_is_stored_byte_for_byte_and_read_only(event, staff, upload_root):
    content = b"\xef\xbb\xbfMeeting Summary\r\nTotal Number of Participants,12\r\n"
    upload = store_upload(event, SimpleUploadedFile("Rounds - Attendance.csv", content), user=staff)
    stored = upload_root / upload.stored_path
    assert stored.read_bytes() == content
    assert upload.sha256 == hashlib.sha256(content).hexdigest()
    assert upload.stored_path == f"teams/{upload.sha256}.csv"
    assert upload.original_filename == "Rounds - Attendance.csv"
    assert not os.access(stored, os.W_OK)
    assert AuditLog.objects.get(action="attendance.upload_stored").object_id == str(upload.pk)


def test_the_same_file_twice_is_refused(event, staff, upload_root):
    first = store_upload(event, SimpleUploadedFile("a.csv", b"same bytes"), user=staff)
    with pytest.raises(DuplicateUpload) as err:
        store_upload(make_event(), SimpleUploadedFile("renamed.csv", b"same bytes"), user=staff)
    assert err.value.existing == first


def test_only_spreadsheet_exports_are_accepted(event, staff, upload_root):
    with pytest.raises(ValidationError):
        store_upload(event, SimpleUploadedFile("notes.exe", b"MZ"), user=staff)
    assert list(upload_root.iterdir()) == []


def test_the_filename_cannot_steer_where_the_file_lands(event, staff, upload_root):
    upload = store_upload(event, SimpleUploadedFile("../../evil.csv", b"x"), user=staff)
    assert upload.stored_path.startswith("teams/")
    assert (upload_root / upload.stored_path).exists()
    assert ".." not in upload.stored_path
