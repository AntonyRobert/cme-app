from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from core.authz import ProgramScopedQuerySet
from core.models import AppendOnlyMixin, FrozenFieldsMixin, UUIDModel
from people.models import Person
from people.ownership import PersonOwnedQuerySet
from rounds.models import RoundsEvent, Session


class AttendanceUploadQuerySet(ProgramScopedQuerySet):
    program_lookup = "event__program"


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
    parse_warnings = models.JSONField(
        default=list,
        blank=True,
        help_text="What the parser noticed but did not stop for, such as Teams' own totals "
        "disagreeing with its rows.",
    )

    objects = AttendanceUploadQuerySet.as_manager()

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self):
        return f"{self.original_filename} ({self.event.date})"


class AttendanceRecordQuerySet(PersonOwnedQuerySet, ProgramScopedQuerySet):
    program_lookup = "event__program"

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
        SIGNIN_SHEET = "signin_sheet", "Sign-in sheet"
        QR_SIGNIN = "qr_signin", "QR sign-in"
        MANUAL = "manual", "Manual"
        ROOM_ROSTER = "room_roster", "Room roster"

    # Sources that come from a stored file.
    UPLOADED_SOURCES = {"teams_upload"}
    # Sources entered by staff one row at a time, which need a reason. A sign-in
    # sheet is also typed in by staff, but from the paper sheet, in one sitting:
    # the sheet is the reason, so no free text per row.
    HAND_ENTERED_SOURCES = {"manual", "room_roster"}
    # Sources whose one row claims the whole session (presence, not duration).
    WHOLE_SESSION_SOURCES = {"signin_sheet", "qr_signin"}

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
    # A Teams MEETING PERMISSION, not a statement about who presented:
    # everyone is given "Presenter" so they can share a screen. Stored as
    # observed; nothing may read it to decide presenter identity or credit.
    raw_participant_role = models.CharField(
        "Teams meeting role",
        max_length=50,
        null=True,
        blank=True,
        help_text="A Teams meeting permission, as written in the export. It says nothing "
        "about who presented and has no effect on credit. Presenters are the session's "
        "presenters.",
    )
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
                condition=~Q(source__in=["manual", "room_roster"])
                | (Q(reason__isnull=False) & ~Q(reason="")),
                name="attendancerecord_non_teams_needs_reason",
            ),
            models.CheckConstraint(
                condition=Q(source="teams_upload", upload__isnull=False)
                | (~Q(source="teams_upload") & Q(upload__isnull=True)),
                name="attendancerecord_upload_iff_teams",
            ),
            # A tick or a scan claims a whole session, so it names one and has no times.
            models.CheckConstraint(
                condition=~Q(source__in=["signin_sheet", "qr_signin"])
                | Q(session__isnull=False, join_at__isnull=True),
                name="attendancerecord_whole_session_sources_name_a_session",
            ),
            # One scan per person per session; a second scan is ignored, never a second row.
            models.UniqueConstraint(
                fields=["person", "session"],
                condition=Q(source="qr_signin"),
                name="attendancerecord_one_qr_scan_per_session",
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

    @property
    def is_whole_session_claim(self):
        return self.source in self.WHOLE_SESSION_SOURCES

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
        if (
            self.source in self.WHOLE_SESSION_SOURCES
            and self.duration_seconds is None
            and self.session_id
        ):
            # Presence, not duration: the claim is the whole session.
            self.duration_seconds = self.session.length_seconds
        if self.person_id is None:
            self.match_method = self.MatchMethod.UNMATCHED
        elif self.match_method == self.MatchMethod.UNMATCHED:
            self.match_method = self.MatchMethod.MANUAL

    def clean(self):
        super().clean()
        self._fill_defaults()
        errors = {}
        if self.source in self.HAND_ENTERED_SOURCES and not (self.reason or "").strip():
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


class SessionAttendanceDecisionQuerySet(PersonOwnedQuerySet, ProgramScopedQuerySet):
    program_lookup = "session__event__program"

    def current(self):
        """Decisions nothing supersedes: one per person and session at most."""
        return self.filter(superseded_by__isnull=True)


class SessionAttendanceDecision(AppendOnlyMixin, UUIDModel):
    """
    One staff member's sign-off of one person's minutes for one session.

    Credit on a certificate counts only confirmed minutes. Append-only: a
    correction is a new decision that supersedes this one and needs its
    own sign-off. Nothing is edited.
    """

    class Basis(models.TextChoices):
        SOURCES_AGREE = "sources_agree", "Sources agree"
        HIGHEST_CLAIM = "highest_claim", "Highest claim, chosen by a person"
        MANUAL = "manual", "Set by hand"

    person = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="attendance_decisions")
    session = models.ForeignKey(Session, on_delete=models.PROTECT, related_name="attendance_decisions")
    confirmed_minutes = models.PositiveIntegerField(help_text="What counts. Zero is a valid decision.")
    proposed_minutes = models.PositiveIntegerField(help_text="What the system proposed at the time.")
    basis = models.CharField(max_length=20, choices=Basis.choices)
    based_on = models.JSONField(
        default=dict, blank=True, help_text="Each source's claim and the row ids, at sign-off."
    )
    comment = models.TextField(
        blank=True, help_text="Required when the confirmed minutes differ from the proposal."
    )
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    confirmed_at = models.DateTimeField(auto_now_add=True)
    supersedes = models.OneToOneField(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="superseded_by",
        help_text="The decision this one corrects.",
    )

    objects = SessionAttendanceDecisionQuerySet.as_manager()

    class Meta:
        ordering = ["session", "person", "-confirmed_at"]
        indexes = [models.Index(fields=["session", "person"])]
        permissions = [
            # The gate between recorded attendance and credit: program admins only.
            ("sign_off_attendance", "Can sign off attendance"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(confirmed_minutes=F("proposed_minutes")) | ~Q(comment=""),
                name="sessionattendancedecision_change_needs_comment",
            ),
            models.CheckConstraint(
                condition=Q(supersedes__isnull=True) | ~Q(supersedes=F("id")),
                name="sessionattendancedecision_not_own_correction",
            ),
        ]

    def __str__(self):
        return f"{self.person}: {self.confirmed_minutes} min of {self.session} ({self.get_basis_display()})"

    @property
    def is_current(self):
        """Nothing saved supersedes this decision. A query, not the reverse cache:
        building a correction in memory already populates that cache."""
        return (
            self.pk is not None
            and not SessionAttendanceDecision.objects.filter(supersedes_id=self.pk).exists()
        )

    def clean(self):
        super().clean()
        if self.confirmed_minutes != self.proposed_minutes and not (self.comment or "").strip():
            raise ValidationError({"comment": "Say why the confirmed minutes differ from the proposal."})
        if self.session_id and self.confirmed_minutes > self.session.length_minutes:
            raise ValidationError(
                {"confirmed_minutes": f"The session lasted {self.session.length_minutes} minutes."}
            )
        if self.supersedes_id:
            old = self.supersedes
            if (old.person_id, old.session_id) != (self.person_id, self.session_id):
                raise ValidationError({"supersedes": "A correction replaces a decision for the same person and session."})
            if not old.is_current:
                raise ValidationError({"supersedes": "That decision has already been corrected."})
