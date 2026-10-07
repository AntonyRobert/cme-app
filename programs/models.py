"""
Institution and Program: the two levels above everything else.

An institution is a deployment (McGill is one instance, Concordia another;
their records never share a database). A program is a row inside it:
Emergency Medicine, Internal Medicine, General Surgery. Every event belongs
to a program, every certificate is issued by one, and staff are scoped to
one or more of them through ProgramRole.
"""
import datetime
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q

from core.constraints import is_quarter_multiple, validate_quarter_multiple
from core.models import UUIDModel


def default_series_name():
    return settings.SERIES_NAME


def default_coi_version():
    return settings.COI_CURRENT_VERSION


def default_year_end_month():
    return settings.ACCREDITATION_YEAR_END[0]


def default_year_end_day():
    return settings.ACCREDITATION_YEAR_END[1]


class Institution(UUIDModel):
    """One row per instance in practice. A table, so the name is data."""

    name = models.CharField(max_length=200)
    short_name = models.SlugField(
        max_length=50, unique=True, help_text="The <org> of the hostname and the deploy script."
    )
    website = models.URLField(max_length=300, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class ProgramQuerySet(models.QuerySet):
    def for_programs(self, programs):
        return self.filter(pk__in=[p.pk for p in programs])


class Program(UUIDModel):
    """
    A program of an institution, with everything that differs between
    programs: rates, defaults, the accreditation year, the questionnaire,
    the series name, retention. Editable by that program's admins only.
    """

    institution = models.ForeignKey(Institution, on_delete=models.PROTECT, related_name="programs")
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=50, help_text="For URLs. Unique within the institution.")
    series_name = models.CharField(
        max_length=200,
        default=default_series_name,
        help_text="The default title of a new event. Prints on certificate lines.",
    )
    attendance_rate_per_hour = models.DecimalField(
        max_digits=4,
        decimal_places=2,
        default=Decimal("1.00"),
        validators=[MinValueValidator(Decimal("0"))],
        help_text="Credits per hour attended. Snapshotted onto each certificate line at issue.",
    )
    teaching_rate_per_hour = models.DecimalField(
        max_digits=4,
        decimal_places=2,
        default=Decimal("1.00"),
        validators=[MinValueValidator(Decimal("0"))],
        help_text="Credits per hour presented. Snapshotted onto each certificate line at issue.",
    )
    default_accredited_credits = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("3.00"),
        validators=[MinValueValidator(Decimal("0")), validate_quarter_multiple],
        help_text="Pre-filled on a new event. The accreditor-set ceiling, in steps of 0.25.",
    )
    accreditation_year_end_month = models.PositiveSmallIntegerField(default=default_year_end_month)
    accreditation_year_end_day = models.PositiveSmallIntegerField(default=default_year_end_day)
    coi_question_version = models.CharField(
        max_length=50,
        default=default_coi_version,
        help_text="Which COI questionnaire version new declarations for this program use.",
    )
    attendance_disagreement_minutes = models.PositiveSmallIntegerField(
        default=5,
        help_text="Attendance sources further apart than this, for one person and session, "
        "are held back from bulk sign-off for a human to look at.",
    )
    default_evaluation_form = models.ForeignKey(
        "credits.EvaluationForm",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        help_text="The evaluation form a session uses unless its event or the session itself says otherwise.",
    )
    retention_years = models.PositiveSmallIntegerField(
        default=7,
        help_text="How many years records are kept after the accreditation year. The value "
        "only: what happens when it expires is still an open decision.",
    )
    is_active = models.BooleanField(
        default=True, help_text="A retired program keeps its history and takes no new events."
    )

    objects = ProgramQuerySet.as_manager()

    class Meta:
        ordering = ["institution", "name"]
        constraints = [
            models.UniqueConstraint(fields=["institution", "slug"], name="program_unique_slug"),
            models.CheckConstraint(
                condition=Q(attendance_rate_per_hour__gte=0, teaching_rate_per_hour__gte=0),
                name="program_rates_not_negative",
            ),
            models.CheckConstraint(
                condition=is_quarter_multiple("default_accredited_credits"),
                name="program_default_credits_quarter_multiple",
            ),
            models.CheckConstraint(
                condition=Q(accreditation_year_end_month__gte=1, accreditation_year_end_month__lte=12),
                name="program_year_end_month_valid",
            ),
            models.CheckConstraint(
                condition=Q(accreditation_year_end_day__gte=1, accreditation_year_end_day__lte=31),
                name="program_year_end_day_valid",
            ),
        ]

    def __str__(self):
        return self.name

    def accreditation_year_end(self, day):
        """The last day of this program's accreditation year that contains `day`."""
        month, day_of_month = self.accreditation_year_end_month, self.accreditation_year_end_day
        end = datetime.date(day.year, month, day_of_month)
        return end if end >= day else datetime.date(day.year + 1, month, day_of_month)

    def accreditation_period(self, day):
        """(start, end) of the accreditation year containing `day`."""
        end = self.accreditation_year_end(day)
        previous_end = datetime.date(end.year - 1, end.month, end.day)
        return previous_end + datetime.timedelta(days=1), end

    def clean(self):
        super().clean()
        errors = {}
        try:
            datetime.date(2001, self.accreditation_year_end_month, self.accreditation_year_end_day)
        except (TypeError, ValueError):
            errors["accreditation_year_end_day"] = "Not a date in any year."
        if self.coi_question_version not in settings.COI_QUESTIONS:
            errors["coi_question_version"] = (
                f"Unknown questionnaire version. Known: {', '.join(settings.COI_QUESTIONS)}."
            )
        if errors:
            raise ValidationError(errors)


class ProgramRole(UUIDModel):
    """
    One staff member's role in one program. The role names match the three
    Django groups, which stay the permission templates; which rows those
    permissions apply to is decided by the programs a user holds roles in.
    """

    class Role(models.TextChoices):
        COORDINATOR = "coordinator", "Coordinator"
        PROGRAM_ADMIN = "program_admin", "Program admin"
        READ_ONLY = "read_only", "Read only"

    GROUP_NAMES = {
        Role.COORDINATOR: "Coordinator",
        Role.PROGRAM_ADMIN: "Program admin",
        Role.READ_ONLY: "Read only",
    }
    WRITE_ROLES = {Role.COORDINATOR, Role.PROGRAM_ADMIN}

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="program_roles"
    )
    program = models.ForeignKey(Program, on_delete=models.PROTECT, related_name="roles")
    role = models.CharField(max_length=20, choices=Role.choices)
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    granted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["program", "user"]
        constraints = [
            models.UniqueConstraint(fields=["user", "program"], name="programrole_one_per_program"),
        ]

    def __str__(self):
        return f"{self.user} is {self.get_role_display()} in {self.program}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        sync_user_groups(self.user)

    def delete(self, *args, **kwargs):
        user = self.user
        super().delete(*args, **kwargs)
        sync_user_groups(user)


def sync_user_groups(user):
    """
    A user's Django groups are exactly the groups of the roles they hold in
    any program. Model-level permissions come from the groups; row-level
    scope comes from the roles (core.authz).
    """
    from django.contrib.auth.models import Group

    names = {
        ProgramRole.GROUP_NAMES[role]
        for role in ProgramRole.objects.filter(user=user).values_list("role", flat=True)
    }
    user.groups.set(Group.objects.filter(name__in=names))
