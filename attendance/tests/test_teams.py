"""
The Teams export parser, against a real (anonymized) export.

The fixture is never edited. Tests build an event that matches it:
10 September 2026, 9:00 to 12:00 Montreal time, three one-hour talks,
Teams meeting title "2026-2027 EM HI lectures / rounds series".
"""
import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from attendance import teams
from attendance.aggregation import attended_minutes
from attendance.models import AttendanceRecord, AttendanceUpload
from attendance.services import (
    check_export_against_event,
    import_export,
    match_event,
    store_upload,
    upload_teams_export,
)
from audit.models import AuditLog
from credits.rules import credit_breakdown
from credits.tests.factories import evaluate
from people.models import PersonEmail
from people.tests.factories import make_person, make_staff
from programs.tests.factories import make_program
from rounds.models import RoundsEvent, Session

FIXTURE = Path(__file__).parent / "fixtures" / "teams-export-2026-09-10.csv"
TITLE = "2026-2027 EM HI lectures / rounds series"
MONTREAL = datetime.timezone(datetime.timedelta(hours=-4))  # EDT on 10 September


def local(hour, minute, second=0, day=10, month=9):
    return datetime.datetime(2026, month, day, hour, minute, second, tzinfo=MONTREAL)


def make_fixture_event(date=datetime.date(2026, 9, 10), title=TITLE, start_hour=9):
    event = RoundsEvent.objects.create(
        program=make_program(),
        date=date,
        start_at=datetime.datetime.combine(date, datetime.time(start_hour), tzinfo=MONTREAL),
        end_at=datetime.datetime.combine(date, datetime.time(start_hour + 3), tzinfo=MONTREAL),
        accredited_credits=Decimal("3.00"),
        teams_meeting_title=title,
    )
    for n in range(3):
        Session.objects.create(event=event, title=f"Talk {n + 1}")
    return event


def fixture_bytes():
    return FIXTURE.read_bytes()


def edited_fixture(old, new, count=1):
    """The fixture with one piece of text changed, re-encoded the way Teams writes it."""
    text = teams.decode(fixture_bytes())
    assert text.count(old) >= count, old
    return text.replace(old, new, count).encode("utf-16")


@pytest.fixture
def export():
    return teams.parse_export(fixture_bytes())


# --- The file itself ----------------------------------------------------------


def test_the_fixture_is_utf16_with_bom_crlf_and_tabs():
    raw = fixture_bytes()
    assert raw[:2] == b"\xff\xfe"
    text = teams.decode(raw)
    assert "\r\n" in raw.decode("utf-16")
    assert "\t" in text and "," in text  # tabs separate; commas live inside names


def test_parses_all_four_sections(export):
    assert export.title == TITLE
    assert export.attended_participants == 10
    assert len(export.participants) == 10
    assert len(export.activities) == 12  # one row per join, not per person
    assert export.engagement_rows == 81
    assert export.warnings == []  # Teams' own totals agree with its rows


def test_names_with_commas_and_suffixes_are_kept_verbatim(export):
    names = [row.display_name for row in export.participants]
    assert "Camille Thibault, Dr" in names
    assert "Thomas Dubois (CUSM) (External)" in names
    assert "Élise Fortin (External)" in names
    camille = next(r for r in export.participants if r.display_name.startswith("Camille"))
    assert camille.email == "camille.thibault@mcgill.ca"  # a comma split would have shifted this


@pytest.mark.parametrize(
    "raw, clean",
    [
        ("Thomas Dubois (CUSM) (External)", "Thomas Dubois"),
        ("Élise Fortin (External)", "Élise Fortin"),
        ("Camille Thibault, Dr", "Camille Thibault, Dr"),
        ("Samuel L Lévesque, Dr", "Samuel L Lévesque, Dr"),
    ],
)
def test_clean_display_name_drops_teams_suffixes_only(raw, clean):
    assert teams.clean_display_name(raw) == clean


@pytest.mark.parametrize(
    "text, seconds",
    [
        ("3h 1m 41s", 3 * 3600 + 61 + 40),
        ("3h 26s", 3 * 3600 + 26),
        ("36m 6s", 36 * 60 + 6),
        ("55m 46s", 55 * 60 + 46),
        ("1h 12m 45s", 4365),
        ("45s", 45),
        ("2h", 7200),
        ("7m", 420),
    ],
)
def test_durations_in_every_shape_teams_writes(text, seconds):
    assert teams.parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["", "   ", "3 hours", "1:02:03", "h m s", "5x"])
def test_unreadable_durations_fail(text):
    with pytest.raises(teams.ExportError):
        teams.parse_duration(text)


@pytest.mark.parametrize(
    "text, hour, minute, second",
    [
        ("9/10/26, 8:57:52 AM", 8, 57, 52),
        ("9/10/26, 12:02:05 PM", 12, 2, 5),
        ("9/10/26, 12:30:00 AM", 0, 30, 0),
        ("9/10/26, 1:15:00 PM", 13, 15, 0),
        ("9/10/2026, 13:15:00", 13, 15, 0),
    ],
)
def test_timestamps(text, hour, minute, second):
    stamp = teams.parse_timestamp(text)
    assert (stamp.a, stamp.b, stamp.year) == (9, 10, 2026)
    assert (stamp.hour, stamp.minute, stamp.second) == (hour, minute, second)


def test_quoted_fields_with_doubled_quotes_in_section_4():
    lines = ["Name\tEngagement Type\tTime", 'Mathieu Caron, Dr\t"Sent reaction ""like"""\t9/10/26, 11:31:44 AM']
    _, data = teams.rows(lines, 3, "Meeting Engagement")
    assert data[0][1] == 'Sent reaction "like"'


def test_ragged_trailing_tabs_are_padded_not_misread():
    """
    In the fixture every Section 2 row happens to end with a value; the empty
    engagement cells are all in the middle. Make a row whose last columns are
    empty, then drop its trailing tabs, as a shorter row would arrive.
    """
    samuel = "samuel.levesque@mcgill.ca\tsamuel.levesque@mcgill.ca\tPresenter\t\t\t\t\t\t\t1\t1"
    text = teams.decode(fixture_bytes())
    assert samuel in text
    text = text.replace(samuel, samuel[: -len("1\t1")].rstrip("\t"))
    export = teams.parse_export(text.encode("utf-16"))
    row = next(r for r in export.participants if r.email == "samuel.levesque@mcgill.ca")
    assert (row.role, row.in_meeting_seconds) == ("Presenter", 3600 + 31 * 60 + 58)
    assert len(export.participants) == 10 and export.warnings == []


def test_a_file_without_the_sections_is_refused():
    with pytest.raises(teams.ExportError, match="Participants"):
        teams.parse_export("1. Summary\r\nMeeting title\tX\r\n".encode("utf-16"))
    with pytest.raises(teams.ExportError):
        teams.parse_export(b"\x00\xff garbage")


def test_a_changed_column_layout_is_refused_not_guessed():
    raw = edited_fixture("Name\tJoin Time\tLeave Time\tDuration\tEmail\tRole",
                         "Name\tJoin Time\tLeave Time\tDuration\tEmail\tRole\tDevice")
    with pytest.raises(teams.ExportError, match="columns"):
        teams.parse_export(raw)


# --- Section 2 is a checksum ----------------------------------------------------


def test_section_2_disagreeing_with_section_3_is_a_warning():
    # Noémie's three rejoins add up to 2h 40m 11s; claim 2h 50m 11s instead.
    raw = edited_fixture("\t2h 40m 11s\tnoemie.giroux@mcgill.ca", "\t2h 50m 11s\tnoemie.giroux@mcgill.ca")
    export = teams.parse_export(raw)
    assert len(export.warnings) == 1
    assert "Noémie Giroux" in export.warnings[0]
    assert "9611" in export.warnings[0] and "10211" in export.warnings[0]


def test_a_participant_count_that_disagrees_is_a_warning():
    export = teams.parse_export(edited_fixture("Attended participants\t10", "Attended participants\t11"))
    assert any("11 participants" in w for w in export.warnings)


def test_a_one_second_difference_per_row_is_tolerated():
    # Teams truncates each row to the second; three rows may differ by up to 3 s.
    export = teams.parse_export(
        edited_fixture("\t2h 40m 11s\tnoemie.giroux@mcgill.ca", "\t2h 40m 13s\tnoemie.giroux@mcgill.ca")
    )
    assert export.warnings == []


# --- The date is never guessed ----------------------------------------------------


@pytest.mark.django_db
def test_the_date_is_read_whichever_way_matches_the_events_known_date(export):
    assert teams.date_order_for(export, datetime.date(2026, 9, 10)) == teams.MONTH_FIRST
    # 9/10/26 really could be 9 October. The file can't say; only the event can.
    assert teams.date_order_for(export, datetime.date(2026, 10, 9)) == teams.DAY_FIRST


@pytest.mark.django_db
def test_a_date_that_matches_neither_reading_fails_loudly(export):
    with pytest.raises(teams.ExportError, match="Is this the right event"):
        teams.date_order_for(export, datetime.date(2026, 9, 11))


@pytest.mark.django_db
def test_an_export_whose_date_does_not_match_the_event_is_rejected_not_parsed(settings, tmp_path):
    settings.UPLOAD_ROOT = tmp_path
    wrong_day = make_fixture_event(date=datetime.date(2026, 9, 24))
    with pytest.raises(teams.ExportError, match="Is this the right event"):
        upload_teams_export(
            SimpleUploadedFile("export.csv", fixture_bytes()), event=wrong_day, user=make_staff()
        )
    assert AttendanceUpload.objects.count() == 0
    assert AttendanceRecord.objects.count() == 0
    assert not list(tmp_path.rglob("*"))  # not even the raw file was stored


@pytest.mark.django_db
def test_a_title_that_does_not_match_the_event_is_rejected(export):
    event = make_fixture_event(title="Surgery grand rounds")
    with pytest.raises(teams.ExportError, match="Surgery grand rounds"):
        check_export_against_event(export, event)


@pytest.mark.django_db
def test_an_event_with_no_teams_title_cannot_be_checked(export):
    event = make_fixture_event(title="")
    with pytest.raises(teams.ExportError, match="no Teams meeting title"):
        check_export_against_event(export, event)


@pytest.mark.django_db
def test_a_meeting_outside_the_events_hours_is_rejected(export):
    evening = make_fixture_event(start_hour=17)
    with pytest.raises(teams.ExportError, match="outside"):
        check_export_against_event(export, evening)


@pytest.mark.django_db
def test_titles_match_ignoring_case_and_spacing(export):
    event = make_fixture_event(title="  2026-2027 em hi LECTURES   /  rounds series ")
    assert check_export_against_event(export, event) == teams.MONTH_FIRST


# --- Finding the event ------------------------------------------------------------


@pytest.mark.django_db
def test_match_event_finds_it_by_title_and_date(export):
    make_fixture_event(date=datetime.date(2026, 9, 24))  # same series, other date
    make_fixture_event(title="Other series")  # same date, other meeting
    right = make_fixture_event()
    assert match_event(export) == right


@pytest.mark.django_db
def test_match_event_refuses_when_both_date_readings_have_an_event(export):
    make_fixture_event()
    make_fixture_event(date=datetime.date(2026, 10, 9))
    with pytest.raises(teams.ExportError, match="Choose the event"):
        match_event(export)


@pytest.mark.django_db
def test_match_event_refuses_when_there_is_nothing(export):
    with pytest.raises(teams.ExportError, match="No event"):
        match_event(export)


# --- Importing ------------------------------------------------------------------


@pytest.fixture
def imported(db, settings, tmp_path):
    settings.UPLOAD_ROOT = tmp_path
    event = make_fixture_event()
    noemie = make_person(given="Noémie", family="Giroux", email="noemie.giroux@mcgill.ca")
    # Stored lowercase; Teams writes "Thomas.Dubois@..." in the export.
    thomas = make_person(given="Thomas", family="Dubois", email="thomas.dubois@muhc.mcgill.ca")
    camille = make_person(given="Camille", family="Thibault", email="camille.thibault@mcgill.ca")
    staff = make_staff()
    upload, export = upload_teams_export(
        SimpleUploadedFile("export.csv", fixture_bytes()), user=staff
    )
    return event, upload, {"noemie": noemie, "thomas": thomas, "camille": camille}, staff


@pytest.mark.django_db
def test_import_writes_one_row_per_join_and_records_the_upload(imported):
    event, upload, people, staff = imported
    assert upload.event == event
    assert (upload.row_count, upload.parser_version) == (12, teams.PARSER_VERSION)
    assert upload.parsed_at is not None and upload.parse_warnings == []
    rows = AttendanceRecord.objects.filter(upload=upload)
    assert rows.count() == 12
    assert set(rows.values_list("source", flat=True)) == {"teams_upload"}
    entry = AuditLog.objects.get(action="attendance.parsed")
    assert (entry.actor_user, entry.metadata["rows"], entry.metadata["matched"]) == (staff, 12, 5)


@pytest.mark.django_db
def test_rows_keep_what_teams_wrote(imported):
    _, upload, _, _ = imported
    thomas = AttendanceRecord.objects.get(upload=upload, raw_display_name__startswith="Thomas")
    assert thomas.raw_display_name == "Thomas Dubois (CUSM) (External)"
    assert thomas.raw_email == "Thomas.Dubois@muhc.mcgill.ca"  # case as written
    assert thomas.raw_participant_role == "Presenter"
    assert thomas.duration_seconds == 2 * 3600 + 56 * 60 + 47
    # 9:02:41 AM Montreal is 13:02:41 UTC.
    assert thomas.join_at == datetime.datetime(2026, 9, 10, 13, 2, 41, tzinfo=datetime.timezone.utc)


@pytest.mark.django_db
def test_emails_match_whatever_their_capitalization(imported):
    _, upload, people, _ = imported
    for key, expected_rows in (("thomas", 1), ("noemie", 3), ("camille", 1)):
        rows = AttendanceRecord.objects.filter(upload=upload, person=people[key])
        assert rows.count() == expected_rows, key
        assert set(rows.values_list("match_method", flat=True)) == {"email_exact"}
    assert AttendanceRecord.objects.filter(upload=upload, person__isnull=True).count() == 7


@pytest.mark.django_db
def test_the_three_row_rejoin_from_the_fixture(imported):
    """
    Noémie Giroux joined three times: 9:15:50-9:51:56, 9:55:31-11:08:17 and
    11:10:45-12:02:05. The gaps are not attendance. Against three one-hour
    talks (9, 10, 11) with five minutes' grace after the last:
      talk 1: 36m 6s + 4m 29s = 40m 35s
      talk 2: the whole hour
      talk 3: 8m 17s + 51m 20s = 59m 37s
    """
    event, _, people, _ = imported
    noemie = people["noemie"]
    rows = AttendanceRecord.objects.filter(person=noemie).order_by("join_at")
    assert [r.duration_seconds for r in rows] == [36 * 60 + 6, 72 * 60 + 45, 51 * 60 + 20]
    result = attended_minutes(noemie, event)
    assert [s.seconds for s in result.sessions] == [40 * 60 + 35, 3600, 59 * 60 + 37]
    assert [s.minutes for s in result.sessions] == [40, 60, 59]
    # Teams' own total for her is 2h 40m 11s; ours, from the timestamps, is
    # one second more because Teams truncates each row to the second.
    assert result.seconds - (2 * 3600 + 40 * 60 + 11) == 1


@pytest.mark.django_db
def test_an_upload_cannot_be_imported_twice(imported, export):
    _, upload, _, staff = imported
    with pytest.raises(teams.ExportError, match="already been parsed"):
        import_export(upload, export, teams.MONTH_FIRST, user=staff)


# --- The Teams meeting role means nothing for credit -----------------------------


def test_the_role_column_is_labelled_as_a_meeting_permission():
    field = AttendanceRecord._meta.get_field("raw_participant_role")
    assert field.verbose_name == "Teams meeting role"
    assert "no effect on credit" in field.help_text


@pytest.mark.django_db
def test_attendance_and_credit_are_identical_whatever_the_teams_role_says(imported):
    """Everyone in the export is an attendee for credit; presenters come from SessionPresenter."""
    event, _, people, _ = imported
    for session in event.sessions.all():
        evaluate(people["camille"], session)
    before = credit_breakdown(people["camille"], event)
    assert before.teaching_credits == Decimal("0.00")  # "Organizer" in Teams teaches nothing
    for role in ("Attendee", "Presenter", "Organizer", "", None, "Co-organizer"):
        AttendanceRecord.objects.filter(person=people["camille"]).update(raw_participant_role=role)
        after = credit_breakdown(people["camille"], event)
        assert after == before, role


def test_nothing_in_the_credit_path_reads_the_teams_role():
    root = Path(__file__).resolve().parents[2]
    credit_path = [
        "attendance/aggregation.py",
        "credits/rules.py",
        "credits/windows.py",
        "credits/reports.py",
        "certificates/figures.py",
        "certificates/rules.py",
    ]
    for relative in credit_path:
        assert "raw_participant_role" not in (root / relative).read_text(encoding="utf-8"), relative
