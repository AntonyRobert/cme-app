import datetime
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from core.authz import ProgramScopedQuerySet
from core.constraints import is_quarter_multiple, validate_quarter_multiple
from core.models import AppendOnlyMixin, ImmutableRowError, UUIDModel
from programs.models import Program
from people.identity import identity_ids
from people.models import Person
from people.ownership import PersonOwnedQuerySet

# Attended time counts from this long before the first session starts to
# this long after the last one ends. Symmetric on purpose.
CREDIT_WINDOW_GRACE = datetime.timedelta(minutes=5)

# A session's end defaults to this long after its start.
DEFAULT_SESSION_LENGTH = datetime.timedelta(hours=1)
# An event's end defaults to this long after its start: three one-hour talks.
DEFAULT_EVENT_LENGTH = datetime.timedelta(hours=3)


def default_event_title():
    """Kept for migration 0001; events now take their title from the program."""
    return settings.SERIES_NAME


def next_position(queryset):
    """The number after the highest position already used among `queryset`."""
    highest = queryset.aggregate(models.Max("position"))["position__max"]
    return (highest or 0) + 1


def coi_questions(version):
    """[(question_key, text)] for a questionnaire version. Raises on an unknown version."""
    try:
        return list(settings.COI_QUESTIONS[version])
    except KeyError:
        raise LookupError(f"No conflict-of-interest questionnaire version {version!r}.")


def coi_is_national_standard(version):
    """Laid out as the National Standard form (binary, two columns, role, speaker questions, attestation)."""
    return version in settings.COI_NATIONAL_STANDARD_VERSIONS


def coi_standard():
    return settings.COI_STANDARD


def coi_validity():
    return datetime.timedelta(days=settings.COI_VALIDITY_DAYS)


def end_of_academic_year(today=None):
    """
    The next 30 June. No longer used by any model: migration 0001 refers to
    it as the old default of a removed field, so it has to keep existing.
    """
    today = today or timezone.localdate()
    year = today.year if (today.month, today.day) <= (6, 30) else today.year + 1
    return datetime.date(year, 6, 30)


class RoundsEventQuerySet(ProgramScopedQuerySet):
    program_lookup = "program"


class RoundsEvent(UUIDModel):
    """One fortnightly rounds: a block of sessions people attend as a whole."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PUBLISHED = "published", "Published"
        HELD = "held", "Held"
        CLOSED = "closed", "Closed"

    program = models.ForeignKey(
        Program,
        on_delete=models.PROTECT,
        related_name="events",
        help_text="Decides the credit rates, the series name and whose certificate this "
        "ends up on.",
    )
    title = models.CharField(
        max_length=200,
        blank=True,
        help_text="Blank means the program's series name. Prints on certificate lines.",
    )
    date = models.DateField(blank=True, help_text="Blank means the day it starts.")
    start_at = models.DateTimeField(help_text="Every session must fall between these two.")
    end_at = models.DateTimeField(blank=True, help_text="Blank means three hours after the start.")
    teams_join_url = models.URLField(max_length=2000, blank=True)
    teams_meeting_title = models.CharField(
        max_length=300,
        blank=True,
        help_text="The meeting title exactly as Teams shows it. An attendance export is "
        "matched to this event on this title plus the date. Teams exports carry no meeting ID.",
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    evaluation_form = models.ForeignKey(
        "credits.EvaluationForm",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        help_text="The evaluation form this event's sessions use. Blank means the program's default.",
    )
    accredited_credits = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        blank=True,
        validators=[MinValueValidator(Decimal("0")), validate_quarter_multiple],
        help_text="The accreditor-set ceiling on attendance credit for this event, in steps "
        "of 0.25. Blank means the program's default.",
    )

    objects = RoundsEventQuerySet.as_manager()

    class Meta:
        ordering = ["-date"]
        constraints = [
            models.CheckConstraint(
                condition=Q(end_at__gt=F("start_at")), name="roundsevent_ends_after_start"
            ),
            models.CheckConstraint(
                condition=Q(accredited_credits__gte=0), name="roundsevent_credits_not_negative"
            ),
            models.CheckConstraint(
                condition=is_quarter_multiple("accredited_credits"),
                name="roundsevent_credits_quarter_multiple",
            ),
        ]

    def __str__(self):
        return f"{self.date} {self.title}"

    @property
    def is_closed(self):
        return self.status == self.Status.CLOSED

    def _stored_status(self):
        if self._state.adding:
            return None
        return RoundsEvent.objects.filter(pk=self.pk).values_list("status", flat=True).first()

    def _fill_defaults(self):
        if self.start_at:
            if self.end_at is None:
                self.end_at = self.start_at + DEFAULT_EVENT_LENGTH
            if self.date is None:
                self.date = timezone.localdate(self.start_at)
        if self.program_id:
            if not self.title:
                self.title = self.program.series_name
            if self.accredited_credits is None:
                self.accredited_credits = self.program.default_accredited_credits

    def undeclared_presenters(self):
        """
        [SessionPresenter] on this event with no declaration attached. The
        National Standard says anyone who fails to disclose cannot
        participate as an SPC member, speaker, moderator, facilitator or
        author, so these block publication.
        """
        if not self.pk:
            return []
        return list(
            SessionPresenter.objects.filter(session__event=self, coi_declaration__isnull=True)
            .select_related("person", "session")
            .order_by("session__position", "position")
        )

    def publication_blockers(self):
        """Human-readable reasons this event cannot be published; empty means it can."""
        return [
            f"{sp.person} (session {sp.session.position}, {sp.session.title}) has no "
            "conflict-of-interest declaration on file."
            for sp in self.undeclared_presenters()
        ]

    def clean(self):
        super().clean()
        self._fill_defaults()
        if self.start_at and self.end_at and self.end_at <= self.start_at:
            raise ValidationError({"end_at": "The event must end after it starts."})
        if self._stored_status() == self.Status.CLOSED and not self.is_closed:
            raise ValidationError(
                {"status": "A closed event stays closed. Its totals are frozen."}
            )
        moving_to_published = (
            self.status == self.Status.PUBLISHED and self._stored_status() != self.Status.PUBLISHED
        )
        if moving_to_published:
            blockers = self.publication_blockers()
            if blockers:
                raise ValidationError(
                    {
                        "status": "Cannot publish: a presenter has not disclosed. "
                        + " ".join(blockers)
                        + " Anyone who fails to disclose cannot participate (National Standard)."
                    }
                )

    def save(self, *args, **kwargs):
        if self._stored_status() == self.Status.CLOSED and not self.is_closed:
            raise ImmutableRowError("A closed event cannot be reopened.")
        self._fill_defaults()
        super().save(*args, **kwargs)


class COIDeclarationQuerySet(PersonOwnedQuerySet):
    def valid_on(self, on_date):
        """
        Declarations in force on a date: made on or before it, less than a
        year earlier. Days are local days: a declaration made at 21:00 in
        Montreal is in force that evening, although it is already tomorrow
        in UTC.
        """
        day_after = timezone.make_aware(
            datetime.datetime.combine(on_date + datetime.timedelta(days=1), datetime.time.min)
        )
        return self.filter(declared_at__lt=day_after, declared_at__gte=day_after - coi_validity())

    def current_for(self, person, on_date):
        """
        The person's most recent complete declaration in force on on_date, or
        None. An expired or unfinished declaration is never picked up.
        """
        for declaration in (
            self.for_person(person).valid_on(on_date).prefetch_related("responses").order_by("-declared_at")
        ):
            if declaration.is_complete:
                return declaration
        return None


class COIDeclaration(AppendOnlyMixin, UUIDModel):
    """
    A conflict-of-interest declaration, as made: one COIResponse per
    question of its questionnaire version.

    Never edited: a change of circumstances is a new declaration. That is
    what lets SessionPresenter point at one as a snapshot. Valid for a
    year from declared_at, rolling. Write one with rounds.coi.declare().
    """

    class Role(models.TextChoices):
        SPC_MEMBER = "spc_member", "Scientific planning committee member"
        MODERATOR = "moderator", "Moderator"
        SPEAKER = "speaker", "Speaker"
        AUTHOR = "author", "Author"
        FACILITATOR = "facilitator", "Facilitator"
        OTHER = "other", "Other"

    person = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="coi_declarations")
    declared_at = models.DateTimeField(default=timezone.now)
    disclosure_text_version = models.CharField(
        max_length=50,
        help_text="Which questionnaire was answered. The declaration always shows that "
        "version's wording.",
    )
    # --- The National Standard form's own fields. Blank on legacy declarations. ---
    activity_role = models.CharField(
        max_length=20, choices=Role.choices, blank=True, help_text="Role in the activity."
    )
    activity_role_other = models.CharField(max_length=200, blank=True)
    has_relationships = models.BooleanField(
        null=True,
        blank=True,
        help_text="The form's top-level answer. No writes an explicit no to every category.",
    )
    # Speaker-only. Null unless the role is speaker.
    off_label = models.BooleanField(
        null=True, blank=True, help_text="Intends to recommend off-label use of medication."
    )
    generic_names_acknowledged = models.BooleanField(
        null=True,
        blank=True,
        help_text="Acknowledges the generic-names requirement. A no is flagged for the program admin.",
    )
    attested = models.BooleanField(default=False, help_text="'I agree' was ticked.")
    attested_name = models.CharField(
        max_length=400, blank=True, help_text="The declarant's name as it was when they agreed."
    )

    objects = COIDeclarationQuerySet.as_manager()

    class Meta:
        verbose_name = "COI declaration"
        ordering = ["-declared_at"]

    def __str__(self):
        return f"{self.person}: {self.summary} ({timezone.localdate(self.declared_at)})"

    @property
    def valid_until(self):
        """The last day this declaration is in force."""
        return timezone.localdate(self.declared_at + coi_validity()) - datetime.timedelta(days=1)

    def is_valid_on(self, on_date):
        return timezone.localdate(self.declared_at) <= on_date <= self.valid_until

    @property
    def questions(self):
        return coi_questions(self.disclosure_text_version)

    @property
    def is_national_standard(self):
        return coi_is_national_standard(self.disclosure_text_version)

    @property
    def is_speaker(self):
        return self.activity_role == self.Role.SPEAKER

    @property
    def role_label(self):
        if not self.activity_role:
            return ""
        label = self.get_activity_role_display()
        if self.activity_role == self.Role.OTHER and self.activity_role_other:
            label = f"{label}: {self.activity_role_other}"
        return label

    @property
    def is_complete(self):
        """
        Every question of its version has an answer; unanswered is not "no".
        On the National Standard form also: a role, the top-level answer, the
        speaker questions when the role is speaker, and the attestation.
        """
        answered = {r.question_key for r in self.responses.all()}
        if not answered >= {key for key, _ in self.questions}:
            return False
        if not self.is_national_standard:
            return True
        if not self.activity_role or self.has_relationships is None or not self.attested:
            return False
        if self.is_speaker and (self.off_label is None or self.generic_names_acknowledged is None):
            return False
        return True

    @property
    def needs_review(self):
        """A speaker who would not acknowledge the generic-names requirement."""
        return self.generic_names_acknowledged is False

    @property
    def has_conflict(self):
        return any(r.has_conflict for r in self.responses.all())

    @property
    def summary(self):
        if not self.is_complete:
            return "incomplete"
        declared = [r for r in self.responses.all() if r.has_conflict]
        return f"{len(declared)} conflict(s) declared" if declared else "no conflicts"

    def rendered(self):
        """
        [(question text, has_conflict or None, organizations, description)] in
        question order, using the wording of this declaration's own version.
        None means unanswered. Legacy declarations have their one explanation
        in the description column and nothing in organizations.
        """
        answers = {r.question_key: r for r in self.responses.all()}
        rows = []
        for key, text in self.questions:
            response = answers.get(key)
            if response is None:
                rows.append((text, None, "", ""))
            else:
                rows.append((text, response.has_conflict, response.organizations, response.relationship_description))
        return rows

    def clean(self):
        super().clean()
        try:
            coi_questions(self.disclosure_text_version)
        except LookupError as error:
            raise ValidationError({"disclosure_text_version": str(error)})


class COIResponse(AppendOnlyMixin, UUIDModel):
    """
    One answer on a declaration. A yes names the organization(s) and
    describes the relationship(s), the form's two columns. Legacy
    declarations carried one explanation; it lives in the description.
    """

    declaration = models.ForeignKey(
        COIDeclaration, on_delete=models.CASCADE, related_name="responses"
    )
    question_key = models.CharField(max_length=50)
    has_conflict = models.BooleanField()
    organizations = models.TextField(
        blank=True, help_text="Name of for-profit or not-for-profit organization(s). Required when yes."
    )
    relationship_description = models.TextField(
        blank=True, help_text="Description of relationship(s). Required when yes."
    )

    class Meta:
        verbose_name = "COI response"
        ordering = ["declaration", "question_key"]
        constraints = [
            models.UniqueConstraint(
                fields=["declaration", "question_key"], name="coiresponse_one_per_question"
            ),
            # Every version needs the description on a yes; the National Standard
            # form needs the organizations too, which the model and the service
            # enforce (a check constraint cannot see the parent's version).
            # Every version needs the description on a yes; the National Standard
            # form needs the organizations too, which the model and the service
            # enforce (a check constraint cannot see the parent's version).
            models.CheckConstraint(
                condition=Q(has_conflict=False) | ~Q(relationship_description=""),
                name="coiresponse_yes_needs_description",
            ),
            models.CheckConstraint(
                condition=Q(has_conflict=True) | (Q(organizations="") & Q(relationship_description="")),
                name="coiresponse_no_is_blank",
            ),
        ]

    def __str__(self):
        return f"{self.question_key}: {'yes' if self.has_conflict else 'no'}"

    def clean(self):
        super().clean()
        if self.has_conflict and not (self.relationship_description or "").strip():
            raise ValidationError({"relationship_description": "Describe the relationship(s)."})
        if (
            self.has_conflict
            and self.declaration_id
            and self.declaration.is_national_standard
            and not (self.organizations or "").strip()
        ):
            raise ValidationError({"organizations": "Name the organization(s)."})
        if self.declaration_id:
            keys = {key for key, _ in self.declaration.questions}
            if self.question_key not in keys:
                raise ValidationError(
                    {"question_key": f"Not a question of version {self.declaration.disclosure_text_version}."}
                )


class SessionQuerySet(ProgramScopedQuerySet):
    program_lookup = "event__program"


class Session(UUIDModel):
    """
    One lecture inside an event. The evaluation form targets a session, and
    credit is earned session by session.

    Presenters give the start and end time when they submit. Attended time
    is clamped to these intervals: a break between sessions is not
    educational activity.
    """

    event = models.ForeignKey(RoundsEvent, on_delete=models.PROTECT, related_name="sessions")
    position = models.PositiveSmallIntegerField(
        blank=True, help_text="Order on the flyer. Blank takes the next number."
    )
    title = models.CharField(max_length=300)
    presenters = models.ManyToManyField(
        Person, through="SessionPresenter", related_name="sessions_presented"
    )
    start_at = models.DateTimeField(
        blank=True, help_text="Blank means right after the previous session, or the event's start."
    )
    end_at = models.DateTimeField(blank=True, help_text="Blank means one hour after the start.")
    evaluation_form = models.ForeignKey(
        "credits.EvaluationForm",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        help_text="The evaluation form this talk uses. Blank means the event's, then the program's.",
    )
    draft_blurb = models.TextField(blank=True)
    published_blurb = models.TextField(blank=True)
    submitted_at = models.DateTimeField(
        null=True, blank=True, help_text="Blank means the presenters haven't filled it in yet."
    )

    objects = SessionQuerySet.as_manager()

    class Meta:
        ordering = ["event", "start_at", "position"]
        constraints = [
            models.UniqueConstraint(
                fields=["event", "position"],
                name="session_unique_position",
                # Checked at commit, so two rows can swap numbers in one save.
                deferrable=models.Deferrable.DEFERRED,
            ),
            models.CheckConstraint(
                condition=Q(end_at__gt=F("start_at")), name="session_ends_after_start"
            ),
        ]

    def __str__(self):
        return f"{self.event.date} #{self.position} {self.title}"

    @property
    def length_seconds(self):
        return int((self.end_at - self.start_at).total_seconds())

    @property
    def length_minutes(self):
        return self.length_seconds // 60

    def _fill_defaults(self):
        if self.position is None and self.event_id:
            self.position = next_position(
                Session.objects.filter(event_id=self.event_id).exclude(pk=self.pk)
            )
        if self.start_at is None and self.event_id:
            previous = (
                Session.objects.filter(event_id=self.event_id)
                .exclude(pk=self.pk)
                .order_by("-end_at")
                .first()
            )
            self.start_at = previous.end_at if previous else self.event.start_at
        if self.start_at and self.end_at is None:
            self.end_at = self.start_at + DEFAULT_SESSION_LENGTH

    def clean(self):
        super().clean()
        self._fill_defaults()
        if not (self.start_at and self.end_at):
            return
        errors = {}
        if self.end_at <= self.start_at:
            errors["end_at"] = "The session must end after it starts."
        if self.event_id:
            event = self.event
            if self.start_at < event.start_at or self.end_at > event.end_at:
                errors["start_at"] = (
                    f"Sessions must fall inside the event, "
                    f"{timezone.localtime(event.start_at):%H:%M} to "
                    f"{timezone.localtime(event.end_at):%H:%M}."
                )
            overlapping = (
                Session.objects.filter(event=event, start_at__lt=self.end_at, end_at__gt=self.start_at)
                .exclude(pk=self.pk)
                .first()
            )
            if overlapping is not None:
                errors["start_at"] = f"Overlaps session {overlapping.position}, {overlapping.title}."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self._fill_defaults()
        super().save(*args, **kwargs)


class SessionPresenter(UUIDModel):
    """
    A person presenting a session, with the conflict-of-interest
    declaration that applied to them at that session.
    """

    session = models.ForeignKey(
        Session, on_delete=models.CASCADE, related_name="session_presenters"
    )
    person = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="presentations")
    position = models.PositiveSmallIntegerField(
        blank=True, help_text="Order the names print. Blank takes the next number."
    )
    coi_declaration = models.ForeignKey(
        COIDeclaration,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="session_presenters",
        help_text="Blank picks up the presenter's current declaration, if they have one.",
    )
    # The approved form is per activity; ours is a standing declaration. A
    # reused declaration is confirmed by the presenter for this session, and
    # when is recorded here. Never silently.
    coi_confirmed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the presenter confirmed the attached declaration is still accurate for this session.",
    )

    objects = PersonOwnedQuerySet.as_manager()

    class Meta:
        ordering = ["session", "position"]
        constraints = [
            models.UniqueConstraint(
                fields=["session", "person"], name="sessionpresenter_unique_person"
            ),
            models.UniqueConstraint(
                fields=["session", "position"],
                name="sessionpresenter_unique_position",
                # Checked at commit, so two rows can swap numbers in one save.
                deferrable=models.Deferrable.DEFERRED,
            ),
        ]

    def __str__(self):
        return f"{self.person} presenting {self.session}"

    def attach_current_declaration(self):
        """Fill coi_declaration from the presenter's current one. Never replaces a set value."""
        if self.coi_declaration_id is None:
            self.coi_declaration = COIDeclaration.objects.current_for(
                self.person, self.session.event.date
            )
        return self.coi_declaration

    def _fill_position(self):
        if self.position is None and self.session_id:
            self.position = next_position(
                SessionPresenter.objects.filter(session_id=self.session_id).exclude(pk=self.pk)
            )

    def clean(self):
        super().clean()
        self._fill_position()
        if (
            self.coi_declaration_id
            and self.person_id
            and self.coi_declaration.person_id not in identity_ids(self.person)
        ):
            raise ValidationError(
                {"coi_declaration": "That declaration was made by someone else."}
            )

    def save(self, *args, **kwargs):
        self._fill_position()
        self.attach_current_declaration()
        super().save(*args, **kwargs)


class LearningObjective(UUIDModel):
    """One objective of a session. The evaluation form asks one question per row."""

    session = models.ForeignKey(Session, on_delete=models.CASCADE, related_name="objectives")
    position = models.PositiveSmallIntegerField(blank=True, help_text="Blank takes the next number.")
    text = models.TextField()

    class Meta:
        ordering = ["session", "position"]
        constraints = [
            models.UniqueConstraint(
                fields=["session", "position"],
                name="learningobjective_unique_position",
                # Checked at commit, so two rows can swap numbers in one save.
                deferrable=models.Deferrable.DEFERRED,
            ),
        ]

    def __str__(self):
        return f"{self.position}. {self.text[:60]}"

    def _fill_position(self):
        if self.position is None and self.session_id:
            self.position = next_position(
                LearningObjective.objects.filter(session_id=self.session_id).exclude(pk=self.pk)
            )

    def clean(self):
        super().clean()
        self._fill_position()

    def save(self, *args, **kwargs):
        self._fill_position()
        super().save(*args, **kwargs)
