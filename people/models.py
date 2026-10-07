from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Lower

from core.models import UUIDModel

from .normalize import normalize_domain, normalize_email, normalize_licence
from .ownership import PersonOwnedQuerySet


class Person(UUIDModel):
    """
    Everyone who attends or presents. The spine of the schema.

    Rows are never deleted. A duplicate is merged by pointing merged_into
    at the survivor (see docs/data-model.md, "Merging").
    """

    class Role(models.TextChoices):
        PHYSICIAN = "physician", "Physician"
        NURSE = "nurse", "Nurse"
        PHARMACIST = "pharmacist", "Pharmacist"
        TRAINEE = "trainee", "Trainee"
        STUDENT = "student", "Student"
        OTHER = "other", "Other"

    class Jurisdiction(models.TextChoices):
        CMQ = "CMQ", "CMQ"
        CPSO = "CPSO", "CPSO"
        OIIQ = "OIIQ", "OIIQ"
        OPQ = "OPQ", "OPQ"
        OTHER = "other", "Other"

    # Names are kept exactly as typed. Never title-case or "clean" them.
    given_name = models.CharField(max_length=200)
    family_name = models.CharField(max_length=200)
    credential = models.CharField(
        max_length=50, blank=True, help_text="MD, RN, PharmD, PhD. Prints on the certificate."
    )
    role = models.CharField(max_length=20, choices=Role.choices)
    # Free text, rendered exactly as typed. Not a lookup: institutions are
    # named however the person names them, several at once if they like.
    affiliation = models.CharField(
        max_length=300,
        blank=True,
        help_text="Appears on the flyer and on certificates, exactly as typed. "
        "For example a university department. More than one is fine.",
    )
    employer = models.CharField(
        max_length=300,
        blank=True,
        help_text="Appears on the flyer and on certificates, exactly as typed. "
        "For example a hospital or clinic. More than one is fine.",
    )

    licence_number = models.CharField(
        max_length=50,
        null=True,
        blank=True,
        help_text="As entered. This is what prints on the certificate.",
    )
    # Derived from licence_number on save. Only for matching and uniqueness.
    licence_number_normalized = models.CharField(max_length=50, null=True, editable=False)
    licence_jurisdiction = models.CharField(
        max_length=10, choices=Jurisdiction.choices, null=True, blank=True
    )

    merged_into = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="merged_from",
        help_text="Set by a merge. This record is then a tombstone.",
    )
    staff_user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="person",
        help_text="Only for a staff member who also attends.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "people"
        ordering = ["family_name", "given_name"]
        permissions = [("merge_person", "Can merge duplicate people")]
        constraints = [
            # Tombstones are exempt, so a merged-away duplicate never blocks
            # the survivor from holding the number.
            models.UniqueConstraint(
                fields=["licence_jurisdiction", "licence_number_normalized"],
                condition=Q(
                    licence_jurisdiction__isnull=False,
                    licence_number_normalized__isnull=False,
                    merged_into__isnull=True,
                ),
                name="person_unique_active_licence",
            ),
            models.CheckConstraint(
                condition=Q(licence_number__isnull=True) | Q(licence_jurisdiction__isnull=False),
                name="person_licence_needs_jurisdiction",
            ),
            models.CheckConstraint(
                condition=Q(merged_into__isnull=True) | ~Q(merged_into=F("id")),
                name="person_not_merged_into_self",
            ),
        ]

    def __str__(self):
        return f"{self.family_name}, {self.given_name}"

    @property
    def full_name(self):
        return f"{self.given_name} {self.family_name}"

    @property
    def is_merged(self):
        return self.merged_into_id is not None

    def _normalize(self):
        if self.licence_number is not None:
            self.licence_number = self.licence_number.strip() or None
        self.licence_number_normalized = normalize_licence(self.licence_number)
        if not self.licence_jurisdiction:
            self.licence_jurisdiction = None

    def clean(self):
        super().clean()
        self._normalize()
        if self.licence_number and not self.licence_jurisdiction:
            raise ValidationError(
                {"licence_jurisdiction": "A licence number needs its jurisdiction."}
            )
        # Checked here as well as by the database constraint, because the
        # normalized field isn't on any form and Django would skip it.
        # Staff-facing message only: a public form must not reveal that a
        # licence number is already on file (see docs/decisions.md).
        if self.licence_number_normalized and not self.is_merged:
            clash = Person.objects.filter(
                licence_jurisdiction=self.licence_jurisdiction,
                licence_number_normalized=self.licence_number_normalized,
                merged_into__isnull=True,
            ).exclude(pk=self.pk)
            if clash.exists():
                raise ValidationError(
                    {
                        "licence_number": "Another person already holds this licence "
                        "number. This is probably a duplicate record to merge."
                    }
                )

    def save(self, *args, **kwargs):
        self._normalize()
        super().save(*args, **kwargs)


class PersonEmail(UUIDModel):
    """
    An address a person is known by. One person, many addresses.

    This table is what matches a Teams row to a person, so addresses are
    stored lowercased and are unique across everyone.
    """

    person = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="emails")
    email = models.EmailField(max_length=254, unique=True)
    verified_at = models.DateTimeField(
        null=True, blank=True, help_text="Set when a magic link sent to this address is used."
    )
    is_primary = models.BooleanField(
        default=False, help_text="Where merge offers and certificates go."
    )
    added_at = models.DateTimeField(auto_now_add=True)

    objects = PersonOwnedQuerySet.as_manager()

    class Meta:
        ordering = ["email"]
        constraints = [
            models.CheckConstraint(
                condition=Q(email=Lower("email")), name="personemail_email_lowercase"
            ),
            models.UniqueConstraint(
                fields=["person"],
                condition=Q(is_primary=True),
                name="personemail_one_primary_per_person",
            ),
        ]

    def __str__(self):
        return self.email

    def clean(self):
        super().clean()
        self.email = normalize_email(self.email)

    def save(self, *args, **kwargs):
        self.email = normalize_email(self.email)
        super().save(*args, **kwargs)


class AllowedDomain(UUIDModel):
    """An institutional email domain whose addresses may sign in."""

    domain = models.CharField(max_length=253, unique=True)
    auto_admit = models.BooleanField(
        default=True, help_text="Off means sign-ins from this domain go to the review queue."
    )
    note = models.TextField(blank=True, help_text="Why it was added.")

    class Meta:
        ordering = ["domain"]
        constraints = [
            models.CheckConstraint(
                condition=Q(domain=Lower("domain")), name="alloweddomain_domain_lowercase"
            ),
        ]

    def __str__(self):
        return self.domain

    def clean(self):
        super().clean()
        self.domain = normalize_domain(self.domain)

    def save(self, *args, **kwargs):
        self.domain = normalize_domain(self.domain)
        super().save(*args, **kwargs)


class SignInRequest(UUIDModel):
    """A sign-in from an address outside AllowedDomain, waiting for a staff decision."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    email = models.EmailField(max_length=254)
    given_name = models.CharField(max_length=200)
    family_name = models.CharField(max_length=200)
    requested_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    note = models.TextField(blank=True)

    class Meta:
        ordering = ["-requested_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(email=Lower("email")), name="signinrequest_email_lowercase"
            ),
        ]

    def __str__(self):
        return f"{self.email} ({self.status})"

    def clean(self):
        super().clean()
        self.email = normalize_email(self.email)

    def save(self, *args, **kwargs):
        self.email = normalize_email(self.email)
        super().save(*args, **kwargs)
