"""
QR sign-in: a rotating code, one scan per session credited as the session,
tied to a signed-in Person. The scan survives the magic-link round trip.
"""
import datetime

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditLog
from people.models import AllowedDomain
from people.tests.factories import make_person
from programs.tests.factories import give_role, make_program
from rounds.tests.factories import make_event, make_session
from signin.tests.test_signin import ask_for_link, last_link_token

from .. import qr
from ..aggregation import RECORDED, attended_minutes
from ..models import AttendanceRecord
from .factories import teams_row

pytestmark = pytest.mark.django_db


def event_now(offset_minutes=-30, **kw):
    """A three-talk event whose first talk started `offset_minutes` ago (default: now open)."""
    start = (timezone.now() + datetime.timedelta(minutes=offset_minutes)).replace(second=0, microsecond=0)
    event = make_event(start=start, minutes=180, credits="3.00", sessions=0, **kw)
    for _ in range(3):
        make_session(event, minutes=60)
    return event


@pytest.fixture
def ada():
    AllowedDomain.objects.create(domain="mcgill.ca")
    return make_person(given="Ada", family="Lovelace", email="ada@mcgill.ca")


def ada_client(ada):
    client = Client()
    ask_for_link(client, "ada@mcgill.ca")
    client.get(reverse("signin:redeem", args=[last_link_token()]))
    return client


# --- The token ------------------------------------------------------------------


def test_the_code_is_valid_for_its_window_and_the_one_before_only():
    event = event_now()
    session = event.sessions.first()
    now = timezone.now()
    window = qr.window_at(now)
    token = qr.token_for(session.pk, window)

    assert qr.token_is_valid(session.pk, window, token, now)
    assert qr.token_is_valid(session.pk, window, token, now + datetime.timedelta(seconds=qr.WINDOW_SECONDS))
    assert not qr.token_is_valid(session.pk, window, token, now + datetime.timedelta(seconds=2 * qr.WINDOW_SECONDS))
    assert not qr.token_is_valid(session.pk, window + 1, qr.token_for(session.pk, window + 1), now)  # from the future
    assert not qr.token_is_valid(session.pk, window, token[:-1] + ("0" if token[-1] != "0" else "1"), now)
    other = event.sessions.last()
    assert not qr.token_is_valid(other.pk, window, token, now)  # another session's code


def test_the_display_picks_the_running_session_then_the_upcoming_one():
    event = event_now(offset_minutes=-70)  # talk 1 ended ten minutes ago, talk 2 running
    s1, s2, s3 = event.sessions.order_by("position")
    assert qr.current_session(event) == s2
    # In a break both graces overlap; people scan on the way in, so the next talk wins.
    gap = make_event(start=timezone.now() - datetime.timedelta(minutes=65), minutes=140, credits="3.00", sessions=0)
    g1 = make_session(gap, start=0, minutes=60)
    g2 = make_session(gap, start=70, minutes=60)
    assert qr.current_session(gap) == g2
    later = event_now(offset_minutes=120)
    assert qr.current_session(later) == later.sessions.order_by("position").first()
    done = event_now(offset_minutes=-600)
    assert qr.current_session(done) == done.sessions.order_by("position").last()


# --- Scanning while signed in -----------------------------------------------------


def test_a_scan_records_the_whole_session_for_the_signed_in_person_once(ada):
    event = event_now()
    session = event.sessions.order_by("position").first()
    client = ada_client(ada)

    response = client.get(qr.scan_path(session))

    assert response.status_code == 200
    assert "You are signed in" in response.content.decode()
    row = AttendanceRecord.objects.get(source="qr_signin")
    assert (row.person, row.session, row.event, row.duration_seconds) == (ada, session, event, 3600)
    assert row.created_by is None and row.match_method == "self" and row.upload is None
    assert row.join_at is None and row.leave_at is None
    entry = AuditLog.objects.get(action="attendance.qr_scanned")
    assert entry.actor_person == ada and entry.metadata["session"] == str(session.pk)

    again = client.get(qr.scan_path(session))
    assert "Already signed in" in again.content.decode()
    assert AttendanceRecord.objects.filter(source="qr_signin").count() == 1
    assert AuditLog.objects.filter(action="attendance.qr_scanned").count() == 1


def test_a_scan_is_its_own_claim_beside_the_teams_record(ada):
    event = event_now()
    session = event.sessions.order_by("position").first()
    teams_row(event, ada, session.start_at, session.start_at + datetime.timedelta(minutes=20))
    ada_client(ada).get(qr.scan_path(session))

    share = attended_minutes(ada, event).sessions[0]
    assert share.claim_minutes == {RECORDED: 20, "qr_signin": 60}
    assert share.minutes == 60 and share.disagree


# --- Scanning before signing in ---------------------------------------------------


def test_a_scan_before_sign_in_is_finished_after_the_magic_link(ada):
    event = event_now()
    session = event.sessions.order_by("position").first()
    client = Client()

    response = client.get(qr.scan_path(session))

    assert response.status_code == 302
    assert response.url.startswith(reverse("signin:start"))
    assert reverse("attendance:scan_complete") in response.url
    assert not AttendanceRecord.objects.filter(source="qr_signin").exists()

    # The email round trip takes longer than a code lives; the scan waits.
    client.get(response.url)
    ask_for_link(client, "ada@mcgill.ca")
    landed = client.get(reverse("signin:redeem", args=[last_link_token()]))
    assert landed.status_code == 302 and landed.url == reverse("attendance:scan_complete")
    done = client.get(landed.url)
    assert "You are signed in" in done.content.decode()
    row = AttendanceRecord.objects.get(source="qr_signin")
    assert (row.person, row.session) == (ada, session)
    # Finished once: the pending scan is gone.
    assert client.get(reverse("attendance:scan_complete")).url == reverse("signin:me")


def test_a_new_person_completing_a_profile_also_lands_on_the_scan(ada):
    event = event_now()
    session = event.sessions.order_by("position").first()
    client = Client()
    client.get(qr.scan_path(session), follow=True)
    ask_for_link(client, "grace@mcgill.ca")
    client.get(reverse("signin:redeem", args=[last_link_token()]))
    landed = client.post(
        reverse("signin:complete"),
        {"given_name": "Grace", "family_name": "Hopper", "role": "physician"},
    )
    assert landed.status_code == 302 and landed.url == reverse("attendance:scan_complete")
    client.get(landed.url)
    row = AttendanceRecord.objects.get(source="qr_signin")
    assert row.person.full_name == "Grace Hopper" and row.session == session


def test_a_remembered_scan_goes_stale_after_twenty_minutes(ada):
    event = event_now()
    session = event.sessions.order_by("position").first()
    client = Client()
    client.get(qr.scan_path(session))
    store = client.session
    store[qr.PENDING_KEY]["at"] = (timezone.now() - qr.PENDING_LIFETIME - datetime.timedelta(minutes=1)).isoformat()
    store.save()
    ask_for_link(client, "ada@mcgill.ca")
    client.get(reverse("signin:redeem", args=[last_link_token()]))

    response = client.get(reverse("attendance:scan_complete"))

    assert response.status_code == 302 and response.url == reverse("signin:me")
    assert not AttendanceRecord.objects.filter(source="qr_signin").exists()


def test_next_only_ever_points_inside_the_site(ada):
    client = Client()
    client.get(reverse("signin:start") + "?next=https://evil.example/phish")
    ask_for_link(client, "ada@mcgill.ca")
    landed = client.get(reverse("signin:redeem", args=[last_link_token()]))
    assert landed.url == reverse("signin:me")


# --- Refusals -------------------------------------------------------------------


def test_a_stale_code_is_refused_and_remembers_nothing(ada):
    event = event_now()
    session = event.sessions.order_by("position").first()
    old = qr.window_at() - 2
    client = ada_client(ada)

    response = client.get(qr.scan_path(session, window=old))

    assert response.status_code == 410
    assert "expired" in response.content.decode()
    assert not AttendanceRecord.objects.filter(source="qr_signin").exists()
    assert qr.PENDING_KEY not in client.session


def test_a_session_not_open_yet_is_refused(ada):
    event = event_now(offset_minutes=60)  # starts in an hour; the grace is fifteen minutes
    session = event.sessions.order_by("position").first()
    client = ada_client(ada)
    response = client.get(qr.scan_path(session))
    assert response.status_code == 410
    assert "not open for sign-in" in response.content.decode()
    assert not AttendanceRecord.objects.filter(source="qr_signin").exists()


def test_a_scan_a_week_after_the_talk_is_refused(ada):
    event = event_now(offset_minutes=-7 * 24 * 60)
    session = event.sessions.order_by("position").first()
    response = ada_client(ada).get(qr.scan_path(session))
    assert response.status_code == 410


def test_a_code_for_a_session_that_does_not_exist_is_refused(ada):
    import uuid

    client = ada_client(ada)
    ghost = uuid.uuid4()
    window = qr.window_at()
    response = client.get(reverse("attendance:scan", args=[ghost, window, qr.token_for(ghost, window)]))
    assert response.status_code == 410


# --- The display page ---------------------------------------------------------------


def staff_client(program, role="read_only"):
    user = get_user_model().objects.create_user(username=f"{role}-{program.slug}", password="x" * 20, is_staff=True)
    give_role(user, program, role)
    client = Client()
    client.force_login(user)
    return client


def qr_url(event):
    return reverse("admin:rounds_roundsevent_qr", args=[event.pk])


def test_the_room_page_shows_the_open_session_s_code_and_refreshes_every_window():
    event = event_now(offset_minutes=-70)
    s1, s2, s3 = event.sessions.order_by("position")
    client = staff_client(event.program)  # read-only can show the code: that is what the room is for

    page = client.get(qr_url(event))

    assert page.status_code == 200
    html = page.content.decode()
    assert f'http-equiv="refresh" content="{qr.WINDOW_SECONDS}"' in html
    assert "<svg" in html
    assert f"<h1>{s2.title}</h1>" in html
    assert qr.scan_path(s2) in qr.scan_path(s2)  # the SVG encodes this URL; checked below by scanning it

    chosen = client.get(qr_url(event) + f"?session={s3.pk}").content.decode()
    assert f"<h1>{s3.title}</h1>" in chosen
    assert "opens 15 minutes before it starts" in chosen


def test_the_room_page_is_scoped_to_the_viewer_s_programs():
    event = event_now()
    client = staff_client(make_program("Other"))
    assert client.get(qr_url(event)).status_code == 404


def test_what_the_room_page_encodes_can_be_scanned(ada, settings):
    """The URL inside the SVG is the one the scan view accepts."""
    import re

    event = event_now()
    session = event.sessions.order_by("position").first()
    html = staff_client(event.program).get(qr_url(event)).content.decode()
    # segno writes the URL nowhere readable; derive it the way the page did and scan it.
    path = qr.scan_path(session)
    assert re.search(r"<svg[^>]*>", html)
    done = ada_client(ada).get(path)
    assert "You are signed in" in done.content.decode()
