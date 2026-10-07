"""
Staff roles: what each admin group may do.

These are about staff accounts and have nothing to do with Person.role,
which only decides which certificate template an attendee gets.

This file is the source of truth. The groups themselves are created by a
migration; their permissions are re-applied from here after every
`migrate`, so a model added later is covered without a new migration.
Editing a group's permissions by hand in the admin does not stick.

Credit adjustments and certificate issue/revocation are the fraud surface.
They are listed explicitly under Program admin and nowhere else.
"""
from django.contrib.auth.models import Group, Permission

COORDINATOR = "Coordinator"
PROGRAM_ADMIN = "Program admin"
READ_ONLY = "Read only"

ROLE_NAMES = [COORDINATOR, PROGRAM_ADMIN, READ_ONLY]

# Staff can see every table of these apps. Staff accounts themselves
# (accounts, auth) are managed by superusers only.
VIEWABLE_APPS = ["people", "rounds", "attendance", "credits", "certificates", "audit"]

# Enter sessions, upload attendance, work the match queue, chase presenters.
COORDINATOR_PERMISSIONS = [
    "people.add_person",
    "people.change_person",
    "people.add_personemail",
    "people.change_personemail",
    "people.delete_personemail",
    "rounds.add_roundsevent",
    "rounds.change_roundsevent",
    "rounds.add_session",
    "rounds.change_session",
    "rounds.delete_session",
    "rounds.add_sessionpresenter",
    "rounds.change_sessionpresenter",
    "rounds.delete_sessionpresenter",
    "rounds.add_learningobjective",
    "rounds.change_learningobjective",
    "rounds.delete_learningobjective",
    "rounds.add_coideclaration",
    "attendance.add_attendanceupload",
    "attendance.add_attendancerecord",
    "attendance.change_attendancerecord",
    "attendance.add_attendancesupersession",
]

# Everything a coordinator does, plus the decisions that create or change credit.
PROGRAM_ADMIN_ONLY_PERMISSIONS = [
    "credits.add_creditadjustment",
    "credits.add_evaluationwindow",
    "certificates.issue_certificate",
    "certificates.revoke_certificate",
    "people.merge_person",
    "people.add_alloweddomain",
    "people.change_alloweddomain",
    "people.delete_alloweddomain",
    "people.change_signinrequest",
    "credits.add_evaluationsubmission",
    "credits.change_evaluationsubmission",
    "credits.delete_evaluationsubmission",
    "credits.add_evaluationresponse",
    "credits.change_evaluationresponse",
    "credits.delete_evaluationresponse",
]


def role_permissions():
    """{role name: set of "app_label.codename"} as currently defined."""
    view = {
        f"{app_label}.{codename}"
        for app_label, codename in Permission.objects.filter(
            content_type__app_label__in=VIEWABLE_APPS, codename__startswith="view_"
        ).values_list("content_type__app_label", "codename")
    }
    coordinator = view | set(COORDINATOR_PERMISSIONS)
    return {
        READ_ONLY: view,
        COORDINATOR: coordinator,
        PROGRAM_ADMIN: coordinator | set(PROGRAM_ADMIN_ONLY_PERMISSIONS),
    }


def sync_role_permissions(strict=False):
    """
    Make each group's permissions exactly what this file says.

    During `migrate` this runs after each app, before later apps'
    permissions exist, so unknown names are skipped unless strict is set.
    The test suite calls it strictly to catch a misspelt permission.
    """
    known = {
        f"{p.content_type.app_label}.{p.codename}": p
        for p in Permission.objects.select_related("content_type")
    }
    for name, wanted in role_permissions().items():
        missing = sorted(wanted - known.keys())
        if missing and strict:
            raise LookupError(f"{name}: unknown permissions {missing}")
        group, _ = Group.objects.get_or_create(name=name)
        group.permissions.set([known[label] for label in wanted if label in known])
