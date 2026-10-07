from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from core.models import AppendOnlyMixin, FrozenFieldsMixin, UUIDModel
from people.models import Person
from people.ownership import PersonOwnedQuerySet
from rounds.models import LearningObjective, RoundsEvent, Session


class EvaluationSubmission(UUIDModel):
    """One person's evaluation of one session. Half of what credit requires."""

    person = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="evaluations")
    session = models.ForeignKey(Session, on_delete=models.PROTECT, related_name="evaluations")
    submitted_at = models.DateTimeField(default=timezone.now)
    self_reported_session_minutes = models.PositiveSmallIntegerField(
        help_text="How long they say they attended this session. Only used when there "
        "is no attendance record."
    )
    attestation = models.BooleanField(help_text="They confirm the minutes are accurate.")
    is_complete = models.BooleanField(
        default=False, help_text="All required objective questions are answered."
    )

    objects = PersonOwnedQuerySet.as_manager()

    class Meta:
        ordering = ["-submitted_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["person", "session"], name="evaluationsubmission_one_per_session"
            ),
            models.CheckConstraint(
                condition=Q(attestation=True), name="evaluationsubmission_attested"
            ),
        ]

    def __str__(self):
        return f"{self.person} on {self.session}"

    def clean(self):
        from .windows import submission_allowed

        super().clean()
        if not self.attestation:
            raise ValidationError({"attestation": "The attestation is required."})
        if self._state.adding and self.person_id and self.session_id:
            if not submission_allowed(self.person, self.session):
                raise ValidationError(
                    "The evaluation window for this session has closed. "
                    "Grant a reopening first (Evaluation windows)."
                )

    def save(self, *args, **kwargs):
        from .windows import close_windows

        super().save(*args, **kwargs)
        if self.is_complete:
            close_windows(self.person, self.session)


class EvaluationResponseQuerySet(PersonOwnedQuerySet):
    person_lookup = "submission__person"


class EvaluationResponse(UUIDModel):
    """One answer inside a submission."""

    submission = models.ForeignKey(
        EvaluationSubmission, on_delete=models.CASCADE, related_name="responses"
    )
    objective = models.ForeignKey(
        LearningObjective,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="responses",
        help_text="Blank for general questions.",
    )
    question_key = models.CharField(
        max_length=100, help_text="Stable across years, so reports compare like with like."
    )
    rating = models.SmallIntegerField(null=True, blank=True)
    free_text = models.TextField(null=True, blank=True)

    objects = EvaluationResponseQuerySet.as_manager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["submission", "question_key", "objective"],
                nulls_distinct=False,
                name="evaluationresponse_one_answer_per_question",
            ),
        ]

    def __str__(self):
        return f"{self.question_key}: {self.rating if self.rating is not None else 'text'}"


class EvaluationWindow(FrozenFieldsMixin, UUIDModel):
    """
    A reopened evaluation form for one person and one session.

    The form is open for a week after the event by default. After that,
    an attendee can ask for another week; the request is granted
    automatically (they did the attending, the form is work they still
    owe) and logged. The window closes when they submit a complete
    evaluation, or when it expires.
    """

    FROZEN_FIELDS = ("person", "session", "opened_at", "expires_at", "reason", "granted_by")

    person = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="evaluation_windows")
    session = models.ForeignKey(
        Session, on_delete=models.PROTECT, related_name="evaluation_windows"
    )
    opened_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    reason = models.TextField(blank=True)
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        help_text="Blank when the attendee asked for it themselves.",
    )
    closed_at = models.DateTimeField(
        null=True, blank=True, help_text="Set when a complete evaluation is submitted."
    )

    objects = PersonOwnedQuerySet.as_manager()

    class Meta:
        ordering = ["-opened_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(expires_at__gt=F("opened_at")), name="evaluationwindow_expires_later"
            ),
        ]

    def __str__(self):
        return f"{self.person} for {self.session} until {timezone.localtime(self.expires_at):%Y-%m-%d}"

    def is_open(self, at=None):
        at = at or timezone.now()
        return self.closed_at is None and self.opened_at <= at < self.expires_at


class CreditAdjustment(AppendOnlyMixin, UUIDModel):
    """
    A ledger entry that changes someone's credit for an event without
    changing the attendance record.

    Rare. Wrong hours are fixed by correcting attendance. This is for when
    the hours are right and the credit still isn't: a rule change, or a
    goodwill grant that should stay visibly separate.
    """

    person = models.ForeignKey(
        Person, on_delete=models.PROTECT, related_name="credit_adjustments"
    )
    event = models.ForeignKey(
        RoundsEvent, on_delete=models.PROTECT, related_name="credit_adjustments"
    )
    delta_credits = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        help_text="Added to the computed credit. Negative to take away.",
    )
    reason = models.TextField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = PersonOwnedQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=~Q(reason=""), name="creditadjustment_needs_reason"
            ),
            models.CheckConstraint(
                condition=~Q(delta_credits=0), name="creditadjustment_not_zero"
            ),
        ]

    def __str__(self):
        return f"{self.delta_credits:+} for {self.person} at {self.event.date}"

    def clean(self):
        super().clean()
        if not (self.reason or "").strip():
            raise ValidationError({"reason": "Say why."})
        if self.delta_credits == 0:
            raise ValidationError({"delta_credits": "An adjustment of zero does nothing."})
