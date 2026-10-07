from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from core.authz import ProgramScopedQuerySet
from core.models import AppendOnlyMixin, FrozenFieldsMixin, UUIDModel
from people.models import Person
from people.ownership import PersonOwnedQuerySet
from rounds.models import LearningObjective, RoundsEvent, Session


# --- Evaluation form templates -------------------------------------------------


class EvaluationFormQuerySet(ProgramScopedQuerySet):
    program_lookup = "program"


class EvaluationForm(UUIDModel):
    """
    A named, reusable evaluation template belonging to a program. The
    questions live on immutable versions: editing a version that has
    submissions creates a new version and leaves the old one in place, so
    an evaluation submitted in October renders and reports with October's
    wording after the form is edited in March.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        ACTIVE = "active", "Active"
        RETIRED = "retired", "Retired"

    program = models.ForeignKey(
        "programs.Program", on_delete=models.PROTECT, related_name="evaluation_forms"
    )
    name = models.CharField(max_length=200)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    objects = EvaluationFormQuerySet.as_manager()

    class Meta:
        ordering = ["program", "name"]
        constraints = [
            models.UniqueConstraint(fields=["program", "name"], name="evaluationform_unique_name"),
        ]

    def __str__(self):
        return self.name

    @property
    def current_version(self):
        """The latest version: what a new submission answers."""
        return self.versions.order_by("-number").first()

    @property
    def is_active(self):
        return self.status == self.Status.ACTIVE


class EvaluationFormVersionQuerySet(ProgramScopedQuerySet):
    program_lookup = "form__program"


class EvaluationFormVersion(UUIDModel):
    """
    An immutable snapshot of a form's questions. Locked as soon as one
    submission answers it; from then on edits go to a new version.
    """

    form = models.ForeignKey(EvaluationForm, on_delete=models.PROTECT, related_name="versions")
    number = models.PositiveSmallIntegerField()
    note = models.CharField(max_length=300, blank=True, help_text="What changed, and why.")
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    objects = EvaluationFormVersionQuerySet.as_manager()

    class Meta:
        ordering = ["form", "-number"]
        constraints = [
            models.UniqueConstraint(fields=["form", "number"], name="evaluationformversion_unique_number"),
        ]

    def __str__(self):
        return f"{self.form} v{self.number}"

    @property
    def is_locked(self):
        """Has submissions: its questions may not change."""
        return self.submissions.exists()

    @property
    def submission_count(self):
        return self.submissions.count()


class EvaluationQuestionQuerySet(ProgramScopedQuerySet):
    program_lookup = "version__form__program"


class EvaluationQuestion(UUIDModel):
    """
    One question of one version. `question_key` is stable across versions:
    it is what makes year-over-year comparison work after a rewording.
    """

    class Kind(models.TextChoices):
        LIKERT_5 = "likert_5", "Likert, 1 to 5"
        YES_NO = "yes_no", "Yes / no"
        SINGLE_CHOICE = "single_choice", "One choice"
        MULTI_CHOICE = "multi_choice", "Several choices"
        FREE_TEXT = "free_text", "Free text"
        # Expands at render time into one Likert question per learning
        # objective of the session; {objective} in the prompt is replaced.
        PER_OBJECTIVE = "per_objective", "One per learning objective"

    CHOICE_KINDS = {"single_choice", "multi_choice"}
    RATING_KINDS = {"likert_5", "yes_no", "per_objective"}

    version = models.ForeignKey(
        EvaluationFormVersion, on_delete=models.CASCADE, related_name="questions"
    )
    question_key = models.SlugField(
        max_length=100, help_text="Stable across versions and years, so reports compare like with like."
    )
    position = models.PositiveSmallIntegerField(blank=True, help_text="Blank takes the next number.")
    prompt = models.TextField()
    help_text = models.CharField(max_length=300, blank=True)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    # Derived from the kind on save (rules.required_for_kind): Likert and
    # choice questions are mandatory, free text is optional unless a
    # condition makes it required.
    required = models.BooleanField(default=True, editable=False)
    required_when = models.JSONField(
        null=True,
        blank=True,
        help_text='Required only when another question has a given answer: '
        '{"question_key": "commercial_bias", "value": 0}. Free-text questions only.',
    )
    choices = models.JSONField(
        default=list, blank=True, help_text="The options, one per line, for choice questions."
    )

    objects = EvaluationQuestionQuerySet.as_manager()

    class Meta:
        ordering = ["version", "position"]
        constraints = [
            models.UniqueConstraint(
                fields=["version", "question_key"], name="evaluationquestion_unique_key"
            ),
            models.UniqueConstraint(
                fields=["version", "position"],
                name="evaluationquestion_unique_position",
                deferrable=models.Deferrable.DEFERRED,
            ),
        ]

    def __str__(self):
        return f"{self.position}. {self.question_key}"

    @property
    def condition(self):
        """(trigger question key, value) or None."""
        if not self.required_when:
            return None
        return self.required_when.get("question_key"), self.required_when.get("value")

    def clean(self):
        super().clean()
        errors = {}
        if self.kind in self.CHOICE_KINDS and not self.choices:
            errors["choices"] = "A choice question needs its options."
        if self.kind not in self.CHOICE_KINDS and self.choices:
            errors["choices"] = "Only choice questions have options."
        if self.kind == self.Kind.PER_OBJECTIVE and "{objective}" not in self.prompt:
            errors["prompt"] = "A per-objective prompt names where the objective goes: {objective}."
        if self.required_when:
            errors.update(self._condition_errors())
        if self.version_id and self.version.is_locked:
            errors["__all__"] = (
                "This version has submissions and is locked. Create a new version of the form."
            )
        if errors:
            raise ValidationError(errors)

    def _condition_errors(self):
        if self.kind != self.Kind.FREE_TEXT:
            return {"required_when": "Only a free-text question can be conditionally required; the others always are."}
        if not isinstance(self.required_when, dict) or "question_key" not in self.required_when or "value" not in self.required_when:
            return {"required_when": 'Give {"question_key": ..., "value": ...}.'}
        key = self.required_when["question_key"]
        if key == self.question_key:
            return {"required_when": "A question cannot depend on itself."}
        trigger = None
        if self.version_id:
            trigger = EvaluationQuestion.objects.filter(version_id=self.version_id, question_key=key).first()
        if trigger is None:
            return {"required_when": f"No question {key!r} in this version."}
        if trigger.kind in (self.Kind.FREE_TEXT, self.Kind.PER_OBJECTIVE):
            return {"required_when": "The trigger must be a Likert, yes/no or choice question."}
        return {}

    def save(self, *args, **kwargs):
        self.required = self.kind != self.Kind.FREE_TEXT
        if self.position is None and self.version_id:
            from rounds.models import next_position

            self.position = next_position(
                EvaluationQuestion.objects.filter(version_id=self.version_id).exclude(pk=self.pk)
            )
        if self.version_id and self.version.is_locked and not kwargs.pop("unlock", False):
            raise ValidationError("This version has submissions and is locked.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.version.is_locked:
            raise ValidationError("This version has submissions and is locked.")
        return super().delete(*args, **kwargs)


class EvaluationSubmissionQuerySet(PersonOwnedQuerySet, ProgramScopedQuerySet):
    program_lookup = "session__event__program"

    def for_programs(self, programs):
        ids = [getattr(p, "pk", p) for p in programs]
        return self.filter(Q(session__event__program__in=ids) | Q(event__program__in=ids))

    def for_sessions(self):
        return self.filter(session__isnull=False)

    def for_events(self):
        """The overall-activity evaluations."""
        return self.filter(event__isnull=False)


class EvaluationSubmission(UUIDModel):
    """
    One person's evaluation of one session, or of one event's overall
    activity (then `event` is set and `session` is null). Only a session
    evaluation can gate credit, and only when the program says so.
    """

    person = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="evaluations")
    session = models.ForeignKey(
        Session, null=True, blank=True, on_delete=models.PROTECT, related_name="evaluations"
    )
    event = models.ForeignKey(
        RoundsEvent,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="activity_evaluations",
        help_text="Set, with session blank, for the overall-activity evaluation.",
    )
    form_version = models.ForeignKey(
        EvaluationFormVersion,
        on_delete=models.PROTECT,
        related_name="submissions",
        help_text="The questions, as worded when this was answered. Never re-pointed.",
    )
    submitted_at = models.DateTimeField(default=timezone.now)
    self_reported_session_minutes = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        help_text="How long they say they attended this session. Only used when there "
        "is no attendance record. Blank on a draft.",
    )
    attestation = models.BooleanField(
        default=False, help_text="They confirm the minutes are accurate. Part of what makes it complete."
    )
    is_complete = models.BooleanField(
        default=False,
        editable=False,
        help_text="Cache: every required question of the form version is answered. "
        "Recomputed from the responses on every save; the responses are the truth.",
    )

    objects = EvaluationSubmissionQuerySet.as_manager()

    class Meta:
        ordering = ["-submitted_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["person", "session"], name="evaluationsubmission_one_per_session"
            ),
            models.UniqueConstraint(
                fields=["person", "event"],
                condition=Q(session__isnull=True),
                name="evaluationsubmission_one_activity_per_event",
            ),
            models.CheckConstraint(
                condition=Q(session__isnull=False, event__isnull=True)
                | Q(session__isnull=True, event__isnull=False),
                name="evaluationsubmission_session_or_event",
            ),
            # A draft may lack both; a complete session evaluation is attested with
            # minutes. An activity evaluation makes no attendance claim.
            models.CheckConstraint(
                condition=Q(is_complete=False)
                | Q(session__isnull=True)
                | (Q(attestation=True) & Q(self_reported_session_minutes__isnull=False)),
                name="evaluationsubmission_complete_is_attested",
            ),
        ]

    def __str__(self):
        return f"{self.person} on {self.session or self.event}"

    @property
    def is_activity(self):
        return self.session_id is None

    @property
    def target(self):
        """The session, or the event for an activity evaluation."""
        return self.session if self.session_id else self.event

    @property
    def program(self):
        return (self.session.event if self.session_id else self.event).program

    def clean(self):
        from .windows import submission_allowed

        super().clean()
        if self._state.adding and self.person_id and self.session_id:
            if not submission_allowed(self.person, self.session):
                raise ValidationError(
                    "The evaluation window for this session has closed. "
                    "Grant a reopening first (Evaluation windows)."
                )

    def save(self, *args, **kwargs):
        from .evaluation_forms import compute_is_complete
        from .windows import close_windows

        if self.pk and not self._state.adding:
            self.is_complete = compute_is_complete(self)
        else:
            self.is_complete = False  # no responses can exist before the row does
        super().save(*args, **kwargs)
        if self.is_complete and self.session_id:
            close_windows(self.person, self.session)

    def recompute(self):
        """Responses changed: refresh the cache (and close windows if now complete)."""
        self.save(update_fields=None)


class EvaluationResponseQuerySet(PersonOwnedQuerySet, ProgramScopedQuerySet):
    person_lookup = "submission__person"
    program_lookup = "submission__session__event__program"

    def for_programs(self, programs):
        ids = [getattr(p, "pk", p) for p in programs]
        return self.filter(
            Q(submission__session__event__program__in=ids) | Q(submission__event__program__in=ids)
        )


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
    rating = models.SmallIntegerField(null=True, blank=True, help_text="Likert 1 to 5; yes 1, no 0.")
    free_text = models.TextField(null=True, blank=True)
    selected = models.JSONField(
        null=True, blank=True, help_text="The option(s) chosen, for choice questions."
    )

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

    @property
    def answered(self):
        return (
            self.rating is not None
            or bool((self.free_text or "").strip())
            or bool(self.selected)
        )

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        self.submission.recompute()

    def delete(self, *args, **kwargs):
        submission = self.submission
        result = super().delete(*args, **kwargs)
        submission.recompute()
        return result


class EvaluationWindowQuerySet(PersonOwnedQuerySet, ProgramScopedQuerySet):
    program_lookup = "session__event__program"


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

    objects = EvaluationWindowQuerySet.as_manager()

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


class CreditKind(models.TextChoices):
    ATTENDANCE = "attendance", "Attendance"
    TEACHING = "teaching", "Teaching"


class CreditAdjustmentQuerySet(PersonOwnedQuerySet, ProgramScopedQuerySet):
    program_lookup = "event__program"


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
    kind = models.CharField(
        max_length=20,
        choices=CreditKind.choices,
        default=CreditKind.ATTENDANCE,
        help_text="Which kind of credit this changes. They are never blended.",
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

    objects = CreditAdjustmentQuerySet.as_manager()

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
