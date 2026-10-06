import decimal
import uuid

import pytest
from django.db import IntegrityError, transaction
from django.test import RequestFactory

from audit.log import record
from audit.models import AuditLog
from core.models import ImmutableRowError
from people.tests.factories import make_person, make_staff

pytestmark = pytest.mark.django_db


def test_a_staff_entry_snapshots_who_it_was():
    user = make_staff("coordinator")
    user.email = "coord@mcgill.ca"
    user.save()
    target = make_person()
    entry = record("person.merged", target, user=user, metadata={"moved": ["a@mcgill.ca"]})
    assert entry.actor_type == "staff"
    assert entry.actor_label == "coordinator <coord@mcgill.ca>"
    assert (entry.object_type, entry.object_id) == ("people.person", str(target.pk))
    user.username = "renamed"
    user.save()
    entry.refresh_from_db()
    assert entry.actor_label == "coordinator <coord@mcgill.ca>"


def test_an_attendee_entry():
    person = make_person(given="Ada", family="Lovelace", email="ada@mcgill.ca")
    entry = record("coi.declared", person, person=person)
    assert (entry.actor_type, entry.actor_person) == ("attendee", person)
    assert entry.actor_label == "Ada Lovelace <ada@mcgill.ca>"


def test_a_system_entry_says_which_job():
    entry = record("reminder.sent", system="reminder-job")
    assert entry.actor_type == "system"
    assert entry.actor_label == "system:reminder-job"
    assert entry.actor_user is None and entry.actor_person is None


def test_exactly_one_actor_is_required():
    with pytest.raises(ValueError):
        record("x")
    with pytest.raises(ValueError):
        record("x", user=make_staff(), system="job")


def test_the_request_supplies_the_user_and_ip():
    request = RequestFactory().get("/", REMOTE_ADDR="203.0.113.9")
    request.user = make_staff()
    entry = record("attendance.matched", request=request)
    assert entry.actor_user == request.user
    assert entry.ip == "203.0.113.9"


def test_metadata_takes_ids_decimals_and_dates():
    entry = record(
        "credit.adjusted",
        system="test",
        metadata={"id": uuid.uuid4(), "delta": decimal.Decimal("0.25")},
    )
    entry.refresh_from_db()
    assert entry.metadata["delta"] == "0.25"


def test_the_database_refuses_an_actor_that_contradicts_its_type():
    user = make_staff()
    bad_rows = [
        dict(actor_type="system", actor_user=user, actor_label="x"),
        dict(actor_type="staff", actor_label="x"),
        dict(actor_type="attendee", actor_user=user, actor_label="x"),
        dict(actor_type="system", actor_label=""),
    ]
    for row in bad_rows:
        with pytest.raises(IntegrityError), transaction.atomic():
            AuditLog.objects.create(action="x", **row)


def test_entries_cannot_be_edited_or_deleted():
    entry = record("x", system="test")
    entry.action = "y"
    with pytest.raises(ImmutableRowError):
        entry.save()
    with pytest.raises(ImmutableRowError):
        entry.delete()


def test_signing_in_to_the_admin_is_logged(client):
    user = make_staff("admin1")
    user.set_password("a long test password")
    user.save()
    assert client.login(username="admin1", password="a long test password")
    entry = AuditLog.objects.get(action="admin.signed_in")
    assert entry.actor_user == user
