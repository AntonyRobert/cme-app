from django.contrib import admin, messages
from django.db.models import Count, Q
from django.utils import timezone
from django.utils.html import format_html, format_html_join

from audit.log import record
from core.admin import AppendOnlyAdmin, BaseAdmin, SafeModelForm, admin_link, changelist_url

from .models import COIDeclaration, LearningObjective, RoundsEvent, Session, SessionPresenter


class SessionInline(admin.TabularInline):
    model = Session
    form = SafeModelForm
    fields = ["position", "title", "duration_minutes", "submitted_at"]
    show_change_link = True

    def get_extra(self, request, obj=None, **kwargs):
        return 0 if obj else 3


@admin.register(RoundsEvent)
class RoundsEventAdmin(BaseAdmin):
    list_display = [
        "date",
        "title",
        "status",
        "accredited_credits",
        "session_count",
        "attendance_rows",
        "unmatched_rows",
    ]
    list_filter = ["status"]
    date_hierarchy = "date"
    search_fields = ["title", "teams_meeting_id", "sessions__title"]
    inlines = [SessionInline]
    readonly_fields = ["credit_window_display", "credit_summary"]
    fieldsets = [
        (None, {"fields": ["title", "date", "status", "accredited_credits"]}),
        ("Schedule", {"fields": ["start_at", "end_at"]}),
        (
            "What actually happened",
            {
                "fields": ["actual_start_at", "actual_end_at", "credit_window_display"],
                "description": "Leave blank if rounds ran to schedule. If it started late or "
                "ran over, set these once and everyone's credit follows.",
            },
        ),
        ("Teams", {"fields": ["teams_join_url", "teams_meeting_id"]}),
        ("Credit, as it stands", {"fields": ["credit_summary"]}),
    ]

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .annotate(
                _sessions=Count("sessions", distinct=True),
                _rows=Count(
                    "attendance_records",
                    filter=Q(attendance_records__supersession__isnull=True),
                    distinct=True,
                ),
                _unmatched=Count(
                    "attendance_records",
                    filter=Q(
                        attendance_records__supersession__isnull=True,
                        attendance_records__person__isnull=True,
                    ),
                    distinct=True,
                ),
            )
        )

    @admin.display(description="Sessions", ordering="_sessions")
    def session_count(self, obj):
        return obj._sessions

    @admin.display(description="Attendance rows", ordering="_rows")
    def attendance_rows(self, obj):
        from attendance.models import AttendanceRecord

        url = changelist_url(AttendanceRecord, event__id__exact=obj.pk)
        return format_html('<a href="{}">{}</a>', url, obj._rows)

    @admin.display(description="Unmatched", ordering="_unmatched")
    def unmatched_rows(self, obj):
        from attendance.models import AttendanceRecord

        if not obj._unmatched:
            return "0"
        url = changelist_url(AttendanceRecord, event__id__exact=obj.pk, matched="no")
        return format_html('<a href="{}"><strong>{} to review</strong></a>', url, obj._unmatched)

    @admin.display(description="Time that counts")
    def credit_window_display(self, obj):
        if not obj.pk:
            return "-"
        start, end = (timezone.localtime(value) for value in obj.credit_window())
        return f"{start:%Y-%m-%d %H:%M} to {end:%H:%M} (includes 5 minutes before the start)"

    @admin.display(description="Per person")
    def credit_summary(self, obj):
        """Computed live from attendance and evaluations. Nothing here is stored."""
        if not obj.pk:
            return "-"
        from credits.reports import event_credit_rows

        rows = event_credit_rows(obj)
        if not rows:
            return "No matched attendance or evaluations yet."
        body = format_html_join(
            "",
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td>"
            "<td><strong>{}</strong></td><td>{}</td></tr>",
            (
                (
                    admin_link(person),
                    b.time.minutes,
                    (b.time.source or "-").replace("_", " "),
                    "-" if b.time.self_reported_minutes is None else b.time.self_reported_minutes,
                    "Done" if b.gate_passed else "Missing",
                    f"{b.computed_credits} {b.adjustment_credits:+}"
                    if b.adjustment_credits
                    else b.computed_credits,
                    b.credits,
                    "Review: claims more than was recorded, or self-reported only" if b.time.needs_review else "",
                )
                for person, b in rows
            ),
        )
        return format_html(
            "<table><thead><tr><th>Person</th><th>Minutes</th><th>From</th>"
            "<th>Self-reported</th><th>Evaluation</th><th>Computed (+ adjustment)</th>"
            "<th>Credits</th><th></th></tr></thead><tbody>{}</tbody></table>",
            body,
        )


class SessionPresenterInline(admin.TabularInline):
    model = SessionPresenter
    form = SafeModelForm
    extra = 0
    fields = ["position", "person", "coi_declaration", "coi_status"]
    readonly_fields = ["coi_status"]
    autocomplete_fields = ["person"]
    raw_id_fields = ["coi_declaration"]

    @admin.display(description="Conflict of interest")
    def coi_status(self, obj):
        if not obj.pk:
            return "Picked up from the presenter's current declaration on save"
        if obj.coi_declaration_id is None:
            return format_html("<strong>{}</strong>", "No declaration on file")
        declaration = obj.coi_declaration
        answer = "Conflict declared" if declaration.has_conflict else "No conflict"
        return format_html(
            "{} on {}", admin_link(declaration, answer), timezone.localdate(declaration.declared_at)
        )


class LearningObjectiveInline(admin.TabularInline):
    model = LearningObjective
    form = SafeModelForm
    extra = 0
    fields = ["position", "text"]


class SubmittedFilter(admin.SimpleListFilter):
    title = "presenter details"
    parameter_name = "submitted"

    def lookups(self, request, model_admin):
        return [("no", "Not submitted yet (chase list)"), ("yes", "Submitted")]

    def queryset(self, request, queryset):
        if self.value() == "no":
            return queryset.filter(submitted_at__isnull=True)
        if self.value() == "yes":
            return queryset.filter(submitted_at__isnull=False)
        return queryset


class MissingCOIFilter(admin.SimpleListFilter):
    title = "conflict of interest"
    parameter_name = "coi"

    def lookups(self, request, model_admin):
        return [("missing", "A presenter has no declaration"), ("none", "No presenter set")]

    def queryset(self, request, queryset):
        if self.value() == "missing":
            return queryset.filter(session_presenters__coi_declaration__isnull=True).distinct()
        if self.value() == "none":
            return queryset.filter(session_presenters__isnull=True)
        return queryset


@admin.register(Session)
class SessionAdmin(BaseAdmin):
    list_display = ["event", "position", "title", "presenter_names", "duration_minutes", "submitted"]
    list_display_links = ["title"]
    list_filter = [SubmittedFilter, MissingCOIFilter, "event__status"]
    date_hierarchy = "event__date"
    search_fields = [
        "title",
        "session_presenters__person__family_name",
        "session_presenters__person__given_name",
    ]
    list_select_related = ["event"]
    inlines = [SessionPresenterInline, LearningObjectiveInline]
    actions = ["attach_declarations"]
    fieldsets = [
        (None, {"fields": ["event", "position", "title", "duration_minutes", "submitted_at"]}),
        (
            "Blurb",
            {
                "fields": ["draft_blurb", "published_blurb"],
                "description": "Only the published blurb is ever shown to attendees.",
            },
        ),
    ]

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("session_presenters__person")

    @admin.display(description="Presenters")
    def presenter_names(self, obj):
        return ", ".join(link.person.full_name for link in obj.session_presenters.all()) or "-"

    @admin.display(description="Submitted", boolean=True, ordering="submitted_at")
    def submitted(self, obj):
        return obj.submitted_at is not None

    @admin.action(description="Attach presenters' current COI declarations")
    def attach_declarations(self, request, queryset):
        attached = 0
        for link in SessionPresenter.objects.filter(
            session__in=queryset, coi_declaration__isnull=True
        ).select_related("person", "session__event"):
            if link.attach_current_declaration() is not None:
                link.save()
                attached += 1
        self.message_user(request, f"Attached {attached} declaration(s).", messages.SUCCESS)


class ValidityFilter(admin.SimpleListFilter):
    title = "validity"
    parameter_name = "valid"

    def lookups(self, request, model_admin):
        return [("yes", "Still valid"), ("no", "Expired")]

    def queryset(self, request, queryset):
        today = timezone.localdate()
        if self.value() == "yes":
            return queryset.filter(valid_until__gte=today)
        if self.value() == "no":
            return queryset.filter(valid_until__lt=today)
        return queryset


@admin.register(COIDeclaration)
class COIDeclarationAdmin(AppendOnlyAdmin):
    """Add-only. A change of circumstances is a new declaration."""

    list_display = ["person", "has_conflict", "declared_at", "valid_until", "disclosure_text_version"]
    list_filter = ["has_conflict", ValidityFilter]
    search_fields = ["person__family_name", "person__given_name", "details"]
    autocomplete_fields = ["person"]
    fields = [
        "person",
        "has_conflict",
        "details",
        "declared_at",
        "valid_until",
        "disclosure_text_version",
    ]

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        record(
            "coi.declared",
            obj,
            request=request,
            metadata={
                "person": str(obj.person_id),
                "has_conflict": obj.has_conflict,
                "entered_by_staff": True,
            },
        )
