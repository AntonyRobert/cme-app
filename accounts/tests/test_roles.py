"""
Staff roles. The point of these tests: a hired coordinator must not be
able to adjust credit, issue a certificate or merge records.
"""
import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.urls import reverse

from accounts.roles import (
    COORDINATOR,
    PROGRAM_ADMIN,
    READ_ONLY,
    role_permissions,
    sync_role_permissions,
)
from people.tests.factories import make_person
from rounds.tests.factories import make_event

pytestmark = pytest.mark.django_db

FRAUD_SURFACE = {
    "credits.add_creditadjustment",
    "credits.add_evaluationwindow",
    "certificates.issue_certificate",
    "certificates.revoke_certificate",
}


def staff_in(role, client=None):
    user = get_user_model().objects.create_user(username=role.lower().replace(" ", "-"), is_staff=True)
    user.groups.add(Group.objects.get(name=role))
    user = get_user_model().objects.get(pk=user.pk)  # drop the permission cache
    if client is not None:
        client.force_login(user)
    return user


def test_the_three_groups_exist_with_their_permissions():
    sync_role_permissions(strict=True)  # raises on a misspelt permission
    for name, wanted in role_permissions().items():
        group = Group.objects.get(name=name)
        actual = {f"{p.content_type.app_label}.{p.codename}" for p in group.permissions.all()}
        assert actual == wanted
        assert wanted, name


def test_only_the_program_admin_holds_the_fraud_surface():
    roles = role_permissions()
    assert FRAUD_SURFACE <= roles[PROGRAM_ADMIN]
    assert not FRAUD_SURFACE & roles[COORDINATOR]
    assert not FRAUD_SURFACE & roles[READ_ONLY]
    assert "people.merge_person" not in roles[COORDINATOR]


def test_read_only_can_view_everything_and_write_nothing():
    perms = role_permissions()[READ_ONLY]
    assert perms and all(p.split(".")[1].startswith("view_") for p in perms)
    for model in ("person", "attendancerecord", "certificate", "auditlog", "creditadjustment"):
        assert any(p.endswith(f".view_{model}") for p in perms)


def test_nobody_but_a_superuser_manages_staff_accounts():
    for perms in role_permissions().values():
        assert not any(p.startswith(("accounts.", "auth.")) for p in perms)


def test_no_role_can_change_or_delete_frozen_records():
    everything = set().union(*role_permissions().values())
    for forbidden in (
        "audit.change_auditlog",
        "audit.delete_auditlog",
        "audit.add_auditlog",
        "certificates.change_certificate",
        "certificates.delete_certificate",
        "credits.change_creditadjustment",
        "credits.delete_creditadjustment",
        "attendance.delete_attendancerecord",
        "attendance.change_attendanceupload",
        "rounds.change_coideclaration",
        "people.delete_person",
    ):
        assert forbidden not in everything


# --- Through the admin itself ------------------------------------------------


def test_coordinator_cannot_reach_credit_adjustments(client):
    staff_in(COORDINATOR, client)
    assert client.get(reverse("admin:credits_creditadjustment_add")).status_code == 403
    response = client.post(
        reverse("admin:credits_creditadjustment_add"),
        {"person": make_person().pk, "event": make_event().pk, "delta_credits": "1.00", "reason": "x"},
    )
    assert response.status_code == 403
    assert client.get(reverse("admin:credits_creditadjustment_changelist")).status_code == 200


def test_coordinator_can_do_the_coordinator_work(client):
    staff_in(COORDINATOR, client)
    for name in (
        "admin:rounds_roundsevent_add",
        "admin:rounds_session_add",
        "admin:attendance_attendanceupload_add",
        "admin:attendance_attendancerecord_add",
        "admin:people_person_add",
        "admin:rounds_coideclaration_add",
    ):
        assert client.get(reverse(name)).status_code == 200, name


def test_coordinator_is_not_offered_the_merge(client):
    staff_in(COORDINATOR, client)
    first, second = make_person(), make_person()
    page = client.get(reverse("admin:people_person_changelist"))
    assert b"merge_selected" not in page.content
    client.post(
        reverse("admin:people_person_changelist"),
        {"action": "merge_selected", "_selected_action": [first.pk, second.pk], "confirm": "1",
         "survivor": first.pk},
    )
    second.refresh_from_db()
    assert second.merged_into is None


def test_program_admin_can_adjust_credit_and_merge(client):
    staff_in(PROGRAM_ADMIN, client)
    make_person()
    assert client.get(reverse("admin:credits_creditadjustment_add")).status_code == 200
    assert b"merge_selected" in client.get(reverse("admin:people_person_changelist")).content


def test_read_only_sees_pages_but_cannot_add_or_post(client):
    staff_in(READ_ONLY, client)
    person = make_person()
    assert client.get(reverse("admin:people_person_changelist")).status_code == 200
    assert client.get(reverse("admin:people_person_change", args=[person.pk])).status_code == 200
    assert client.get(reverse("admin:audit_auditlog_changelist")).status_code == 200
    assert client.get(reverse("admin:people_person_add")).status_code == 403
    response = client.post(
        reverse("admin:people_person_change", args=[person.pk]),
        {"given_name": "Changed", "family_name": "Name", "role": "nurse"},
    )
    assert response.status_code == 403
    person.refresh_from_db()
    assert person.given_name != "Changed"
    assert client.get(reverse("admin:accounts_user_changelist")).status_code == 403
