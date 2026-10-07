"""
Magic-link sign-in: the link is the credential, so it is treated like a
password hash. Email goes to the console backend; tests read mail.outbox.
"""
import datetime
import re

import pytest
from django.core import mail
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from attendance.tests.factories import teams_row
from audit.models import AuditLog
from core.authz import unmarked_routes
from credits.tests.factories import evaluate
from people.models import AllowedDomain, Person, PersonEmail, SignInRequest
from people.tests.factories import make_person
from programs.tests.factories import make_program
from rounds.tests.factories import EVENT_START, make_event
from signin import services
from signin.models import MagicLinkToken, hash_token

pytestmark = pytest.mark.django_db

LINK = re.compile(r"https?://\S+/signin/([A-Za-z0-9_-]+)/")


def ask_for_link(client, email, **extra):
    return client.post(reverse("signin:start"), {"email": email}, **extra)


def last_link_token():
    return LINK.search(mail.outbox[-1].body).group(1)


@pytest.fixture
def ada():
    AllowedDomain.objects.create(domain="mcgill.ca")
    return make_person(given="Ada", family="Lovelace", email="ada@mcgill.ca")


# --- The link ----------------------------------------------------------------


def test_asking_for_a_link_sends_one_and_stores_only_its_hash(client, ada):
    response = ask_for_link(client, "Ada@McGill.ca")
    assert response.status_code == 302 and response.url == reverse("signin:sent")
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == ["ada@mcgill.ca"]
    token = last_link_token()
    row = MagicLinkToken.objects.get()
    assert row.email == "ada@mcgill.ca"
    assert row.token_hash == hash_token(token)
    assert token not in row.token_hash and token not in str(row.pk)
    assert abs((row.expires_at - row.created_at) - datetime.timedelta(minutes=15)) < datetime.timedelta(seconds=2)
    assert AuditLog.objects.filter(action="signin.link_sent").exists()


def test_the_link_signs_the_known_person_in(client, ada):
    ask_for_link(client, "ada@mcgill.ca")
    response = client.get(reverse("signin:redeem", args=[last_link_token()]))
    assert response.status_code == 302 and response.url == reverse("signin:me")
    page = client.get(reverse("signin:me"))
    assert page.status_code == 200
    assert page.context["person"] == ada
    assert "Ada Lovelace" in page.content.decode()
    assert PersonEmail.objects.get(email="ada@mcgill.ca").verified_at is not None
    assert AuditLog.objects.filter(action="attendee.signed_in", actor_person=ada).exists()


def test_the_link_is_single_use(client, ada):
    ask_for_link(client, "ada@mcgill.ca")
    token = last_link_token()
    assert client.get(reverse("signin:redeem", args=[token])).status_code == 302
    other = Client()
    response = other.get(reverse("signin:redeem", args=[token]))
    assert response.status_code == 410
    assert other.get(reverse("signin:me")).status_code == 302  # not signed in


def test_an_expired_link_is_refused(client, ada):
    ask_for_link(client, "ada@mcgill.ca")
    MagicLinkToken.objects.update(expires_at=timezone.now() - datetime.timedelta(seconds=1))
    assert client.get(reverse("signin:redeem", args=[last_link_token()])).status_code == 410


def test_a_made_up_token_is_refused_the_same_way(client, ada):
    assert client.get(reverse("signin:redeem", args=["not-a-real-token"])).status_code == 410


def test_the_link_is_bound_to_its_address(client, ada):
    """A link sent to one address signs in the person who holds that address, nobody else."""
    bob = make_person(given="Bob", family="Byrne", email="bob@mcgill.ca")
    ask_for_link(client, "bob@mcgill.ca")
    client.get(reverse("signin:redeem", args=[last_link_token()]))
    assert client.get(reverse("signin:me")).context["person"] == bob


def test_a_merged_address_signs_in_the_survivor(client, ada):
    survivor = make_person(given="Ada", family="King")
    Person.objects.filter(pk=ada.pk).update(merged_into=survivor)
    ask_for_link(client, "ada@mcgill.ca")
    client.get(reverse("signin:redeem", args=[last_link_token()]))
    assert client.get(reverse("signin:me")).context["person"] == survivor


# --- Unknown addresses ---------------------------------------------------------


def test_an_unknown_address_on_an_allowed_domain_registers_after_the_link(client):
    AllowedDomain.objects.create(domain="mcgill.ca")
    ask_for_link(client, "new.person@mcgill.ca")
    assert Person.objects.count() == 0
    response = client.get(reverse("signin:redeem", args=[last_link_token()]))
    assert response.url == reverse("signin:complete")
    response = client.post(
        reverse("signin:complete"),
        {"given_name": "New", "family_name": "Person", "role": "nurse"},
    )
    assert response.url == reverse("signin:me")
    person = Person.objects.get()
    assert (person.given_name, person.role) == ("New", "nurse")
    link = PersonEmail.objects.get(email="new.person@mcgill.ca")
    assert (link.person, link.is_primary) == (person, True) and link.verified_at is not None
    assert client.get(reverse("signin:me")).context["person"] == person
    assert AuditLog.objects.filter(action="person.self_registered", actor_person=person).exists()


def test_the_profile_step_needs_a_verified_address(client):
    response = client.get(reverse("signin:complete"))
    assert response.url == reverse("signin:start")


def test_an_address_outside_the_allowed_domains_goes_to_the_review_queue(client):
    AllowedDomain.objects.create(domain="mcgill.ca")
    ask_for_link(client, "someone@elsewhere.example")
    assert len(mail.outbox) == 1  # the link is still sent: the address is verified by using it
    response = client.get(reverse("signin:redeem", args=[last_link_token()]))
    assert response.status_code == 200
    assert "coordinator will take a look" in response.content.decode()
    queued = SignInRequest.objects.get()
    assert (queued.email, queued.status) == ("someone@elsewhere.example", "pending")
    assert Person.objects.count() == 0
    assert client.get(reverse("signin:me")).status_code == 302


def test_a_domain_marked_for_review_queues_too(client):
    AllowedDomain.objects.create(domain="example.com", auto_admit=False)
    ask_for_link(client, "x@example.com")
    client.get(reverse("signin:redeem", args=[last_link_token()]))
    assert SignInRequest.objects.filter(email="x@example.com").exists()


def test_known_and_unknown_addresses_look_the_same_when_asking(client, ada):
    known = ask_for_link(client, "ada@mcgill.ca")
    unknown = ask_for_link(client, "stranger@mcgill.ca")
    assert (known.status_code, known.url) == (unknown.status_code, unknown.url)


# --- Rate limits ---------------------------------------------------------------


def test_five_links_per_address_per_hour(client, ada):
    for _ in range(5):
        assert ask_for_link(client, "ada@mcgill.ca").status_code == 302
    response = ask_for_link(client, "ada@mcgill.ca")
    assert response.status_code == 200
    assert "Too many" in response.content.decode()
    assert len(mail.outbox) == 5


def test_twenty_links_per_network_address_per_hour(client, ada):
    for n in range(20):
        assert ask_for_link(client, f"p{n}@mcgill.ca", REMOTE_ADDR="203.0.113.5").status_code == 302
    assert ask_for_link(client, "p99@mcgill.ca", REMOTE_ADDR="203.0.113.5").status_code == 200
    assert ask_for_link(client, "p99@mcgill.ca", REMOTE_ADDR="203.0.113.6").status_code == 302


def test_old_links_do_not_count_against_the_limit(client, ada):
    for _ in range(5):
        ask_for_link(client, "ada@mcgill.ca")
    MagicLinkToken.objects.update(created_at=timezone.now() - datetime.timedelta(hours=2))
    assert ask_for_link(client, "ada@mcgill.ca").status_code == 302


# --- Sessions ----------------------------------------------------------------


def signed_in_client(person_email):
    client = Client()
    ask_for_link(client, person_email)
    client.get(reverse("signin:redeem", args=[last_link_token()]))
    return client


def test_signing_in_rotates_the_session_key_and_lasts_ninety_days(client, ada):
    client.get(reverse("signin:start"))
    before = client.session.session_key
    ask_for_link(client, "ada@mcgill.ca")
    client.get(reverse("signin:redeem", args=[last_link_token()]))
    assert client.session.session_key != before
    assert client.session.get_expiry_age() == 90 * 24 * 3600


def test_sign_out_here_leaves_other_devices_signed_in(ada):
    laptop, phone = signed_in_client("ada@mcgill.ca"), signed_in_client("ada@mcgill.ca")
    laptop.post(reverse("signin:signout"))
    assert laptop.get(reverse("signin:me")).status_code == 302
    assert phone.get(reverse("signin:me")).status_code == 200


def test_sign_out_everywhere_ends_every_session(ada):
    laptop, phone = signed_in_client("ada@mcgill.ca"), signed_in_client("ada@mcgill.ca")
    laptop.post(reverse("signin:signout_everywhere"))
    assert laptop.get(reverse("signin:me")).status_code == 302
    assert phone.get(reverse("signin:me")).status_code == 302
    assert AuditLog.objects.filter(action="attendee.signed_out_everywhere").exists()
    # A fresh link works again.
    again = signed_in_client("ada@mcgill.ca")
    assert again.get(reverse("signin:me")).status_code == 200


def test_the_credits_page_needs_a_signed_in_person(client):
    response = client.get(reverse("signin:me"))
    assert response.status_code == 302 and response.url.startswith(reverse("signin:start"))


def test_staff_admin_login_is_not_an_attendee_session(client, ada):
    from django.contrib.auth import get_user_model

    boss = get_user_model().objects.create_superuser(username="boss", password="x" * 20)
    client.force_login(boss)
    assert client.get(reverse("signin:me")).status_code == 302  # staff are not attendees


# --- The credits page ----------------------------------------------------------


def test_the_credits_page_shows_a_total_per_program_with_rates_and_pending(ada):
    em = make_program("Emergency Medicine", attendance_rate_per_hour="1.00")
    im = make_program("Internal Medicine", attendance_rate_per_hour="1.50")
    em_event = make_event(program=em)
    im_event = make_event(
        program=im, credits="3.00", start=EVENT_START + datetime.timedelta(days=7)
    )  # a ceiling above 1.50, so the rate shows
    teams_row(em_event, ada, 0, 60)
    evaluate(ada, em_event.sessions.get())
    teams_row(im_event, ada, 7 * 24 * 60, 7 * 24 * 60 + 60)
    evaluate(ada, im_event.sessions.get())

    page = signed_in_client("ada@mcgill.ca").get(reverse("signin:me"))
    programs = page.context["programs"]
    assert [p.program for p in programs] == [em, im]
    assert [str(p.earned_attendance) for p in programs] == ["1.00", "1.50"]
    text = page.content.decode()
    assert "Total for Emergency Medicine" in text and "Total for Internal Medicine" in text
    assert "1.50/h" in text  # the rate per event, so a mid-year change is visible
    assert "pending" in text  # nothing certified yet


def test_the_credits_page_lists_talks_to_evaluate_and_can_ask_for_another_week(ada, monkeypatch):
    event = make_event()  # 2026-09-15
    teams_row(event, ada, 0, 60)
    client = signed_in_client("ada@mcgill.ca")
    monkeypatch.setattr(
        "django.utils.timezone.now",
        lambda: datetime.datetime(2026, 10, 1, 12, 0, tzinfo=datetime.timezone.utc),
    )
    page = client.get(reverse("signin:me"))
    [(shown_event, session, state)] = page.context["to_evaluate"]
    assert (shown_event, state) == (event, "can_request")
    assert "Request another week" in page.content.decode()
    response = client.post(reverse("signin:reopen"), {"session": session.pk}, follow=True)
    assert "open again" in response.content.decode()
    assert response.context["to_evaluate"][0][2] == "reopened"
    entry = AuditLog.objects.get(action="evaluation.window_granted")
    assert entry.actor_person == ada


def test_every_signin_route_declares_its_authorization():
    assert unmarked_routes() == []
