"""
Program scope: an Emergency Medicine coordinator must not see Internal
Medicine's match queue, and a user with different roles in two programs
gets each role's powers only in that program.
"""
from decimal import Decimal

import pytest
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.urls import reverse

from attendance.models import AttendanceRecord
from attendance.tests.factories import teams_row
from core.admin import ProgramScopedAdminMixin
from core.authz import (
    ProgramScopedQuerySet,
    admin_programs,
    get_in_programs_or_404,
    staff_programs,
    writable_programs,
)
from credits.models import CreditAdjustment
from people.tests.factories import make_person
from programs.models import Program, ProgramRole
from programs.tests.factories import give_role, make_institution, make_program
from rounds.models import RoundsEvent
from rounds.tests.factories import EVENT_START, make_event

pytestmark = pytest.mark.django_db

User = get_user_model()
Role = ProgramRole.Role


@pytest.fixture
def two_programs():
    mcgill = make_institution("McGill University", "mcgill")
    em = make_program("Emergency Medicine", institution=mcgill, slug="em")
    im = make_program("Internal Medicine", institution=mcgill, slug="im")
    return em, im


@pytest.fixture
def events(two_programs):
    em, im = two_programs
    import datetime

    return (
        make_event(program=em),
        make_event(program=im, start=EVENT_START + datetime.timedelta(days=7)),
    )


def staff(username, *roles):
    """staff("name", (program, role), ...)"""
    user = User.objects.create_user(username=username, is_staff=True)
    for program, role in roles:
        give_role(user, program, role)
    return User.objects.get(pk=user.pk)


# --- Roles and groups --------------------------------------------------------------


def test_groups_follow_the_roles_a_user_holds_anywhere(two_programs):
    em, im = two_programs
    user = staff("mixed")
    assert list(user.groups.values_list("name", flat=True)) == []
    give_role(user, em, Role.COORDINATOR)
    give_role(user, im, Role.READ_ONLY)
    assert set(user.groups.values_list("name", flat=True)) == {"Coordinator", "Read only"}
    ProgramRole.objects.get(user=user, program=em).delete()
    assert set(user.groups.values_list("name", flat=True)) == {"Read only"}


def test_a_user_with_different_roles_in_two_programs_has_each_role_only_there(two_programs, events):
    """The case group sync could get wrong: both groups, but each power in one program."""
    em, im = two_programs
    em_event, im_event = events
    user = staff("mixed", (em, Role.COORDINATOR), (im, Role.READ_ONLY))

    assert set(staff_programs(user)) == {em, im}
    assert list(writable_programs(user)) == [em]
    assert list(admin_programs(user)) == []
    # Model-level, Django says yes to changing events (the Coordinator group);
    # object-level, only the EM one.
    assert user.has_perm("rounds.change_roundsevent")
    model_admin = admin.site._registry[RoundsEvent]

    class R:
        pass

    request = R()
    request.user = user
    assert model_admin.has_change_permission(request, em_event) is True
    assert model_admin.has_change_permission(request, im_event) is False
    assert set(model_admin.get_queryset(request)) == {em_event, im_event}  # sees both


def test_the_other_way_round(two_programs, events):
    em, im = two_programs
    em_event, im_event = events
    user = staff("mixed2", (em, Role.READ_ONLY), (im, Role.PROGRAM_ADMIN))
    assert list(writable_programs(user)) == [im]
    assert list(admin_programs(user)) == [im]
    model_admin = admin.site._registry[CreditAdjustment]

    class R:
        pass

    request = R()
    request.user = user
    adjustment_em = CreditAdjustment(event=em_event, person=make_person(), delta_credits=1, reason="x")
    adjustment_im = CreditAdjustment(event=im_event, person=make_person(), delta_credits=1, reason="x")
    assert model_admin._can_write(request, adjustment_im) is True
    assert model_admin._can_write(request, adjustment_em) is False


def test_superusers_see_and_write_everything(two_programs):
    boss = User.objects.create_superuser(username="boss", password="x" * 20)
    assert set(staff_programs(boss)) == set(two_programs)
    assert set(admin_programs(boss)) == set(two_programs)


def test_a_user_with_no_role_sees_nothing(two_programs):
    user = staff("nobody")
    assert list(staff_programs(user)) == []
    assert list(writable_programs(user)) == []


# --- Through the admin -------------------------------------------------------------


def test_the_other_programs_match_queue_is_invisible(client, two_programs, events):
    em, im = two_programs
    em_event, im_event = events
    em_row = teams_row(em_event, None, 0, 30, raw_display_name="EM unknown")
    im_row = teams_row(im_event, None, 0, 30, raw_display_name="IM unknown")
    client.force_login(staff("em-coord", (em, Role.COORDINATOR)))

    page = client.get(reverse("admin:attendance_attendancerecord_changelist") + "?matched=no")
    assert list(page.context["cl"].result_list) == [em_row]
    # The admin answers an id outside the staff member's programs the way it
    # answers a deleted one: a redirect to the index saying it does not exist.
    for name, pk in (
        ("admin:attendance_attendancerecord_change", im_row.pk),
        ("admin:rounds_roundsevent_change", im_event.pk),
    ):
        response = client.get(reverse(name, args=[pk]), follow=True)
        assert "IM unknown" not in response.content.decode()
        assert [str(m) for m in response.context["messages"]] and "exist" in str(
            list(response.context["messages"])[0]
        )
    assert client.get(reverse("admin:rounds_roundsevent_change", args=[em_event.pk])).status_code == 200


def test_read_only_in_one_program_cannot_write_there_even_as_coordinator_elsewhere(
    client, two_programs, events
):
    em, im = two_programs
    em_event, im_event = events
    client.force_login(staff("mixed", (em, Role.COORDINATOR), (im, Role.READ_ONLY)))

    def post_title(event, title):
        return client.post(
            reverse("admin:rounds_roundsevent_change", args=[event.pk]),
            {
                "program": event.program.pk, "title": title, "date": event.date.isoformat(),
                "status": "draft", "accredited_credits": "1.00",
                "start_at_0": "2026-09-15", "start_at_1": "12:00:00",
                "end_at_0": "2026-09-15", "end_at_1": "13:00:00",
                "teams_join_url": "", "teams_meeting_title": "",
                "sessions-TOTAL_FORMS": 0, "sessions-INITIAL_FORMS": 0,
            },
        )

    assert post_title(em_event, "Renamed EM").status_code == 302
    im_response = post_title(im_event, "Renamed IM")
    assert im_response.status_code in (403, 200)  # refused, or redisplayed read-only
    em_event.refresh_from_db()
    im_event.refresh_from_db()
    assert em_event.title == "Renamed EM"
    assert im_event.title != "Renamed IM"


def test_a_form_cannot_point_a_new_row_at_another_program(client, two_programs, events):
    em, im = two_programs
    em_event, im_event = events
    client.force_login(staff("em-admin", (em, Role.PROGRAM_ADMIN)))
    person = make_person()
    response = client.post(
        reverse("admin:credits_creditadjustment_add"),
        {"person": person.pk, "event": im_event.pk, "kind": "attendance",
         "delta_credits": "1.00", "reason": "Trying the other program"},
    )
    assert response.status_code == 200  # redisplayed: that event is not a choice
    assert "event" in response.context["adminform"].form.errors
    assert CreditAdjustment.objects.count() == 0
    ok = client.post(
        reverse("admin:credits_creditadjustment_add"),
        {"person": person.pk, "event": em_event.pk, "kind": "attendance",
         "delta_credits": "1.00", "reason": "Own program"},
    )
    assert ok.status_code == 302
    assert CreditAdjustment.objects.get().event == em_event


def test_the_event_picker_offers_only_writable_programs(client, two_programs, events):
    em, im = two_programs
    client.force_login(staff("mixed", (em, Role.COORDINATOR), (im, Role.READ_ONLY)))
    page = client.get(reverse("admin:rounds_roundsevent_add")).content.decode()
    assert "Emergency Medicine" in page
    assert "Internal Medicine" not in page


def test_a_program_admin_edits_their_own_program_only(client, two_programs):
    em, im = two_programs
    client.force_login(staff("em-admin", (em, Role.PROGRAM_ADMIN), (im, Role.COORDINATOR)))
    assert client.get(reverse("admin:programs_program_change", args=[em.pk])).status_code == 200
    page = client.get(reverse("admin:programs_program_change", args=[im.pk]))
    assert page.status_code == 200  # visible, read-only
    response = client.post(
        reverse("admin:programs_program_change", args=[em.pk]),
        {
            "name": "Emergency Medicine", "series_name": "EM Rounds", "is_active": "on",
            "attendance_rate_per_hour": "1.00", "teaching_rate_per_hour": "1.50",
            "default_accredited_credits": "3.00", "attendance_disagreement_minutes": "3",
            "accreditation_year_end_month": "12", "accreditation_year_end_day": "31",
            "coi_question_version": "2026-10", "retention_years": "7", "activity_evaluation_cadence": "per_event",
            "roles-TOTAL_FORMS": 0, "roles-INITIAL_FORMS": 0,
        },
    )
    assert response.status_code == 302, response.context["adminform"].form.errors
    em.refresh_from_db()
    assert (em.teaching_rate_per_hour, em.attendance_disagreement_minutes) == (Decimal("1.50"), 3)
    assert em.slug == "em"  # identity is not theirs to change
    refused = client.post(
        reverse("admin:programs_program_change", args=[im.pk]),
        {"name": "Hijacked", "series_name": "x", "attendance_rate_per_hour": "9",
         "teaching_rate_per_hour": "9", "default_accredited_credits": "3.00",
         "attendance_disagreement_minutes": "5", "accreditation_year_end_month": "12",
         "accreditation_year_end_day": "31", "coi_question_version": "2026-10",
         "retention_years": "7", "activity_evaluation_cadence": "per_event", "roles-TOTAL_FORMS": 0, "roles-INITIAL_FORMS": 0},
    )
    assert refused.status_code == 403
    im.refresh_from_db()
    assert im.name == "Internal Medicine"


def test_a_program_admin_grants_coordinators_but_not_program_admins(client, two_programs):
    em, im = two_programs
    client.force_login(staff("em-admin", (em, Role.PROGRAM_ADMIN)))
    newcomer = User.objects.create_user(username="newcomer", is_staff=True)
    ok = client.post(
        reverse("admin:programs_programrole_add"),
        {"user": newcomer.pk, "program": em.pk, "role": "coordinator"},
    )
    assert ok.status_code == 302, ok.context["adminform"].form.errors
    assert ProgramRole.objects.get(user=newcomer).role == "coordinator"
    assert newcomer.groups.filter(name="Coordinator").exists()
    elsewhere = client.post(
        reverse("admin:programs_programrole_add"),
        {"user": newcomer.pk, "program": im.pk, "role": "coordinator"},
    )
    assert elsewhere.status_code == 200 and "program" in elsewhere.context["adminform"].form.errors
    promote = client.post(
        reverse("admin:programs_programrole_change", args=[ProgramRole.objects.get(user=newcomer).pk]),
        {"user": newcomer.pk, "program": em.pk, "role": "program_admin"},
    )
    assert promote.status_code == 200 and "role" in promote.context["adminform"].form.errors


# --- The registry walk ----------------------------------------------------------


INSTANCE_WIDE = {
    "accounts.user",
    "auth.group",
    "people.person",
    "people.alloweddomain",
    "people.signinrequest",
    "rounds.coideclaration",
    "audit.auditlog",
    "programs.institution",
    "programs.program",
    "programs.programrole",
}


def test_every_registered_model_is_either_program_scoped_or_deliberately_instance_wide():
    """
    If this fails you registered a model in the admin. Either its manager
    gets for_programs() and its admin the mixin, or it is added to
    INSTANCE_WIDE here on purpose.
    """
    for model, model_admin in admin.site._registry.items():
        label = model._meta.label_lower
        scoped_manager = isinstance(model._default_manager.all(), ProgramScopedQuerySet)
        scoped_admin = isinstance(model_admin, ProgramScopedAdminMixin)
        if label in INSTANCE_WIDE:
            assert not scoped_admin, f"{label} is listed instance-wide but has the mixin"
            continue
        assert scoped_manager, f"{label}: manager has no for_programs()"
        assert scoped_admin, f"{label}: admin lacks ProgramScopedAdminMixin"


def test_get_in_programs_or_404(two_programs, events):
    from django.http import Http404

    em, im = two_programs
    em_event, im_event = events
    user = staff("em-only", (em, Role.READ_ONLY))
    assert get_in_programs_or_404(RoundsEvent, em_event.pk, user) == em_event
    with pytest.raises(Http404):
        get_in_programs_or_404(RoundsEvent, im_event.pk, user)


def test_program_rules_and_defaults():
    import datetime

    program = make_program("Rules", accreditation_year_end_month=6, accreditation_year_end_day=30)
    assert program.retention_years == 7
    assert program.attendance_disagreement_minutes == 5
    assert program.accreditation_year_end(datetime.date(2026, 10, 7)) == datetime.date(2027, 6, 30)
    assert program.accreditation_period(datetime.date(2026, 10, 7)) == (
        datetime.date(2026, 7, 1),
        datetime.date(2027, 6, 30),
    )
