import datetime
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from core.constraints import is_quarter_multiple, validate_quarter_multiple
from core.models import AppendOnlyMixin, ImmutableRowError, UUIDModel
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
    return settings.SERIES_NAME


def next_position(queryset):
    """The number after the highest position already used among `queryset`."""
    highest = queryset.aggregate(models.Max("position"))["position__max"]
    return (highest or 0) + 1


def end_of_academic_year(today=None):
    """The next 30 June, the default expiry of a conflict-of-interest declaration."""
    today = today or timezone.localdate()
    year = today.year if (today.month, today.day) <= (6, 30) else today.year + 1
    return datetime.date(year, 6, 30)


class RoundsEvent(UUIDModel):
    """One fortnightly rounds: a block of sessions people attend as a whole."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PUBLISHED = "published", "Published"
        HELD = "held", "Held"
        CLOSED = "closed", "Closed"

    title = models.CharField(
        max_length=200, default=default_event_title, help_text="Prints on certificate lines."
    )
    date = models.DateField(blank=True, help_text="Blank means the day it starts.")
    start_at = models.DateTimeField(help_text="Every session must fall between these two.")
    end_at = models.DateTimeField(blank=True, help_text="Blank means three hours after the start.")
    teams_join_url = models.URLField(max_length=2000, blank=True)
    teams_meeting_id = models.CharField(max_length=200, null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    accredited_credits = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0")), validate_quarter_multiple],
        help_text="Credits available for the whole event, in steps of 0.25.",
    )

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

    def clean(self):
        super().clean()
        self._fill_defaults()
        if self.start_at and self.end_at and self.end_at <= self.start_at:
            raise ValidationError({"end_at": "The event must end after it starts."})
        if self._stored_status() == self.Status.CLOSED and not self.is_closed:
            raise ValidationError(
                {"status": "A closed event stays closed. Its totals are frozen."}
            )

    def save(self, *args, **kwargs):
        if self._stored_status() == self.Status.CLOSED and not self.is_closed:
            raise ImmutableRowError("A closed event cannot be reopened.")
        self._fill_defaults()
        super().save(*args, **kwargs)


class COIDeclarationQuerySet(PersonOwnedQuerySet):
    def current_for(self, person, on_date):
        """The person's most recent declaration still valid on on_date, or None."""
        return (
            self.for_person(person)
            .filter(valid_until__gte=on_date)
            .order_by("-declared_at")
            .first()
        )


class COIDeclaration(AppendOnlyMixin, UUIDModel):
    """
    A conflict-of-interest declaration, as made.

    Never edited: a change of circumstances is a new declaration. That is
    what lets SessionPresenter point at one as a snapshot.
    """

    person = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="coi_declarations")
    has_conflict = models.BooleanField()
    details = models.TextField(blank=True, help_text="Required when there is a conflict.")
    declared_at = models.DateTimeField(default=timezone.now)
    valid_until = models.DateField(default=end_of_academic_year)
    disclosure_text_version = models.CharField(
        max_length=50, help_text="Which version of the statement was agreed to."
    )

    objects = COIDeclarationQuerySet.as_manager()

    class Meta:
        verbose_name = "COI declaration"
        ordering = ["-declared_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(has_conflict=False) | ~Q(details=""),
                name="coideclaration_conflict_needs_details",
            ),
        ]

    def __str__(self):
        answer = "conflict declared" if self.has_conflict else "no conflict"
        return f"{self.person}: {answer} ({timezone.localdate(self.declared_at)})"

    def clean(self):
        super().clean()
        if self.has_conflict and not (self.details or "").strip():
            raise ValidationError({"details": "Describe the conflict."})


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
    draft_blurb = models.TextField(blank=True)
    published_blurb = models.TextField(blank=True)
    submitted_at = models.DateTimeField(
        null=True, blank=True, help_text="Blank means the presenters haven't filled it in yet."
    )

    class Meta:
        ordering = ["event", "start_at", "position"]
        constraints = [
            models.UniqueConstraint(fields=["event", "position"], name="session_unique_position"),
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

    objects = PersonOwnedQuerySet.as_manager()

    class Meta:
        ordering = ["session", "position"]
        constraints = [
            models.UniqueConstraint(
                fields=["session", "person"], name="sessionpresenter_unique_person"
            ),
            models.UniqueConstraint(
                fields=["session", "position"], name="sessionpresenter_unique_position"
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
                fields=["session", "position"], name="learningobjective_unique_position"
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
