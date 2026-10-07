from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from core.models import AppendOnlyMixin, FrozenFieldsMixin, UUIDModel
from people.models import Person
from people.ownership import PersonOwnedQuerySet
from rounds.models import RoundsEvent, Session


class AttendanceUpload(FrozenFieldsMixin, UUIDModel):
    """
    A Teams attendance export, stored untouched.

    The file is the evidence behind every row parsed from it. It is never
    edited: if a parse was wrong, fix the parser and re-parse.
    """

    FROZEN_FIELDS = (
        "event",
        "original_filename",
        "stored_path",
        "sha256",
        "uploaded_by",
        "uploaded_at",
    )

    event = models.ForeignKey(RoundsEvent, on_delete=models.PROTECT, related_name="uploads")
    original_filename = models.CharField(max_length=255)
    stored_path = models.CharField(
        max_length=500, help_text="Relative to UPLOAD_ROOT. Back this up with the database."
    )
    sha256 = models.CharField(max_length=64, unique=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)
    parsed_at = models.DateTimeField(null=True, blank=True)
    parser_version = models.CharField(max_length=50, null=True, blank=True)
    row_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self):
        return f"{self.original_filename} ({self.event.date})"


class AttendanceRecordQuerySet(PersonOwnedQuerySet):
    def active(self):
        """Rows that count: everything no correction has replaced."""
        return self.filter(supersession__isnull=True)

    def superseded(self):
        return self.filter(supersession__isnull=False)

    def unmatched(self):
        """The review queue: rows nobody has been identified for yet."""
        return self.filter(person__isnull=True)


class AttendanceRecord(FrozenFieldsMixin, UUIDModel):
    """
    One observation of someone being present.

    Teams writes one row per join, not per person, so a single attendee is
    usually several rows. Never read attended time off these rows directly:
    use attendance.aggregation.attended_minutes().

    The fields in FROZEN_FIELDS are what was observed and never change. The
    rest (person, match_method, matched_at, matched_by) are our judgment of
    who it was and may be revised, with an audit entry. A wrong observation
    is corrected by adding a row and an AttendanceSupersession, not by
    editing.
    """

    class Source(models.TextChoices):
        TEAMS_UPLOAD = "teams_upload", "Teams upload"
        MANUAL = "manual", "Manual"
        ROOM_ROSTER = "room_roster", "Room roster"

    class MatchMethod(models.TextChoices):
        EMAIL_EXACT = "email_exact", "Email (exact)"
        EMAIL_ALIAS = "email_alias", "Email (alias)"
        MANUAL = "manual", "Manual"
        UNMATCHED = "unmatched", "Unmatched"

    FROZEN_FIELDS = (
        "source",
        "upload",
        "parser_version",
        "event",
        "session",
        "raw_display_name",
        "raw_email",
        "raw_participant_role",
        "join_at",
        "leave_at",
        "duration_seconds",
        "attributed_to",
        "reason",
        "created_by",
        "created_at",
    )

    # --- Observation: never changes after insert ---
    source = models.CharField(max_length=20, choices=Source.choices)
    upload = models.ForeignKey(
        AttendanceUpload,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="records",
    )
    parser_version = models.CharField(max_length=50, null=True, blank=True)
    event = models.ForeignKey(
        RoundsEvent, on_delete=models.PROTECT, related_name="attendance_records"
    )
    session = models.ForeignKey(
        Session,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="attendance_records",
        help_text="Only for a row without join and leave times: which session the "
        "minutes belong to. Timed rows are matched to sessions by their times.",
    )
    raw_display_name = models.CharField(max_length=300, null=True, blank=True)
    # Exactly as Teams wrote it, so not lowercased. Matching lowercases.
    raw_email = models.CharField(max_length=320, null=True, blank=True)
    raw_participant_role = models.CharField(max_length=50, null=True, blank=True)
    join_at = models.DateTimeField(null=True, blank=True)
    leave_at = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.PositiveIntegerField(
        blank=True,
        help_text="As recorded. For rows with join and leave times the credit "
        "calculation uses the times, not this.",
    )
    attributed_to = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="room_roster_rows",
        help_text="Room roster only: the row for the device this person sat at.",
    )
    reason = models.TextField(
        null=True, blank=True, help_text="Required on every row that didn't come from Teams."
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    # --- Interpretation: may be revised, always audit-logged ---
    person = models.ForeignKey(
        Person,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="attendance_records",
    )
    match_method = models.CharField(
        max_length=20, choices=MatchMethod.choices, default=MatchMethod.UNMATCHED
    )
    matched_at = models.DateTimeField(null=True, blank=True)
    matched_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )

    objects = AttendanceRecordQuerySet.as_manager()

    class Meta:
        ordering = ["event", "join_at", "created_at"]
        indexes = [models.Index(fields=["event", "person"])]
        constraints = [
            models.CheckConstraint(
                condition=Q(join_at__isnull=True, leave_at__isnull=True)
                | Q(join_at__isnull=False, leave_at__isnull=False),
                name="attendancerecord_times_both_or_neither",
            ),
            models.CheckConstraint(
                condition=Q(join_at__isnull=True) | Q(leave_at__gte=F("join_at")),
                name="attendancerecord_leave_after_join",
            ),
            # Minutes without times have to belong to a session, or they
            # could not be credited to one.
            models.CheckConstraint(
                condition=Q(join_at__isnull=False) | Q(session__isnull=False),
                name="attendancerecord_hours_only_needs_session",
            ),
            models.CheckConstraint(
                condition=Q(source="teams_upload")
                | (Q(reason__isnull=False) & ~Q(reason="")),
                name="attendancerecord_non_teams_needs_reason",
            ),
            models.CheckConstraint(
                condition=Q(source="teams_upload", upload__isnull=False)
                | (~Q(source="teams_upload") & Q(upload__isnull=True)),
                name="attendancerecord_upload_iff_teams",
            ),
            models.CheckConstraint(
                condition=Q(source="room_roster", attributed_to__isnull=False)
                | (~Q(source="room_roster") & Q(attributed_to__isnull=True)),
                name="attendancerecord_attributed_iff_room_roster",
            ),
            models.CheckConstraint(
                condition=Q(attributed_to__isnull=True) | ~Q(attributed_to=F("id")),
                name="attendancerecord_not_attributed_to_self",
            ),
            models.CheckConstraint(
                condition=Q(person__isnull=True, match_method="unmatched")
                | (Q(person__isnull=False) & ~Q(match_method="unmatched")),
                name="attendancerecord_match_method_agrees_with_person",
            ),
        ]

    def __str__(self):
        who = self.person or self.raw_display_name or self.raw_email or "unknown"
        if self.join_at and self.leave_at:
            join, leave = timezone.localtime(self.join_at), timezone.localtime(self.leave_at)
            when = f"{join:%Y-%m-%d %H:%M}-{leave:%H:%M}"
        else:
            when = f"{self.event.date}, {self.duration_seconds // 60} min"
        return f"{who} ({when}, {self.get_source_display()})"

    @property
    def is_superseded(self):
        return hasattr(self, "supersession")

    def _fill_defaults(self):
        """Derive what can be derived, so forms and code only supply the facts."""
        if self.source == self.Source.ROOM_ROSTER and self.attributed_to_id:
            # Copied, not invented: the device really was in the meeting for
            # this window. Explicit times win, for someone who walked in late.
            if self.join_at is None and self.leave_at is None:
                self.join_at = self.attributed_to.join_at
                self.leave_at = self.attributed_to.leave_at
            # A device row with no times of its own: copy its duration instead.
            if self.join_at is None and self.duration_seconds is None:
                self.duration_seconds = self.attributed_to.duration_seconds
        if self.duration_seconds is None and self.join_at and self.leave_at:
            self.duration_seconds = max(int((self.leave_at - self.join_at).total_seconds()), 0)
        if self.person_id is None:
            self.match_method = self.MatchMethod.UNMATCHED
        elif self.match_method == self.MatchMethod.UNMATCHED:
            self.match_method = self.MatchMethod.MANUAL

    def clean(self):
        super().clean()
        self._fill_defaults()
        errors = {}
        if self.source != self.Source.TEAMS_UPLOAD and not (self.reason or "").strip():
            errors["reason"] = "Say why this row is being added by hand."
        if self.source == self.Source.ROOM_ROSTER:
            if not self.attributed_to_id:
                errors["attributed_to"] = "A room roster row points at the device's row."
            elif self.event_id and self.attributed_to.event_id != self.event_id:
                errors["attributed_to"] = "That row belongs to a different event."
        elif self.attributed_to_id:
            errors["attributed_to"] = "Only room roster rows are attributed to another row."
        if (self.join_at is None) != (self.leave_at is None):
            errors["leave_at"] = "Give both join and leave times, or neither."
        elif self.join_at and self.leave_at < self.join_at:
            errors["leave_at"] = "Leave time is before join time."
        if self.join_at is None and self.leave_at is None:
            if self.session_id is None:
                errors["session"] = "Minutes without times must say which session they are for."
            elif self.event_id and self.session.event_id != self.event_id:
                errors["session"] = "That session belongs to a different event."
        elif self.session_id is not None:
            errors["session"] = "Timed rows are matched to sessions by their times; leave this blank."
        if self.duration_seconds is None:
            errors["duration_seconds"] = "Give a duration, or join and leave times."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self._fill_defaults()
        super().save(*args, **kwargs)


class AttendanceSupersession(AppendOnlyMixin, UUIDModel):
    """
    "Row `old` was replaced by row `new`."

    A correction is a new observation, so the replaced row is never touched;
    the link lives here. One correction may replace several rows (four
    rejoin rows become one). `old` is unique: if a row could be superseded
    twice, both corrections would count and the credit would be inflated.
    """

    old = models.OneToOneField(
        AttendanceRecord, on_delete=models.PROTECT, related_name="supersession"
    )
    new = models.ForeignKey(
        AttendanceRecord, on_delete=models.PROTECT, related_name="supersedes"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=~Q(old=F("new")), name="attendancesupersession_old_is_not_new"
            ),
        ]

    def __str__(self):
        return f"{self.old_id} replaced by {self.new_id}"

    def clean(self):
        super().clean()
        if not (self.old_id and self.new_id):
            return
        if self.old_id == self.new_id:
            raise ValidationError("A row cannot replace itself.")
        if self.old.event_id != self.new.event_id:
            raise ValidationError("Both rows must belong to the same event.")
        # Also what prevents loops: the replacement has to be a live row.
        if AttendanceSupersession.objects.filter(old_id=self.new_id).exists():
            raise ValidationError(
                {"new": "The replacement row has itself been superseded. Point at the live row."}
            )

    def save(self, *args, **kwargs):
        if self._state.adding:
            self.clean()
        super().save(*args, **kwargs)
