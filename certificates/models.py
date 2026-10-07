from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from attendance.aggregation import MinutesSource
from core.authz import ProgramScopedQuerySet
from core.models import AppendOnlyMixin, FrozenFieldsMixin, UUIDModel
from people.models import Person
from people.ownership import PersonOwnedQuerySet
from programs.models import Program
from rounds.models import RoundsEvent

from .rules import ATTENDANCE, CME, generate_verification_code


class CertificateQuerySet(PersonOwnedQuerySet, ProgramScopedQuerySet):
    program_lookup = "program"


class Certificate(FrozenFieldsMixin, UUIDModel):
    """
    An issued certificate: a snapshot, not a view.

    Every value the PDF prints is copied here at issue and never read from
    live data again. A mistake is fixed by issuing a new certificate whose
    `supersedes` points at this one. Only revocation changes a row.
    """

    class Type(models.TextChoices):
        CME = CME, "CME credit certificate"
        ATTENDANCE = ATTENDANCE, "Attendance certificate"

    FROZEN_FIELDS = (
        "program",
        "program_name",
        "institution_name",
        "certificate_type",
        "period_start",
        "period_end",
        "attendance_credits",
        "teaching_credits",
        "total_credits",
        "recipient_name",
        "recipient_credential",
        "licence_number",
        "licence_jurisdiction",
        "verification_code",
        "template_version",
        "pdf_path",
        "pdf_sha256",
        "issued_at",
        "issued_by",
        "supersedes",
    )

    person = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="certificates")
    # One certificate per program: a person attending two collects two.
    program = models.ForeignKey(Program, on_delete=models.PROTECT, related_name="certificates")
    program_name = models.CharField(max_length=200, help_text="Snapshotted. Programs get renamed.")
    institution_name = models.CharField(max_length=200, help_text="Snapshotted.")
    certificate_type = models.CharField(max_length=20, choices=Type.choices)
    period_start = models.DateField()
    period_end = models.DateField()
    # Each kind is rounded on its own; the total is their sum. Never a single
    # blended figure: it could not be split again later.
    attendance_credits = models.DecimalField(
        max_digits=6, decimal_places=2, validators=[MinValueValidator(0)]
    )
    teaching_credits = models.DecimalField(
        max_digits=6, decimal_places=2, validators=[MinValueValidator(0)]
    )
    total_credits = models.DecimalField(
        max_digits=6, decimal_places=2, validators=[MinValueValidator(0)]
    )
    recipient_name = models.CharField(max_length=400)
    recipient_credential = models.CharField(max_length=50, blank=True)
    # As the person entered it, never the normalized form.
    licence_number = models.CharField(max_length=50, blank=True)
    licence_jurisdiction = models.CharField(max_length=10, blank=True)
    verification_code = models.CharField(
        max_length=20, unique=True, default=generate_verification_code
    )
    accreditation_statement = models.TextField(
        blank=True,
        help_text="Snapshotted from the program when issued: names the accredited CPD provider.",
    )
    template_version = models.CharField(max_length=50)
    pdf_path = models.CharField(
        max_length=500, blank=True, help_text="Relative to UPLOAD_ROOT. The artefact of record."
    )
    pdf_sha256 = models.CharField(max_length=64, blank=True)
    issued_at = models.DateTimeField(default=timezone.now)
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    supersedes = models.OneToOneField(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="superseded_by",
        help_text="The certificate this one replaces.",
    )
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_reason = models.TextField(blank=True)

    objects = CertificateQuerySet.as_manager()

    class Meta:
        ordering = ["-issued_at"]
        permissions = [
            ("issue_certificate", "Can issue certificates"),
            ("revoke_certificate", "Can revoke certificates"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(period_end__gte=F("period_start")), name="certificate_period_in_order"
            ),
            models.CheckConstraint(
                condition=Q(total_credits__gte=0), name="certificate_total_not_negative"
            ),
            models.CheckConstraint(
                condition=Q(total_credits=F("attendance_credits") + F("teaching_credits")),
                name="certificate_total_is_sum_of_kinds",
            ),
            models.CheckConstraint(
                condition=Q(revoked_at__isnull=True, revoked_reason="")
                | (Q(revoked_at__isnull=False) & ~Q(revoked_reason="")),
                name="certificate_revocation_has_reason",
            ),
            models.CheckConstraint(
                condition=Q(supersedes__isnull=True) | ~Q(supersedes=F("id")),
                name="certificate_does_not_supersede_itself",
            ),
        ]

    def __str__(self):
        return f"{self.verification_code} {self.recipient_name}"

    @property
    def is_revoked(self):
        return self.revoked_at is not None

    @property
    def is_superseded(self):
        return hasattr(self, "superseded_by")


class CertificateLineQuerySet(PersonOwnedQuerySet, ProgramScopedQuerySet):
    person_lookup = "certificate__person"
    program_lookup = "certificate__program"


class CertificateLine(AppendOnlyMixin, UUIDModel):
    """
    One event on a certificate, with everything behind its credit figure.

    Credit is per event, so there is one line per event. `event` is kept for
    traceability only: nothing printed is read through it.
    """

    certificate = models.ForeignKey(Certificate, on_delete=models.PROTECT, related_name="lines")
    event = models.ForeignKey(RoundsEvent, on_delete=models.PROTECT, related_name="+")
    event_title = models.CharField(max_length=200)
    event_date = models.DateField()
    # Attendance: the sessions attended, not presented.
    session_titles = models.JSONField(default=list, help_text="Print-only snapshot.")
    attended_minutes = models.PositiveIntegerField()
    minutes_source = models.CharField(max_length=20, choices=MinutesSource.choices)
    # The program's rate AT ISSUE. A later rate change never touches this line.
    attendance_rate_per_hour = models.DecimalField(max_digits=4, decimal_places=2, default=1)
    attendance_computed = models.DecimalField(max_digits=5, decimal_places=2)
    attendance_adjustment = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    attendance_credits = models.DecimalField(max_digits=5, decimal_places=2)
    # Teaching: the sessions presented.
    presented_session_titles = models.JSONField(default=list, help_text="Print-only snapshot.")
    teaching_minutes = models.PositiveIntegerField(default=0)
    teaching_rate_per_hour = models.DecimalField(max_digits=4, decimal_places=2, default=1)
    teaching_computed = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    teaching_adjustment = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    teaching_credits = models.DecimalField(max_digits=5, decimal_places=2, default=0)

    objects = CertificateLineQuerySet.as_manager()

    class Meta:
        ordering = ["certificate", "event_date"]
        constraints = [
            models.UniqueConstraint(
                fields=["certificate", "event"], name="certificateline_one_per_event"
            ),
            models.CheckConstraint(
                condition=Q(attendance_credits__gte=0, teaching_credits__gte=0),
                name="certificateline_credits_not_negative",
            ),
        ]

    def __str__(self):
        return (
            f"{self.event_date} {self.event_title}: {self.attendance_credits} attendance, "
            f"{self.teaching_credits} teaching"
        )
