import hashlib
from pathlib import Path

from django import forms
from django.contrib import admin, messages
from django.utils import timezone
from django.utils.html import format_html, format_html_join

from core.admin import BaseAdmin, NoDeleteMixin, SafeModelForm, admin_link

from . import teams
from .models import AttendanceRecord, AttendanceSupersession, AttendanceUpload
from .services import (
    ALLOWED_UPLOAD_EXTENSIONS,
    MAX_UPLOAD_BYTES,
    check_export_against_event,
    log_manual_row,
    match_event,
    match_record,
    supersede_rows,
    upload_teams_export,
)

Source = AttendanceRecord.Source


# --- Uploads -----------------------------------------------------------------


class UploadForm(SafeModelForm):
    file = forms.FileField(
        help_text="The attendance export exactly as downloaded from Teams. It is stored "
        "untouched, then its rows are read in."
    )

    class Meta:
        model = AttendanceUpload
        fields = ["event"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["event"].required = False
        self.fields["event"].help_text = (
            "Leave blank to find the event by its Teams meeting title and the date in the "
            "file. Either way the file is checked against the event and refused if the "
            "date, title or hours don't match."
        )

    def clean(self):
        cleaned = super().clean()
        uploaded = cleaned.get("file")
        if uploaded is None or self.errors:
            return cleaned
        try:
            export = teams.parse_export(b"".join(uploaded.chunks()))
            event = cleaned.get("event") or match_event(export)
            check_export_against_event(export, event)
        except teams.ExportError as error:
            raise forms.ValidationError(str(error))
        finally:
            uploaded.seek(0)
        cleaned["event"] = event
        self.instance.event = event
        return cleaned

    def clean_file(self):
        uploaded = self.cleaned_data["file"]
        if Path(uploaded.name).suffix.lower() not in ALLOWED_UPLOAD_EXTENSIONS:
            raise forms.ValidationError("Teams exports are .csv or .xlsx files.")
        if uploaded.size > MAX_UPLOAD_BYTES:
            raise forms.ValidationError("That file is too large to be a Teams attendance export.")
        digest = hashlib.sha256()
        for chunk in uploaded.chunks():
            digest.update(chunk)
        existing = AttendanceUpload.objects.filter(sha256=digest.hexdigest()).first()
        if existing is not None:
            raise forms.ValidationError(
                f"This exact file was already uploaded for {existing.event} "
                f"on {timezone.localtime(existing.uploaded_at):%Y-%m-%d}."
            )
        return uploaded


@admin.register(AttendanceUpload)
class AttendanceUploadAdmin(NoDeleteMixin, BaseAdmin):
    """Upload a Teams export. Once stored, nothing about it can be edited."""

    list_display = [
        "original_filename",
        "event",
        "uploaded_at",
        "uploaded_by",
        "row_count",
        "parsed",
        "short_hash",
    ]
    list_filter = ["event"]
    date_hierarchy = "uploaded_at"
    search_fields = ["original_filename", "sha256"]
    list_select_related = ["event", "uploaded_by"]

    def get_form(self, request, obj=None, **kwargs):
        if obj is None:
            kwargs["form"] = UploadForm
        return super().get_form(request, obj, **kwargs)

    def get_fields(self, request, obj=None):
        if obj is None:
            return ["event", "file"]
        return [
            "event",
            "original_filename",
            "stored_path",
            "sha256",
            "uploaded_by",
            "uploaded_at",
            "parsed_at",
            "parser_version",
            "row_count",
            "warnings_display",
        ]

    def get_readonly_fields(self, request, obj=None):
        return [] if obj is None else self.get_fields(request, obj)

    def save_model(self, request, obj, form, change):
        if change:
            return
        stored, export = upload_teams_export(
            form.cleaned_data["file"], event=obj.event, user=request.user, request=request
        )
        # The admin goes on to use `obj` for its redirect and message.
        obj.__dict__.update(stored.__dict__)
        unmatched = stored.records.filter(person__isnull=True).count()
        self.message_user(
            request,
            f"Read {stored.row_count} rows for {stored.event}. {unmatched} could not be matched "
            "by email and wait in the review queue (Attendance records, filter Unmatched).",
            messages.SUCCESS,
        )
        for warning in export.warnings:
            self.message_user(request, f"Check: {warning}", messages.WARNING)

    @admin.display(description="Parser warnings")
    def warnings_display(self, obj):
        if not obj.parse_warnings:
            return "None"
        return format_html_join("", "<p>{}</p>", ((w,) for w in obj.parse_warnings))

    @admin.display(description="Parsed", boolean=True)
    def parsed(self, obj):
        return obj.parsed_at is not None

    @admin.display(description="SHA-256")
    def short_hash(self, obj):
        return obj.sha256[:12]


# --- Records -----------------------------------------------------------------


class MatchedFilter(admin.SimpleListFilter):
    title = "matched to a person"
    parameter_name = "matched"

    def lookups(self, request, model_admin):
        return [("no", "Unmatched (review queue)"), ("yes", "Matched")]

    def queryset(self, request, queryset):
        if self.value() == "no":
            return queryset.filter(person__isnull=True)
        if self.value() == "yes":
            return queryset.filter(person__isnull=False)
        return queryset


class ActiveFilter(admin.SimpleListFilter):
    title = "counts toward credit"
    parameter_name = "active"

    def lookups(self, request, model_admin):
        return [("yes", "Active"), ("no", "Superseded by a correction")]

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(supersession__isnull=True)
        if self.value() == "no":
            return queryset.filter(supersession__isnull=False)
        return queryset


class ManualAttendanceForm(SafeModelForm):
    """Adding a row by hand. Teams rows only ever come from an upload."""

    duration_minutes = forms.IntegerField(
        required=False,
        min_value=0,
        label="Minutes",
        help_text="Only for a row without join and leave times. Don't invent times: "
        "a reconstructed record should not look like a captured one.",
    )

    class Meta:
        model = AttendanceRecord
        fields = [
            "event",
            "source",
            "person",
            "attributed_to",
            "join_at",
            "leave_at",
            "session",
            "reason",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["source"].choices = [
            (Source.MANUAL, "Manual: a correction, or someone Teams never saw"),
            (Source.ROOM_ROSTER, "Room roster: sat in a room behind someone else's device"),
        ]
        self.fields["attributed_to"].help_text = (
            "Room roster only: the row for the device they sat at. Leave the times blank "
            "to copy that row's times."
        )
        self.fields["session"].help_text = (
            "Only with minutes (no times): which session the minutes are for."
        )

    def clean(self):
        cleaned = super().clean()
        minutes = cleaned.get("duration_minutes")
        timed = cleaned.get("join_at") or cleaned.get("leave_at")
        if minutes is not None and timed:
            self.add_error("duration_minutes", "Give either minutes or times, not both.")
        elif minutes is not None:
            self.instance.duration_seconds = minutes * 60
        elif not timed and cleaned.get("source") != Source.ROOM_ROSTER:
            self.add_error("duration_minutes", "Give the minutes, or join and leave times.")
        return cleaned


class ReplacesInline(admin.TabularInline):
    """On a correcting row: the rows it replaces."""

    model = AttendanceSupersession
    form = SafeModelForm
    fk_name = "new"
    extra = 0
    fields = ["old"]
    autocomplete_fields = ["old"]
    verbose_name = "row this one replaces"
    verbose_name_plural = "Rows this one replaces (they stop counting; they are not changed)"

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AttendanceRecord)
class AttendanceRecordAdmin(NoDeleteMixin, BaseAdmin):
    """
    The attendance review screen.

    Filter to "Unmatched", pick the person on each row and save: the
    person's other rows with the same address are matched with it. What
    Teams recorded is read-only; only who it was can be changed.
    """

    list_display = [
        "who",
        "event",
        "person",
        "source",
        "joined",
        "left",
        "minutes",
        "match_method",
        "status",
    ]
    list_display_links = ["who"]
    list_editable = ["person"]
    list_filter = [MatchedFilter, ActiveFilter, "source", "match_method", "event"]
    date_hierarchy = "event__date"
    search_fields = [
        "raw_display_name",
        "raw_email",
        "person__family_name",
        "person__given_name",
        "person__emails__email",
    ]
    ordering = ["-event__date", "raw_display_name", "join_at"]
    list_select_related = ["event", "person", "supersession", "attributed_to__supersession"]
    autocomplete_fields = ["person", "attributed_to"]
    inlines = [ReplacesInline]

    OBSERVED = [
        "source",
        "event",
        "session",
        "upload",
        "parser_version",
        "raw_display_name",
        "raw_email",
        "raw_participant_role",
        "join_at",
        "leave_at",
        "duration_display",
        "attributed_to",
        "reason",
        "created_by",
        "created_at",
    ]
    INTERPRETED_READONLY = ["match_method", "matched_at", "matched_by", "status"]

    def get_form(self, request, obj=None, **kwargs):
        if obj is None:
            kwargs["form"] = ManualAttendanceForm
        return super().get_form(request, obj, **kwargs)

    def get_fieldsets(self, request, obj=None):
        if obj is None:
            return [
                (
                    None,
                    {
                        "fields": [
                            "event",
                            "source",
                            "person",
                            "attributed_to",
                            "join_at",
                            "leave_at",
                            "duration_minutes",
                            "session",
                            "reason",
                        ]
                    },
                )
            ]
        return [
            ("Who this was (can be revised)", {"fields": ["person", *self.INTERPRETED_READONLY]}),
            ("What was recorded (never changes)", {"fields": self.OBSERVED}),
        ]

    def get_readonly_fields(self, request, obj=None):
        return [] if obj is None else [*self.OBSERVED, *self.INTERPRETED_READONLY]

    def get_search_results(self, request, queryset, search_term):
        queryset, distinct = super().get_search_results(request, queryset, search_term)
        # The "row this one replaces" picker should only offer live rows.
        if request.path.endswith("/autocomplete/"):
            queryset = queryset.filter(supersession__isnull=True)
        return queryset, distinct

    # --- Saving goes through the services so everything is audit-logged ---

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
            if obj.person_id:
                obj.matched_at = timezone.now()
                obj.matched_by = request.user
            obj.save()
            log_manual_row(obj, user=request.user, request=request)
            return
        if "person" not in form.changed_data:
            return
        stored = AttendanceRecord.objects.get(pk=obj.pk)
        result = match_record(stored, obj.person, user=request.user, request=request)
        if result.email_added:
            self.message_user(
                request,
                f"Remembered {result.email_added} for {result.row.person}: "
                "future uploads will match it automatically.",
                messages.SUCCESS,
            )
        if result.also_matched:
            self.message_user(
                request,
                f"{result.also_matched} other row(s) with the same address were matched "
                f"to {result.row.person} as well.",
                messages.SUCCESS,
            )

    def save_formset(self, request, form, formset, change):
        if formset.model is not AttendanceSupersession:
            return super().save_formset(request, form, formset, change)
        links = formset.save(commit=False)
        if links:
            supersede_rows(
                [link.old for link in links], form.instance, user=request.user, request=request
            )

    # --- Columns ---

    @admin.display(description="As recorded", ordering="raw_display_name")
    def who(self, obj):
        if obj.raw_display_name or obj.raw_email:
            return " / ".join(filter(None, [obj.raw_display_name, obj.raw_email]))
        return f"({obj.get_source_display().lower()} entry)"

    @admin.display(description="Joined", ordering="join_at")
    def joined(self, obj):
        return f"{timezone.localtime(obj.join_at):%H:%M:%S}" if obj.join_at else "-"

    @admin.display(description="Left", ordering="leave_at")
    def left(self, obj):
        return f"{timezone.localtime(obj.leave_at):%H:%M:%S}" if obj.leave_at else "-"

    @admin.display(description="Min", ordering="duration_seconds")
    def minutes(self, obj):
        return obj.duration_seconds // 60

    @admin.display(description="Recorded duration")
    def duration_display(self, obj):
        if obj.duration_seconds is None:
            return "-"
        return f"{obj.duration_seconds // 60} min {obj.duration_seconds % 60} s"

    @admin.display(description="Status")
    def status(self, obj):
        if not obj.pk:
            return "-"
        if obj.is_superseded:
            return format_html("Superseded by {}", admin_link(obj.supersession.new, "a correction"))
        if obj.attributed_to_id and obj.attributed_to.is_superseded:
            # The copied times may be the ones that were wrong.
            return format_html(
                "<strong>Check:</strong> the device row this copies, {}, was superseded. "
                "If its times were wrong, so are these.",
                admin_link(obj.attributed_to, "here"),
            )
        replaced = list(obj.supersedes.select_related("old"))
        if replaced:
            return format_html(
                "Active. Replaces {}",
                format_html_join(", ", "{}", ((admin_link(link.old, "a row"),) for link in replaced)),
            )
        return "Active"
