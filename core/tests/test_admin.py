"""
The admin is the whole interface at this stage, so it is tested through
real requests against the seeded data.
"""
import datetime

import pytest
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from attendance.aggregation import attended_minutes
from attendance.models import AttendanceRecord, AttendanceSupersession, AttendanceUpload
from audit.models import AuditLog
from credits.models import CreditAdjustment, EvaluationSubmission, EvaluationWindow
from people.models import Person, PersonEmail
from rounds.models import COIDeclaration, RoundsEvent, Session

pytestmark = pytest.mark.django_db

OUR_APPS = {"accounts", "people", "rounds", "attendance", "credits", "certificates", "audit"}


@pytest.fixture
def seeded(settings, tmp_path):
    settings.UPLOAD_ROOT = tmp_path
    call_command("seed_demo", verbosity=0, allow_non_debug=True)
    return tuple(RoundsEvent.objects.order_by("date"))


@pytest.fixture
def boss(client):
    user = get_user_model().objects.create_superuser(username="boss", password="x" * 20)
    client.force_login(user)
    return user


def url(model, page, *args):
    return reverse(f"admin:{model._meta.app_label}_{model._meta.model_name}_{page}", args=args)


def who(family):
    return Person.objects.filter(family_name=family).order_by("created_at").first()


# --- Every page loads --------------------------------------------------------


def test_every_admin_page_renders(client, boss, seeded):
    models = [m for m in admin.site._registry if m._meta.app_label in OUR_APPS]
    assert len(models) == 14
    for model in models:
        model_admin = admin.site._registry[model]
        assert client.get(url(model, "changelist")).status_code == 200, model
        for instance in model._base_manager.all()[:3]:
            assert client.get(url(model, "change", instance.pk)).status_code == 200, model
        response = client.get(url(model, "add"))
        expected = 200 if model_admin.has_add_permission(response.wsgi_request) else 403
        assert response.status_code == expected, model


def test_every_list_filter_and_search_works(client, boss, seeded):
    first = seeded[0]
    for model, query in [
        (Person, "state=duplicates"),
        (Person, "state=merged"),
        (Person, "q=tremblay"),
        (Person, "q=01234"),
        (Person, "q=example.com"),
        (AttendanceRecord, "matched=no"),
        (AttendanceRecord, "active=no"),
        (AttendanceRecord, f"event__id__exact={first.pk}&matched=no"),
        (AttendanceRecord, "q=iphone"),
        (AttendanceRecord, "source__exact=room_roster"),
        (AuditLog, "q=seed-script"),
        (AuditLog, "action=attendance.superseded"),
        (COIDeclaration, "valid=yes"),
        (EvaluationSubmission, "is_complete__exact=0"),
    ]:
        response = client.get(url(model, "changelist") + "?" + query)
        assert response.status_code == 200, (model, query)
        assert "e=1" not in response.headers.get("Location", ""), (model, query)


def test_possible_duplicates_filter_finds_the_two_tremblays(client, boss, seeded):
    response = client.get(url(Person, "changelist") + "?state=duplicates")
    assert response.context["cl"].result_count == 2


def test_event_page_shows_live_credit_per_person(client, boss, seeded):
    page = client.get(url(RoundsEvent, "change", seeded[0].pk)).content.decode()
    assert "Côté" in page and "0.75" in page
    assert "Review: self-reported only" in page  # Morin
    assert "0/3" in page  # Okafor has evaluated nothing completely
    assert "+0.75" in page  # his adjustment


def test_person_autocomplete_never_offers_a_tombstone(client, boss, seeded):
    main, duplicate = Person.objects.filter(family_name="Tremblay").order_by("created_at")
    Person.objects.filter(pk=duplicate.pk).update(merged_into=main)
    response = client.get(
        reverse("admin:autocomplete"),
        {"term": "tremblay", "app_label": "attendance", "model_name": "attendancerecord",
         "field_name": "person"},
    )
    assert [r["id"] for r in response.json()["results"]] == [str(main.pk)]


# --- The review queue --------------------------------------------------------


def changelist_post(client, rows, changes):
    """Post the editable changelist back with `changes` = {row pk: person pk}."""
    data = {
        "form-TOTAL_FORMS": len(rows),
        "form-INITIAL_FORMS": len(rows),
        "form-MIN_NUM_FORMS": 0,
        "form-MAX_NUM_FORMS": 1000,
        "_save": "Save",
    }
    for index, row in enumerate(rows):
        data[f"form-{index}-id"] = str(row.pk)
        person = changes.get(row.pk, row.person_id)
        data[f"form-{index}-person"] = str(person) if person else ""
    return data


def test_matching_from_the_unmatched_list(client, boss, seeded):
    second = seeded[1]
    bouchard = who("Bouchard")
    queue = url(AttendanceRecord, "changelist") + f"?matched=no&event__id__exact={second.pk}"
    page = client.get(queue)
    rows = list(page.context["cl"].result_list)
    assert len(rows) == 3

    response = client.post(queue, changelist_post(client, rows, {rows[0].pk: bouchard.pk}), follow=True)

    assert response.status_code == 200
    assert not AttendanceRecord.objects.unmatched().filter(event=second).exists()
    assert PersonEmail.objects.get(email="lea.bouchard@hospital.example").person == bouchard
    assert attended_minutes(bouchard, second).minutes == 178
    entry = AuditLog.objects.filter(action="attendance.matched", actor_user=boss).first()
    assert entry is not None
    messages = [str(m) for m in response.context["messages"]]
    assert any("2 other row(s)" in m for m in messages)


def test_what_teams_recorded_cannot_be_changed_through_the_form(client, boss, seeded):
    row = AttendanceRecord.objects.filter(source="teams_upload", person__isnull=False).first()
    before = AttendanceRecord.objects.filter(pk=row.pk).values(*AttendanceRecord.FROZEN_FIELDS).get()
    other = who("Roy")
    client.post(
        url(AttendanceRecord, "change", row.pk),
        {
            "person": other.pk,
            "raw_display_name": "Tampered",
            "duration_seconds": 99999,
            "join_at_0": "2020-01-01",
            "join_at_1": "00:00:00",
            "supersedes-TOTAL_FORMS": 0,
            "supersedes-INITIAL_FORMS": 0,
        },
    )
    row.refresh_from_db()
    assert row.person == other  # the interpretation changed
    assert (
        AttendanceRecord.objects.filter(pk=row.pk).values(*AttendanceRecord.FROZEN_FIELDS).get()
        == before
    )
    assert AuditLog.objects.filter(action="attendance.rematched", object_id=str(row.pk)).exists()


def test_attendance_rows_cannot_be_deleted_in_the_admin(client, boss, seeded):
    row = AttendanceRecord.objects.first()
    assert client.get(url(AttendanceRecord, "delete", row.pk)).status_code == 403
    assert b"delete_selected" not in client.get(url(AttendanceRecord, "changelist")).content


# --- Adding rows by hand -----------------------------------------------------


def manual_form(event, person, **fields):
    data = {
        "event": event.pk,
        "source": "manual",
        "person": person.pk,
        "attributed_to": "",
        "join_at_0": "",
        "join_at_1": "",
        "leave_at_0": "",
        "leave_at_1": "",
        "duration_minutes": "",
        "session": "",
        "reason": "Chair confirms attendance",
        "supersedes-TOTAL_FORMS": 0,
        "supersedes-INITIAL_FORMS": 0,
        "supersedes-MIN_NUM_FORMS": 0,
        "supersedes-MAX_NUM_FORMS": 1000,
    }
    data.update(fields)
    return data


def test_adding_an_hours_only_manual_row(client, boss, seeded):
    first = seeded[0]
    roy = who("Roy")
    session = first.sessions.order_by("start_at").first()
    response = client.post(
        url(AttendanceRecord, "add"),
        manual_form(first, roy, duration_minutes="20", session=session.pk),
    )
    assert response.status_code == 302, response.context["adminform"].form.errors
    row = AttendanceRecord.objects.filter(person=roy, source="manual").get()
    assert (row.duration_seconds, row.join_at, row.session, row.created_by) == (
        20 * 60,
        None,
        session,
        boss,
    )
    assert (row.match_method, row.matched_by) == ("manual", boss)
    assert AuditLog.objects.filter(
        action="attendance.manual_row_created", object_id=str(row.pk), actor_user=boss
    ).exists()


def test_a_manual_row_needs_a_reason_and_a_duration(client, boss, seeded):
    first = seeded[0]
    roy = who("Roy")
    count = AttendanceRecord.objects.count()
    session = first.sessions.order_by("start_at").first()
    for bad in (
        manual_form(first, roy, duration_minutes="20", session=session.pk, reason=""),
        manual_form(first, roy),
        manual_form(first, roy, duration_minutes="20"),  # minutes without a session
        manual_form(first, roy, duration_minutes="20", session=session.pk,
                    join_at_0="2026-09-15", join_at_1="12:00:00",
                    leave_at_0="2026-09-15", leave_at_1="12:30:00"),
        manual_form(first, roy, source="teams_upload", duration_minutes="20", session=session.pk),
    ):
        response = client.post(url(AttendanceRecord, "add"), bad)
        assert response.status_code == 200  # redisplayed with errors, not a crash
        assert response.context["adminform"].form.errors
    assert AttendanceRecord.objects.count() == count


def test_adding_a_room_roster_row_copies_the_device_times(client, boss, seeded):
    first = seeded[0]
    device = AttendanceRecord.objects.get(event=first, raw_display_name="Conference Room B")
    morin = who("Morin")
    response = client.post(
        url(AttendanceRecord, "add"),
        manual_form(first, morin, source="room_roster", attributed_to=device.pk,
                    reason="On the Room B sheet"),
    )
    assert response.status_code == 302, response.context["adminform"].form.errors
    row = AttendanceRecord.objects.get(person=morin, source="room_roster")
    assert (row.join_at, row.leave_at) == (device.join_at, device.leave_at)
    assert attended_minutes(morin, first).minutes == 174  # 12:03 to 14:57


def test_a_correction_added_in_the_admin_replaces_the_rows_it_names(client, boss, seeded):
    first = seeded[0]
    cote = who("Côté")
    rejoins = list(AttendanceRecord.objects.filter(event=first, person=cote))
    assert attended_minutes(cote, first).minutes == 171
    first_session = first.sessions.order_by("start_at").first()
    data = manual_form(
        first, cote, duration_minutes="60", session=first_session.pk,
        reason="Teams dropped him twice; he was in the room for the whole first talk",
    )
    data["supersedes-TOTAL_FORMS"] = len(rejoins)
    for index, old in enumerate(rejoins):
        data[f"supersedes-{index}-old"] = str(old.pk)
    response = client.post(url(AttendanceRecord, "add"), data)
    assert response.status_code == 302, response.context["adminform"].form.errors
    assert AttendanceSupersession.objects.filter(old__in=rejoins, created_by=boss).count() == 3
    assert [s.minutes for s in attended_minutes(cote, first).sessions] == [60, 0, 0]
    assert AuditLog.objects.filter(action="attendance.superseded", actor_user=boss).count() == 1


def test_a_row_already_superseded_cannot_be_replaced_again(client, boss, seeded):
    first = seeded[0]
    sharma = who("Sharma")
    old = AttendanceRecord.objects.superseded().filter(person=sharma).first()
    data = manual_form(
        first, sharma, duration_minutes="60", session=first.sessions.order_by("start_at").first().pk
    )
    data["supersedes-TOTAL_FORMS"] = 1
    data["supersedes-0-old"] = str(old.pk)
    response = client.post(url(AttendanceRecord, "add"), data)
    assert response.status_code == 200
    assert AttendanceSupersession.objects.filter(old=old).count() == 1


# --- Uploads, adjustments, declarations --------------------------------------


def fixture_event():
    """An event matching the real fixture: 2026-09-10, 9:00 to 12:00, same Teams title."""
    from attendance.tests.test_teams import make_fixture_event

    return make_fixture_event()


def test_uploading_an_export_previews_first_then_stores_on_confirm(client, boss, seeded, settings):
    from attendance.tests.test_teams import FIXTURE

    event = fixture_event()
    content = FIXTURE.read_bytes()

    def post(event_pk=""):
        return client.post(
            url(AttendanceUpload, "add"),
            {"event": event_pk, "file": SimpleUploadedFile("teams-export.csv", content)},
        )

    response = post()  # no event chosen: found by title and date
    assert response.status_code == 302, response.context["adminform"].form.errors
    assert "/preview/" in response.url
    # Nothing is stored until the reviewer confirms: no row, no file, no log entry.
    before = (AttendanceUpload.objects.count(), AttendanceRecord.objects.count())
    assert not AttendanceUpload.objects.filter(original_filename="teams-export.csv").exists()
    assert not AttendanceRecord.objects.filter(event=event).exists()
    assert not AuditLog.objects.filter(action="attendance.upload_stored", actor_user=boss).exists()
    assert (AttendanceUpload.objects.count(), AttendanceRecord.objects.count()) == before

    preview = client.get(response.url)
    assert preview.status_code == 200
    page = preview.content.decode()
    assert "Nothing has been stored yet" in page
    assert "month/day/year" in page  # the date reading chosen, and why
    assert "The organizer and every participant" in page  # the rule, stated
    assert "matches nobody" in page  # the unmatched rows are flagged
    assert str(event.pk) in page or event.title in page

    confirmed = client.post(response.url, {"action": "confirm"})
    assert confirmed.status_code == 302
    upload = AttendanceUpload.objects.get(original_filename="teams-export.csv")
    assert confirmed.url == url(AttendanceUpload, "change", upload.pk)
    assert (upload.event, upload.uploaded_by, upload.row_count) == (event, boss, 12)
    assert (settings.UPLOAD_ROOT / upload.stored_path).read_bytes() == content
    assert upload.records.count() == 12
    assert client.get(response.url).status_code == 404  # the pending copy is gone

    again = post(event.pk)
    assert again.status_code == 200
    assert "already uploaded" in str(again.context["adminform"].form.errors)
    page = client.get(url(AttendanceUpload, "change", upload.pk)).content.decode()
    assert "Parser warnings" in page
    assert client.get(url(AttendanceUpload, "delete", upload.pk)).status_code == 403


def test_cancelling_a_preview_stores_nothing(client, boss, seeded, settings):
    from attendance.tests.test_teams import FIXTURE

    fixture_event()
    response = client.post(
        url(AttendanceUpload, "add"),
        {"event": "", "file": SimpleUploadedFile("teams-export.csv", FIXTURE.read_bytes())},
    )
    assert response.status_code == 302
    cancelled = client.post(response.url, {"action": "cancel"})
    assert cancelled.status_code == 302
    assert not AttendanceUpload.objects.filter(original_filename="teams-export.csv").exists()
    assert not AttendanceRecord.objects.filter(event__date=datetime.date(2026, 9, 10)).exists()
    assert not list((settings.UPLOAD_ROOT / "pending").glob("*"))
    assert client.get(response.url).status_code == 404


def test_a_preview_shows_what_a_second_file_would_disagree_with(client, boss, seeded, settings):
    """Rows already stored for a person show beside the file's figure, and a gap is flagged."""
    from attendance.tests.test_teams import FIXTURE

    event = fixture_event()
    session = event.sessions.order_by("position").first()
    person = Person.objects.create(given_name="Vincent", family_name="Marchand")
    PersonEmail.objects.create(person=person, email="vincent.marchand@mcgill.ca")
    AttendanceRecord.objects.create(
        event=event,
        session=session,
        person=person,
        source=AttendanceRecord.Source.SIGNIN_SHEET,
        created_by=boss,
    )
    response = client.post(
        url(AttendanceUpload, "add"),
        {"event": event.pk, "file": SimpleUploadedFile("teams-export.csv", FIXTURE.read_bytes())},
    )
    page = client.get(response.url).content.decode()
    assert "Vincent Marchand" in page  # 10 min on Teams, a whole-session tick on paper
    assert "stored:" in page and "sign-in sheet" in page
    assert "the stored sign-in sheet claim says" in page


def test_the_preview_is_scoped_to_the_viewer_s_programs(client, seeded, settings):
    """A coordinator of another program cannot see a preview stashed for this one."""
    from attendance import preview as pending
    from attendance.tests.test_teams import FIXTURE
    from programs.tests.factories import give_role, make_program

    event = fixture_event()
    sha = pending.stash(FIXTURE.read_bytes(), "teams-export.csv", event_id=event.pk, user_id=0)
    other = make_program("Other")
    user = get_user_model().objects.create_user(username="elsewhere", password="x" * 20, is_staff=True)
    give_role(user, other, "coordinator")
    client.force_login(user)
    response = client.get(reverse("admin:attendance_attendanceupload_preview", args=[sha]))
    assert response.status_code == 404
    assert pending.load(sha) is not None  # and it was not discarded by the attempt


def test_an_export_for_another_event_is_refused_and_nothing_is_stored(client, boss, seeded, settings):
    from attendance.tests.test_teams import FIXTURE

    wrong = seeded[0]  # a different date
    response = client.post(
        url(AttendanceUpload, "add"),
        {"event": wrong.pk, "file": SimpleUploadedFile("teams-export.csv", FIXTURE.read_bytes())},
    )
    assert response.status_code == 200
    assert "Is this the right event" in str(response.context["adminform"].form.errors)
    assert not AttendanceUpload.objects.filter(original_filename="teams-export.csv").exists()
    teams_dir = settings.UPLOAD_ROOT / "teams"
    stored = list(teams_dir.glob("*")) if teams_dir.exists() else []
    assert len(stored) == AttendanceUpload.objects.count()  # no file without its row
    pending_dir = settings.UPLOAD_ROOT / "pending"
    assert not (pending_dir.exists() and list(pending_dir.glob("*")))  # nothing stashed either


# --- Sign-off ----------------------------------------------------------------


def signoff_url(event):
    return reverse("admin:rounds_roundsevent_signoff", args=[event.pk])


def import_fixture(client, event):
    from attendance.tests.test_teams import FIXTURE

    response = client.post(
        url(AttendanceUpload, "add"),
        {"event": event.pk, "file": SimpleUploadedFile("teams-export.csv", FIXTURE.read_bytes())},
    )
    assert client.post(response.url, {"action": "confirm"}).status_code == 302
    return AttendanceUpload.objects.get(original_filename="teams-export.csv")


def test_sign_off_page_lists_people_and_holds_back_what_needs_a_look(client, boss, seeded):
    from attendance.models import SessionAttendanceDecision

    event = fixture_event()
    camille = Person.objects.create(given_name="Camille", family_name="Thibault")
    PersonEmail.objects.create(person=camille, email="camille.thibault@mcgill.ca")
    import_fixture(client, event)
    page = client.get(signoff_url(event))
    assert page.status_code == 200
    html = page.content.decode()
    assert "Thibault, Camille" in html
    # Unmatched rows block the sessions they touch; the page says so and links the queue.
    unmatched = AttendanceRecord.objects.active().unmatched().filter(event=event).count()
    assert unmatched > 0
    assert f"{unmatched} attendance row(s) are still unmatched" in html
    assert "matched=no" in html
    assert "the session has unmatched rows" in html
    # Nothing can be bulk-confirmed while the queue is not empty.
    assert "disabled" in html
    client.post(signoff_url(event), {"action": "confirm_event"})
    assert SessionAttendanceDecision.objects.count() == 0


def clear_queue(event, user):
    """Match every unmatched row to a fresh person so sign-off can proceed."""
    from attendance.services import match_record

    for row in AttendanceRecord.objects.active().unmatched().filter(event=event):
        words = row.raw_display_name.replace(",", "").split()
        person = Person.objects.create(given_name=words[0], family_name=words[1])
        match_record(row, person, user=user)


def test_confirm_event_confirms_agreeing_rows_and_holds_the_rest(client, boss, seeded):
    from attendance.models import SessionAttendanceDecision
    from attendance.signoff import review

    event = fixture_event()
    import_fixture(client, event)
    clear_queue(event, boss)
    ready = sum(1 for pr in review(event) for s in pr.sessions if s.can_bulk_confirm)
    assert ready > 0
    response = client.post(signoff_url(event), {"action": "confirm_event"})
    assert response.status_code == 302
    assert SessionAttendanceDecision.objects.count() == ready
    assert set(SessionAttendanceDecision.objects.values_list("basis", flat=True)) == {"sources_agree"}
    assert set(SessionAttendanceDecision.objects.values_list("confirmed_by", flat=True)) == {boss.pk}
    assert AuditLog.objects.filter(action="attendance.event_confirmed").count() == 1
    html = client.get(signoff_url(event)).content.decode()
    assert "(signed off)" in html
    # A second press does nothing: every row is already decided.
    client.post(signoff_url(event), {"action": "confirm_event"})
    assert SessionAttendanceDecision.objects.count() == ready


def test_confirm_person_takes_a_figure_and_needs_a_comment_when_it_differs(client, boss, seeded):
    from attendance.models import SessionAttendanceDecision

    event = fixture_event()
    import_fixture(client, event)
    clear_queue(event, boss)
    person = AttendanceRecord.objects.active().filter(event=event, person__isnull=False).first().person
    session = event.sessions.order_by("position").first()

    def post(**extra):
        return client.post(
            signoff_url(event),
            {"action": "confirm_person", "person": person.pk, f"include_{session.pk}": "1", **extra},
            follow=True,
        )

    # A figure that differs from the proposal without a comment is refused and nothing is written.
    response = post(**{f"minutes_{session.pk}": "7"})
    assert "comment" in response.content.decode().lower()
    assert SessionAttendanceDecision.objects.count() == 0

    response = post(**{f"minutes_{session.pk}": "7", "comment": "Was in the room; Teams dropped them."})
    assert "Signed off 1 session(s)" in response.content.decode()
    decision = SessionAttendanceDecision.objects.get()
    assert (decision.person, decision.session, decision.confirmed_minutes, decision.basis) == (
        person, session, 7, "manual",
    )
    assert decision.confirmed_by == boss
    assert AuditLog.objects.filter(action="attendance.person_confirmed").count() == 1

    # Signing off again supersedes rather than edits; both rows stay.
    post(**{f"minutes_{session.pk}": "8", "comment": "Second look."})
    assert SessionAttendanceDecision.objects.count() == 2
    assert SessionAttendanceDecision.objects.current().get().confirmed_minutes == 8
    assert SessionAttendanceDecision.objects.current().get().supersedes == decision


def test_sign_off_is_scoped_to_the_viewer_s_programs(client, seeded):
    from programs.tests.factories import give_role, make_program

    event = fixture_event()
    other = make_program("Other")
    user = get_user_model().objects.create_user(username="elsewhere", password="x" * 20, is_staff=True)
    give_role(user, other, "coordinator")
    client.force_login(user)
    assert client.get(signoff_url(event)).status_code == 404
    assert client.post(signoff_url(event), {"action": "confirm_event"}).status_code == 404


def test_read_only_staff_cannot_sign_off(client, seeded):
    from attendance.models import SessionAttendanceDecision
    from programs.tests.factories import give_role

    event = fixture_event()
    user = get_user_model().objects.create_user(username="reader", password="x" * 20, is_staff=True)
    give_role(user, event.program, "read_only")
    client.force_login(user)
    assert client.get(signoff_url(event)).status_code == 403
    assert client.post(signoff_url(event), {"action": "confirm_event"}).status_code == 403
    assert SessionAttendanceDecision.objects.count() == 0


def test_a_credit_adjustment_records_who_and_why_and_is_then_frozen(client, boss, seeded):
    first = seeded[0]
    roy = who("Roy")
    response = client.post(
        url(CreditAdjustment, "add"),
        {"person": roy.pk, "event": first.pk, "kind": "attendance", "delta_credits": "0.50",
         "reason": "Chair approved"},
    )
    assert response.status_code == 302, response.context["adminform"].form.errors
    adjustment = CreditAdjustment.objects.get(person=roy)
    assert adjustment.created_by == boss
    assert AuditLog.objects.filter(
        action="credit.adjusted", object_id=str(adjustment.pk), actor_user=boss
    ).exists()
    client.post(
        url(CreditAdjustment, "change", adjustment.pk),
        {"person": roy.pk, "event": first.pk, "kind": "attendance", "delta_credits": "9.00",
         "reason": "edited"},
    )
    adjustment.refresh_from_db()
    assert str(adjustment.delta_credits) == "0.50"
    bad = client.post(
        url(CreditAdjustment, "add"),
        {"person": roy.pk, "event": first.pk, "kind": "attendance", "delta_credits": "0",
         "reason": "does nothing"},
    )
    assert bad.status_code == 200 and bad.context["adminform"].form.errors


def test_a_declaration_entered_by_staff_is_logged_and_cannot_be_edited(client, boss, seeded):
    haddad = who("Haddad")
    form = {
        "person": haddad.pk,
        "declared_at_0": "2026-10-01",
        "declared_at_1": "09:00:00",
        "disclosure_text_version": "2026-10",
        "q_equity": "on",  # a yes with no explanation: refused
    }
    refused = client.post(url(COIDeclaration, "add"), form)
    assert refused.status_code == 200
    assert "q_equity_details" in refused.context["adminform"].form.errors

    form["q_equity_details"] = "Shares in Acme Devices"
    response = client.post(url(COIDeclaration, "add"), form)
    assert response.status_code == 302, response.context["adminform"].form.errors
    declaration = COIDeclaration.objects.get(person=haddad)
    assert declaration.is_complete
    assert [yes for _, yes, _ in declaration.rendered()] == [False, False, False, True, False, False, False]
    assert declaration.responses.get(question_key="equity").details == "Shares in Acme Devices"
    entry = AuditLog.objects.get(action="coi.declared", object_id=str(declaration.pk))
    assert (entry.actor_user, entry.metadata["entered_by_staff"]) == (boss, True)

    page = client.get(url(COIDeclaration, "change", declaration.pk)).content.decode()
    assert "Equity or ownership" in page and "Shares in Acme Devices" in page
    client.post(url(COIDeclaration, "change", declaration.pk), {"q_other": "on", "q_other_details": "x"})
    assert declaration.responses.get(question_key="other").has_conflict is False


def test_nothing_ticked_is_an_explicit_no_to_every_question(client, boss, seeded):
    roy = who("Roy")
    response = client.post(
        url(COIDeclaration, "add"),
        {"person": roy.pk, "declared_at_0": "2026-10-01", "declared_at_1": "09:00:00",
         "disclosure_text_version": "2026-10"},
    )
    assert response.status_code == 302, response.context["adminform"].form.errors
    declaration = COIDeclaration.objects.get(person=roy)
    assert declaration.responses.count() == 7
    assert declaration.has_conflict is False and declaration.is_complete


def test_a_duplicate_licence_is_reported_on_the_form(client, boss, seeded):
    response = client.post(
        url(Person, "add"),
        {
            "given_name": "M.",
            "family_name": "Tremblay",
            "role": "physician",
            "credential": "MD",
            "licence_jurisdiction": "CMQ",
            "licence_number": " 1234",  # same as 01234 once normalized
            "emails-TOTAL_FORMS": 0,
            "emails-INITIAL_FORMS": 0,
        },
    )
    assert response.status_code == 200
    assert "licence_number" in response.context["adminform"].form.errors


# --- Merging -----------------------------------------------------------------


def merge_post(client, people, **extra):
    data = {"action": "merge_selected", "_selected_action": [str(p.pk) for p in people]}
    data.update(extra)
    return client.post(url(Person, "changelist"), data, follow=True)


def test_merge_shows_both_records_then_merges_on_confirmation(client, boss, seeded):
    main, duplicate = Person.objects.filter(family_name="Tremblay").order_by("created_at")

    page = merge_post(client, [main, duplicate])
    text = page.content.decode()
    assert "Merge two people" in text
    assert "m.tremblay@example.com" in text and "marie.tremblay@example.org" in text
    duplicate.refresh_from_db()
    assert duplicate.merged_into is None  # nothing happens without confirmation

    done = merge_post(client, [main, duplicate], confirm="1", survivor=str(main.pk))
    duplicate.refresh_from_db()
    assert duplicate.merged_into == main
    assert set(main.emails.values_list("email", flat=True)) == {
        "marie.tremblay@example.org",
        "m.tremblay@example.com",
    }
    assert AuditLog.objects.filter(action="person.merged", actor_user=boss).count() == 1
    assert any("Merged into" in str(m) for m in done.context["messages"])


def test_merge_needs_exactly_two_people(client, boss, seeded):
    people = list(Person.objects.all()[:3])
    page = merge_post(client, people, confirm="1", survivor=str(people[0].pk))
    assert any("exactly two" in str(m) for m in page.context["messages"])
    assert not Person.objects.filter(merged_into__isnull=False).exists()


def test_merge_with_a_collision_is_refused_and_shows_both_rows(client, boss, seeded):
    main, duplicate = Person.objects.filter(family_name="Tremblay").order_by("created_at")
    theirs = EvaluationSubmission.objects.filter(person=main, session__event=seeded[1]).get()
    EvaluationSubmission.objects.create(
        person=duplicate, session=theirs.session, self_reported_session_minutes=70, attestation=True
    )
    page = merge_post(client, [main, duplicate], confirm="1", survivor=str(main.pk))
    text = page.content.decode()
    assert "This merge cannot go ahead yet" in text
    assert text.count(reverse("admin:credits_evaluationsubmission_changelist")) >= 2
    duplicate.refresh_from_db()
    assert duplicate.merged_into is None
    assert (
        EvaluationSubmission.objects.filter(
            session=theirs.session, person__in=[main, duplicate]
        ).count()
        == 2
    )


# --- Evaluation windows, closed events, superseded device rows --------------


def test_staff_can_reopen_a_form_as_an_override_and_it_is_logged(client, boss, seeded):
    first = seeded[0]
    roy = who("Roy")
    session = first.sessions.order_by("start_at").last()
    response = client.post(
        url(EvaluationWindow, "add"),
        {"person": roy.pk, "session": session.pk, "reason": "Asked by email; audit pending"},
    )
    assert response.status_code == 302, response.context["adminform"].form.errors
    window = EvaluationWindow.objects.get(person=roy, session=session)
    assert window.granted_by == boss
    entry = AuditLog.objects.get(action="evaluation.window_granted", object_id=str(window.pk))
    assert (entry.actor_user, entry.metadata["override"]) == (boss, True)
    # Frozen afterwards: no change form, no delete.
    assert client.get(url(EvaluationWindow, "delete", window.pk)).status_code == 403


def test_an_evaluation_entered_by_staff_needs_an_open_window(client, boss, seeded):
    first = seeded[0]  # four weeks ago: the default week is long gone
    roy = who("Roy")
    session = first.sessions.order_by("start_at").last()
    form = {
        "person": roy.pk,
        "session": session.pk,
        "submitted_at_0": "2026-10-06",
        "submitted_at_1": "12:00:00",
        "self_reported_session_minutes": 57,
        "attestation": "on",
        "is_complete": "on",
        "responses-TOTAL_FORMS": 0,
        "responses-INITIAL_FORMS": 0,
    }
    refused = client.post(url(EvaluationSubmission, "add"), form)
    assert refused.status_code == 200
    assert "window" in str(refused.context["adminform"].form.errors)

    client.post(url(EvaluationWindow, "add"), {"person": roy.pk, "session": session.pk, "reason": "x"})
    accepted = client.post(url(EvaluationSubmission, "add"), form)
    assert accepted.status_code == 302, accepted.context["adminform"].form.errors
    window = EvaluationWindow.objects.get(person=roy, session=session)
    assert window.closed_at is not None  # a complete submission closes it


def test_a_closed_event_cannot_be_reopened_from_the_admin(client, boss, seeded):
    first = seeded[0]
    assert first.status == "closed"
    response = client.post(
        url(RoundsEvent, "change", first.pk),
        {
            "program": first.program.pk,
            "title": first.title,
            "date": first.date.isoformat(),
            "status": "draft",
            "accredited_credits": "3.00",
            "start_at_0": "2026-09-08",
            "start_at_1": "12:00:00",
            "end_at_0": "2026-09-08",
            "end_at_1": "15:00:00",
            "teams_join_url": "",
            "teams_meeting_title": "",
            "sessions-TOTAL_FORMS": 0,
            "sessions-INITIAL_FORMS": 0,
        },
    )
    assert response.status_code == 200
    assert "status" in response.context["adminform"].form.errors
    first.refresh_from_db()
    assert first.status == "closed"


def test_a_room_roster_row_warns_when_its_device_row_was_superseded(client, boss, seeded):
    first = seeded[0]
    device = AttendanceRecord.objects.get(event=first, raw_display_name="Conference Room B")
    roster = AttendanceRecord.objects.filter(attributed_to=device).first()
    page = client.get(url(AttendanceRecord, "change", roster.pk)).content.decode()
    assert "was superseded" not in page
    correction = manual_form(
        first, who("Lavoie"), duration_minutes="30",
        session=first.sessions.order_by("start_at").first().pk,
        reason="The room device's times were wrong",
    )
    correction["supersedes-TOTAL_FORMS"] = 1
    correction["supersedes-0-old"] = str(device.pk)
    assert client.post(url(AttendanceRecord, "add"), correction).status_code == 302
    page = client.get(url(AttendanceRecord, "change", roster.pk)).content.decode()
    assert "was superseded" in page
    listing = client.get(url(AttendanceRecord, "changelist") + "?source__exact=room_roster").content.decode()
    assert listing.count("was superseded") == 2  # both people behind the device


def test_person_page_shows_earned_versus_certified(client, boss, seeded):
    page = client.get(url(Person, "change", who("Haddad").pk)).content.decode()
    assert "Credit, earned versus certified" in page
    assert "not yet certified" in page


# --- Entering the next event ------------------------------------------------


def test_adding_an_event_needs_only_a_start_and_three_titles(client, boss, seeded):
    response = client.post(
        url(RoundsEvent, "add"),
        {
            "program": seeded[0].program.pk,
            "title": "Health Informatics Rounds",
            "date": "",
            "status": "draft",
            "accredited_credits": "3.00",
            "start_at_0": "2026-10-08",
            "start_at_1": "09:00:00",
            "end_at_0": "",
            "end_at_1": "",
            "teams_join_url": "https://teams.microsoft.com/meet/demo",
            "teams_meeting_title": "",
            "sessions-TOTAL_FORMS": 3,
            "sessions-INITIAL_FORMS": 0,
            "sessions-0-position": 1,
            "sessions-0-title": "First talk",
            "sessions-1-position": 2,
            "sessions-1-title": "Second talk",
            "sessions-2-position": 3,
            "sessions-2-title": "Third talk",
        },
    )
    assert response.status_code == 302, response.context["adminform"].form.errors
    event = RoundsEvent.objects.get(title="Health Informatics Rounds", date="2026-10-08")
    local = lambda value: timezone.localtime(value).strftime("%H:%M")
    assert (local(event.start_at), local(event.end_at)) == ("09:00", "12:00")
    assert [
        (s.position, s.title, local(s.start_at), local(s.end_at))
        for s in event.sessions.order_by("start_at")
    ] == [
        (1, "First talk", "09:00", "10:00"),
        (2, "Second talk", "10:00", "11:00"),
        (3, "Third talk", "11:00", "12:00"),
    ]


def test_an_empty_session_row_is_ignored_and_a_given_time_is_kept(client, boss, seeded):
    response = client.post(
        url(RoundsEvent, "add"),
        {
            "program": seeded[0].program.pk,
            "title": "Short rounds",
            "status": "draft",
            "accredited_credits": "2.00",
            "start_at_0": "2026-10-22",
            "start_at_1": "12:00:00",
            "sessions-TOTAL_FORMS": 3,
            "sessions-INITIAL_FORMS": 0,
            "sessions-0-position": 1,
            "sessions-0-title": "Only talk",
            "sessions-1-position": 2,
            "sessions-1-title": "Late talk",
            "sessions-1-start_at_0": "2026-10-22",
            "sessions-1-start_at_1": "13:30:00",
            "sessions-2-position": 3,
        },
    )
    assert response.status_code == 302, response.context["adminform"].form.errors
    event = RoundsEvent.objects.get(title="Short rounds")
    local = lambda value: timezone.localtime(value).strftime("%H:%M")
    assert [(s.title, local(s.start_at), local(s.end_at)) for s in event.sessions.order_by("start_at")] == [
        ("Only talk", "12:00", "13:00"),
        ("Late talk", "13:30", "14:30"),
    ]


def test_a_session_added_on_its_own_page_follows_the_previous_one(client, boss, seeded):
    first = seeded[0]
    response = client.post(
        url(Session, "add"),
        {
            "event": first.pk,
            "position": 4,
            "title": "Bonus talk",
            "draft_blurb": "",
            "published_blurb": "",
            "session_presenters-TOTAL_FORMS": 0,
            "session_presenters-INITIAL_FORMS": 0,
            "objectives-TOTAL_FORMS": 0,
            "objectives-INITIAL_FORMS": 0,
        },
    )
    # The seeded event ends at 15:00 and its last talk ends at 15:00, so a
    # fourth hour does not fit: the form says so instead of crashing.
    assert response.status_code == 200
    assert "start_at" in response.context["adminform"].form.errors
    first.end_at = first.end_at + datetime.timedelta(hours=1)
    first.save()
    response = client.post(url(Session, "add"), {
        "event": first.pk, "position": 4, "title": "Bonus talk",
        "draft_blurb": "", "published_blurb": "",
        "session_presenters-TOTAL_FORMS": 0, "session_presenters-INITIAL_FORMS": 0,
        "objectives-TOTAL_FORMS": 0, "objectives-INITIAL_FORMS": 0,
    })
    assert response.status_code == 302, response.context["adminform"].form.errors
    bonus = Session.objects.get(title="Bonus talk")
    assert timezone.localtime(bonus.start_at).strftime("%H:%M") == "15:00"
    assert timezone.localtime(bonus.end_at).strftime("%H:%M") == "16:00"


def test_positions_number_themselves(client, boss, seeded):
    """Sessions on an event, then presenters and objectives on a session: no numbers typed."""
    response = client.post(
        url(RoundsEvent, "add"),
        {
            "program": seeded[0].program.pk,
            "title": "Numbered for me",
            "status": "draft",
            "accredited_credits": "3.00",
            "start_at_0": "2026-11-05",
            "start_at_1": "12:00:00",
            "sessions-TOTAL_FORMS": 3,
            "sessions-INITIAL_FORMS": 0,
            "sessions-0-title": "A",
            "sessions-1-title": "B",
            "sessions-2-title": "",  # left empty: ignored
        },
    )
    assert response.status_code == 302, response.context["adminform"].form.errors
    event = RoundsEvent.objects.get(title="Numbered for me")
    assert [(s.position, s.title) for s in event.sessions.order_by("position")] == [(1, "A"), (2, "B")]

    session = event.sessions.get(title="A")
    haddad, sharma = who("Haddad"), who("Sharma")
    response = client.post(
        url(Session, "change", session.pk),
        {
            "event": event.pk,
            "position": "",  # blank on the session itself keeps its number too
            "title": "A",
            "start_at_0": "2026-11-05", "start_at_1": "12:00:00",
            "end_at_0": "2026-11-05", "end_at_1": "13:00:00",
            "draft_blurb": "", "published_blurb": "",
            "session_presenters-TOTAL_FORMS": 2,
            "session_presenters-INITIAL_FORMS": 0,
            "session_presenters-0-person": haddad.pk,
            "session_presenters-1-person": sharma.pk,
            "session_presenters-1-position": 5,  # a typed number is kept; blanks follow it
            "objectives-TOTAL_FORMS": 3,
            "objectives-INITIAL_FORMS": 0,
            "objectives-0-text": "First objective",
            "objectives-1-text": "Second objective",
            "objectives-2-text": "",
        },
    )
    assert response.status_code == 302, (
        response.context["adminform"].form.errors,
        [f.errors for fs in response.context["inline_admin_formsets"] for f in fs.formset.forms],
    )
    assert [(p.position, p.person) for p in session.session_presenters.order_by("position")] == [
        (5, sharma),
        (6, haddad),
    ]
    assert [(o.position, o.text) for o in session.objectives.order_by("position")] == [
        (1, "First objective"),
        (2, "Second objective"),
    ]

    # Adding one more of each later continues the numbering.
    response = client.post(
        url(Session, "change", session.pk),
        {
            "event": event.pk, "position": 1, "title": "A",
            "start_at_0": "2026-11-05", "start_at_1": "12:00:00",
            "end_at_0": "2026-11-05", "end_at_1": "13:00:00",
            "draft_blurb": "", "published_blurb": "",
            "session_presenters-TOTAL_FORMS": 3,
            "session_presenters-INITIAL_FORMS": 2,
            "session_presenters-0-id": session.session_presenters.get(person=haddad).pk,
            "session_presenters-0-person": haddad.pk,
            "session_presenters-0-position": 6,
            "session_presenters-1-id": session.session_presenters.get(person=sharma).pk,
            "session_presenters-1-person": sharma.pk,
            "session_presenters-1-position": 5,
            "session_presenters-2-person": who("Gagnon").pk,
            "objectives-TOTAL_FORMS": 3,
            "objectives-INITIAL_FORMS": 2,
            "objectives-0-id": session.objectives.get(position=1).pk,
            "objectives-0-position": 1,
            "objectives-0-text": "First objective",
            "objectives-1-id": session.objectives.get(position=2).pk,
            "objectives-1-position": 2,
            "objectives-1-text": "Second objective",
            "objectives-2-text": "Third objective",
        },
    )
    assert response.status_code == 302
    assert session.session_presenters.get(person=who("Gagnon")).position == 7
    assert session.objectives.get(text="Third objective").position == 3


def test_a_coordinator_sees_the_review_but_only_a_program_admin_signs(client, seeded):
    from attendance.models import SessionAttendanceDecision
    from programs.tests.factories import give_role

    event = fixture_event()
    camille = Person.objects.create(given_name="Camille", family_name="Thibault")
    PersonEmail.objects.create(person=camille, email="camille.thibault@mcgill.ca")
    coordinator = get_user_model().objects.create_user(username="coord", password="x" * 20, is_staff=True)
    give_role(coordinator, event.program, "coordinator")
    client.force_login(coordinator)
    import_fixture(client, event)  # coordinators upload
    clear_queue(event, coordinator)  # and work the match queue

    html = client.get(signoff_url(event)).content.decode()
    assert "Review only" in html and "Thibault, Camille" in html
    assert 'name="action" value="confirm_event"' not in html
    assert "Sign off Camille" not in html
    assert client.post(signoff_url(event), {"action": "confirm_event"}).status_code == 403
    assert SessionAttendanceDecision.objects.count() == 0

    give_role(coordinator, event.program, "program_admin")
    html = client.get(signoff_url(event)).content.decode()
    assert 'name="action" value="confirm_event"' in html
    assert client.post(signoff_url(event), {"action": "confirm_event"}).status_code == 302
    assert SessionAttendanceDecision.objects.count() > 0


def test_person_page_lists_what_blocks_a_certificate_with_links(client, boss, seeded):
    from attendance.signoff import confirm_event

    event = fixture_event()
    camille = Person.objects.create(given_name="Camille", family_name="Thibault")
    PersonEmail.objects.create(person=camille, email="camille.thibault@mcgill.ca")
    import_fixture(client, event)
    clear_queue(event, boss)
    from credits.tests.factories import evaluate

    evaluate(camille, event.sessions.order_by("position").first())  # unevaluated minutes block nothing

    page = client.get(url(Person, "change", camille.pk)).content.decode()
    assert "Blocking a certificate" in page
    assert "1 event(s) must be signed off first" in page
    assert f'href="{signoff_url(event)}"' in page
    assert "2026-09-10" in page

    confirm_event(event, user=boss)
    page = client.get(url(Person, "change", camille.pk)).content.decode()
    assert "nothing. A certificate can be issued" in page
