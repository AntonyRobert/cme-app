"""
The paper sign-in sheet, typed in: what a tick writes, what re-submitting
does, where a walk-in goes, and who may do it.
"""
import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from audit.models import AuditLog
from people.models import Person
from programs.tests.factories import give_role, make_program

from ..aggregation import attended_minutes
from ..models import AttendanceRecord
from ..sheet import enter_sheet, existing_ticks, sheet_people
from ..signoff import HELD_TICK_ONLY, review
from .factories import teams_row
from people.tests.factories import make_staff
from rounds.tests.factories import EVENT_START, make_event, make_session

pytestmark = pytest.mark.django_db


def person(given, family):
    return Person.objects.create(given_name=given, family_name=family)


def three_session_event(day_offset=0, **kw):
    import datetime

    start = EVENT_START + datetime.timedelta(days=day_offset)
    event = make_event(start=start, minutes=180, credits="3.00", sessions=0, **kw)
    for _ in range(3):
        make_session(event, minutes=60)
    return event


def test_a_tick_claims_the_whole_session_with_no_upload_and_no_reason():
    event = three_session_event()
    s1, s2, _ = event.sessions.order_by("position")
    ada = person("Ada", "Lovelace")
    staff = make_staff()

    result = enter_sheet(event, {(ada, s1), (ada, s2)}, [], user=staff)

    assert result.written == 2 and result.already == 0 and result.walk_ins == 0
    rows = AttendanceRecord.objects.filter(person=ada).order_by("session__position")
    assert [(r.source, r.session, r.duration_seconds, r.upload, r.reason) for r in rows] == [
        ("signin_sheet", s1, s1.length_seconds, None, None),
        ("signin_sheet", s2, s2.length_seconds, None, None),
    ]
    assert {r.created_by for r in rows} == {staff}
    assert all(r.join_at is None and r.leave_at is None for r in rows)
    # Sign-off sees a sheet claim of the whole session, held as tick-only.
    assert attended_minutes(ada, event).sessions[0].claim_minutes == {"signin_sheet": 60}
    held = [s for pr in review(event) for s in pr.held]
    assert [s.session for s in held] == [s1, s2]
    assert HELD_TICK_ONLY in held[0].held_reasons


def test_resubmitting_the_same_ticks_adds_nothing():
    event = three_session_event()
    s1, s2, s3 = event.sessions.order_by("position")
    ada = person("Ada", "Lovelace")
    staff = make_staff()
    enter_sheet(event, {(ada, s1)}, [], user=staff)

    result = enter_sheet(event, {(ada, s1), (ada, s3)}, [], user=staff)

    assert (result.written, result.already) == (1, 1)
    assert existing_ticks(event) == {(ada.pk, s1.pk), (ada.pk, s3.pk)}
    assert AttendanceRecord.objects.filter(person=ada).count() == 2


def test_a_walk_in_is_an_unmatched_row_in_the_queue():
    event = three_session_event()
    s1, s2, _ = event.sessions.order_by("position")
    staff = make_staff()

    result = enter_sheet(event, set(), [("  Grace   Hopper ", [s1, s2]), ("   ", [s1])], user=staff)

    assert (result.written, result.walk_ins) == (2, 2)
    rows = AttendanceRecord.objects.active().unmatched().filter(event=event).order_by("session__position")
    assert [(r.raw_display_name, r.session, r.source, r.match_method) for r in rows] == [
        ("Grace Hopper", s1, "signin_sheet", "unmatched"),
        ("Grace Hopper", s2, "signin_sheet", "unmatched"),
    ]


def test_the_sitting_is_logged_once_with_every_row():
    event = three_session_event()
    s1, _, _ = event.sessions.order_by("position")
    ada = person("Ada", "Lovelace")
    staff = make_staff()

    result = enter_sheet(event, {(ada, s1)}, [("Grace Hopper", [s1])], user=staff)

    entry = AuditLog.objects.get(action="attendance.signin_sheet_entered")
    assert entry.actor_user == staff
    assert entry.object_id == str(event.pk)
    assert sorted(entry.metadata["rows"]) == sorted(str(r.pk) for r in result.rows)
    assert entry.metadata["written"] == 2 and entry.metadata["walk_ins"] == 1


def test_a_session_from_another_event_is_refused_and_nothing_is_written():
    event = three_session_event()
    other = three_session_event(day_offset=14)
    ada = person("Ada", "Lovelace")
    staff = make_staff()
    with pytest.raises(ValueError):
        enter_sheet(
            event, {(ada, event.sessions.first()), (ada, other.sessions.first())}, [], user=staff
        )
    assert AttendanceRecord.objects.filter(person=ada).count() == 0


def test_the_list_is_the_program_s_known_people_with_merged_duplicates_folded():
    event = three_session_event()
    s1 = event.sessions.order_by("position").first()
    ada = person("Ada", "Lovelace")
    twin = person("Ada", "Lovelace")
    stranger = person("Nobody", "Here")
    elsewhere = three_session_event(program=make_program("Other"))
    teams_row(event, ada, 0, 30)
    teams_row(event, twin, 0, 30)
    teams_row(elsewhere, stranger, 0, 30)
    twin.merged_into = ada
    twin.save()

    assert sheet_people(event.program) == [ada]


# --- The admin page ----------------------------------------------------------


def sheet_url(event):
    return reverse("admin:rounds_roundsevent_signin_sheet", args=[event.pk])


@pytest.fixture
def coordinator(client):
    user = get_user_model().objects.create_user(username="coord", password="x" * 20, is_staff=True)
    client.force_login(user)
    return user


def test_the_page_lists_people_with_a_box_per_session_and_records_ticks(client, coordinator):
    event = three_session_event()
    give_role(coordinator, event.program)
    s1, s2, s3 = event.sessions.order_by("position")
    ada = person("Ada", "Lovelace")
    teams_row(event, ada, 0, 30)

    page = client.get(sheet_url(event))
    assert page.status_code == 200
    html = page.content.decode()
    assert "Lovelace, Ada" in html
    assert f'name="tick_{ada.pk}_{s1.pk}"' in html
    assert "Not on the list" in html

    response = client.post(
        sheet_url(event),
        {
            f"tick_{ada.pk}_{s1.pk}": "1",
            f"tick_{ada.pk}_{s3.pk}": "1",
            "walkin_0_name": "Grace Hopper",
            f"walkin_0_{s2.pk}": "1",
            "walkin_1_name": "Nobody Ticked",  # a name with no tick writes nothing
        },
        follow=True,
    )
    assert "Recorded 3 tick(s), 1 of them for names not on the list" in response.content.decode()
    assert AttendanceRecord.objects.filter(person=ada, source="signin_sheet").count() == 2
    assert AttendanceRecord.objects.filter(raw_display_name="Grace Hopper").count() == 1
    assert not AttendanceRecord.objects.filter(raw_display_name="Nobody Ticked").exists()

    # The recorded ticks come back locked, and re-posting them adds nothing.
    html = client.get(sheet_url(event)).content.decode()
    assert html.count("Already recorded") == 2
    assert f'name="tick_{ada.pk}_{s2.pk}"' in html
    assert f'name="tick_{ada.pk}_{s1.pk}"' not in html
    client.post(sheet_url(event), {f"tick_{ada.pk}_{s1.pk}": "1"})
    assert AttendanceRecord.objects.filter(person=ada, source="signin_sheet").count() == 2


def test_the_page_is_scoped_to_the_viewer_s_programs(client, coordinator):
    event = three_session_event()
    give_role(coordinator, make_program("Other"))
    assert client.get(sheet_url(event)).status_code == 404
    assert client.post(sheet_url(event), {}).status_code == 404


def test_read_only_staff_cannot_enter_a_sheet(client, coordinator):
    event = three_session_event()
    give_role(coordinator, event.program, "read_only")
    ada = person("Ada", "Lovelace")
    s1 = event.sessions.first()
    assert client.get(sheet_url(event)).status_code == 403
    assert client.post(sheet_url(event), {f"tick_{ada.pk}_{s1.pk}": "1"}).status_code == 403
    assert AttendanceRecord.objects.count() == 0


def test_the_event_page_links_to_the_sheet(client, coordinator):
    event = three_session_event()
    give_role(coordinator, event.program)
    html = client.get(reverse("admin:rounds_roundsevent_change", args=[event.pk])).content.decode()
    assert sheet_url(event) in html
    assert "Import paper sign-in sheet" in html
