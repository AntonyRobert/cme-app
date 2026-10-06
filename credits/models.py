from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone

from core.constraints import is_quarter_multiple, validate_quarter_multiple
from core.models import AppendOnlyMixin, UUIDModel
from people.models import Person
from people.ownership import PersonOwnedQuerySet
from rounds.models import LearningObjective, RoundsEvent, Session


class EvaluationSubmission(UUIDModel):
    """One person's evaluation of one session. Half of what credit requires."""

    person = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="evaluations")
    session = models.ForeignKey(Session, on_delete=models.PROTECT, related_name="evaluations")
    submitted_at = models.DateTimeField(default=timezone.now)
    self_reported_minutes = models.PositiveSmallIntegerField(
        help_text="What they say they attended. Only used when there is no attendance record."
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
        super().clean()
        if not self.attestation:
            raise ValidationError({"attestation": "The attestation is required."})


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
        validators=[validate_quarter_multiple],
        help_text="Added to the computed credit. Negative to take away. Steps of 0.25.",
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
            models.CheckConstraint(
                condition=is_quarter_multiple("delta_credits"),
                name="creditadjustment_quarter_multiple",
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
