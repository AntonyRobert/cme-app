from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.db.models import Count, Q
from django.utils import timezone
from django.utils.html import format_html, format_html_join

from audit.log import record
from core.admin import (
    AppendOnlyAdmin,
    AutoPositionFormSet,
    BaseAdmin,
    PositionedRowForm,
    ProgramScopedAdminMixin,
    SafeModelForm,
    admin_link,
    changelist_url,
)
from core.authz import staff_programs, writable_programs

from .coi import declare
from .models import (
    DEFAULT_SESSION_LENGTH,
    COIDeclaration,
    LearningObjective,
    RoundsEvent,
    Session,
    SessionPresenter,
    coi_questions,
)


class SessionFormSet(AutoPositionFormSet):
    """
    Fill in what nobody wants to type. A row with a title but no number is
    the next session; with no start it begins when the previous row ends
    (or when the event starts), and lasts an hour unless an end is given.
    """

    def clean(self):
        super().clean()
        event = self.instance
        if not event.start_at:
            return
        cursor = event.start_at
        rows = sorted(
            (f for f in self.forms if f.has_changed() and not self._should_delete_form(f)),
            key=lambda f: (f.cleaned_data.get("position") or 0),
        )
        for form in rows:
            data = form.cleaned_data
            if not data.get("start_at"):
                data["start_at"] = form.instance.start_at = cursor
            if not data.get("end_at"):
                data["end_at"] = form.instance.end_at = data["start_at"] + DEFAULT_SESSION_LENGTH
            cursor = data["end_at"]


class SessionInline(admin.TabularInline):
    model = Session
    form = PositionedRowForm
    formset = SessionFormSet
    fields = ["position", "title", "start_at", "end_at"]
    show_change_link = True
    verbose_name_plural = (
        "Sessions (type the titles; the number, start and end fill themselves in)"
    )

    def get_extra(self, request, obj=None, **kwargs):
        return 0 if obj else 3


@admin.register(RoundsEvent)
class RoundsEventAdmin(ProgramScopedAdminMixin, BaseAdmin):
    list_display = [
        "date",
        "program",
        "title",
        "status",
        "accredited_credits",
        "session_count",
        "attendance_rows",
        "unmatched_rows",
    ]
    list_filter = ["program", "status"]
    date_hierarchy = "date"
    search_fields = ["title", "teams_meeting_title", "sessions__title"]
    inlines = [SessionInline]
    readonly_fields = ["credit_summary"]
    fieldsets = [
        (
            None,
            {
                "fields": ["program", "title", "date", "status", "accredited_credits"],
                "description": "A blank title takes the program's series name; blank credits "
                "take the program's default.",
            },
        ),
        (
            "Schedule",
            {
                "fields": ["start_at", "end_at"],
                "description": "When rounds starts; the end can be left blank for three "
                "hours later. Attended time is counted against the sessions' own times, "
                "below. If a talk ran over, change its end time.",
            },
        ),
        ("Teams", {"fields": ["teams_join_url", "teams_meeting_title"]}),
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

    @admin.display(description="Per person")
    def credit_summary(self, obj):
        """Computed live from attendance and evaluations. Nothing here is stored."""
        if not obj.pk:
            return "-"
        from credits.reports import event_credit_rows

        rows = event_credit_rows(obj)
        if not rows:
            return "No matched attendance or evaluations yet."
        total = obj.sessions.count()
        def with_adjustment(computed, adjustment):
            return f"{computed} {adjustment:+}" if adjustment else computed

        body = format_html_join(
            "",
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td>"
            "<td><strong>{}</strong></td><td>{}</td><td><strong>{}</strong></td><td>{}</td></tr>",
            (
                (
                    admin_link(person),
                    b.minutes,
                    (b.source or "-").replace("_", " "),
                    f"{len(b.sessions_attended)}/{total}",
                    f"{len(b.sessions_evaluated)}/{total}",
                    b.credited_minutes,
                    with_adjustment(b.attendance_computed, b.attendance_adjustment),
                    b.attendance_credits,
                    ", ".join(str(s.position) for s in b.sessions_presented) or "-",
                    b.teaching_credits,
                    "Review: " + "; ".join(b.review_reasons) if b.needs_review else "",
                )
                for person, b in rows
            ),
        )
        return format_html(
            "<table><thead><tr><th>Person</th><th>Minutes</th><th>From</th>"
            "<th>Attended</th><th>Evaluated</th><th>Attendance minutes credited</th>"
            "<th>Computed (+ adjustment)</th><th>Attendance credits</th>"
            "<th>Presented session</th><th>Teaching credits</th><th></th></tr></thead>"
            "<tbody>{}</tbody></table>",
            body,
        )


class SessionPresenterInline(admin.TabularInline):
    model = SessionPresenter
    form = PositionedRowForm
    formset = AutoPositionFormSet
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
        return format_html(
            "{} on {}",
            admin_link(declaration, declaration.summary.capitalize()),
            timezone.localdate(declaration.declared_at),
        )


class LearningObjectiveInline(admin.TabularInline):
    model = LearningObjective
    form = PositionedRowForm
    formset = AutoPositionFormSet
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
class SessionAdmin(ProgramScopedAdminMixin, BaseAdmin):
    list_display = ["event", "position", "title", "times", "presenter_names", "submitted"]
    list_display_links = ["title"]
    list_filter = ["event__program", SubmittedFilter, MissingCOIFilter, "event__status"]
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
        (
            None,
            {
                "fields": ["event", "position", "title", "start_at", "end_at", "submitted_at"],
                "description": "Attended time is clamped to these times. Leave the end blank "
                "for one hour after the start.",
            },
        ),
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

    @admin.display(description="Time", ordering="start_at")
    def times(self, obj):
        start, end = timezone.localtime(obj.start_at), timezone.localtime(obj.end_at)
        return f"{start:%H:%M}-{end:%H:%M}"

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
        return [("yes", "In force today"), ("no", "Expired")]

    def queryset(self, request, queryset):
        today = timezone.localdate()
        if self.value() == "yes":
            return queryset.valid_on(today)
        if self.value() == "no":
            return queryset.exclude(pk__in=queryset.valid_on(today).values("pk"))
        return queryset


class COIDeclarationForm(SafeModelForm):
    """
    One yes/no and an explanation box per question of the current
    questionnaire. Leaving every box unticked is an explicit no to each
    question, which is what gets written.
    """

    class Meta:
        model = COIDeclaration
        fields = ["person", "declared_at", "disclosure_text_version"]

    def __init__(self, *args, current_version=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.current_version = current_version or settings.COI_CURRENT_VERSION
        self.fields["disclosure_text_version"].widget = forms.Select(
            choices=[(v, v) for v in settings.COI_QUESTIONS]
        )
        self.fields["disclosure_text_version"].initial = self.current_version
        self.fields["disclosure_text_version"].help_text = (
            "The questions below are your program's current version. Choose another only to "
            "transcribe a declaration made on an older form."
        )
        for key, text in coi_questions(self.current_version):
            self.fields[f"q_{key}"] = forms.BooleanField(required=False, label=text)
            self.fields[f"q_{key}_details"] = forms.CharField(
                required=False,
                label="Details",
                widget=forms.Textarea(attrs={"rows": 2}),
                help_text="Required when the answer is yes.",
            )

    def answers(self):
        version = self.cleaned_data.get("disclosure_text_version") or self.current_version
        return {
            key: (
                bool(self.cleaned_data.get(f"q_{key}")),
                self.cleaned_data.get(f"q_{key}_details", ""),
            )
            for key, _ in coi_questions(version)
        }

    def clean(self):
        cleaned = super().clean()
        version = cleaned.get("disclosure_text_version") or self.current_version
        if version != self.current_version:
            self.add_error(
                "disclosure_text_version",
                "Only your program's current questionnaire can be entered here. Older "
                "declarations are read-only.",
            )
            return cleaned
        for key, text in coi_questions(version):
            if cleaned.get(f"q_{key}") and not (cleaned.get(f"q_{key}_details") or "").strip():
                self.add_error(f"q_{key}_details", f"Explain the conflict for: {text}.")
        return cleaned


@admin.register(COIDeclaration)
class COIDeclarationAdmin(AppendOnlyAdmin):
    """
    Add-only. A change of circumstances is a new declaration. Entering one
    here is staff acting on the presenter's behalf, and is logged as such.
    """

    list_display = ["person", "answers_summary", "declared_at", "valid_to", "disclosure_text_version"]
    list_filter = [ValidityFilter, "disclosure_text_version"]
    search_fields = [
        "person__family_name",
        "person__given_name",
        "responses__details",
    ]
    autocomplete_fields = ["person"]
    readonly_fields = ["answers", "valid_to"]

    def current_version_for(self, request):
        """The questionnaire version of the staff member's programs (the first, if several)."""
        versions = list(
            writable_programs(request.user)
            .order_by("name")
            .values_list("coi_question_version", flat=True)
            .distinct()
        )
        return versions[0] if versions else settings.COI_CURRENT_VERSION

    def get_form(self, request, obj=None, **kwargs):
        if obj is None:
            version = self.current_version_for(request)

            class BoundForm(COIDeclarationForm):
                def __init__(self, *args, **inner):
                    inner.setdefault("current_version", version)
                    super().__init__(*args, **inner)

            kwargs["form"] = BoundForm
            # The question fields are added by the form itself; only the
            # model's own fields go through the form factory.
            kwargs["fields"] = ["person", "declared_at", "disclosure_text_version"]
        return super().get_form(request, obj, **kwargs)

    def get_fieldsets(self, request, obj=None):
        if obj is not None:
            return [
                (None, {"fields": ["person", "declared_at", "valid_to", "disclosure_text_version"]}),
                ("Answers, in the wording of that version", {"fields": ["answers"]}),
            ]
        question_fields = []
        for key, _ in coi_questions(self.current_version_for(request)):
            question_fields.append((f"q_{key}", f"q_{key}_details"))
        return [
            (None, {"fields": ["person", "declared_at", "disclosure_text_version"]}),
            (
                "Does the presenter have any of the following? Tick what applies and explain. "
                "Nothing ticked means no to every question.",
                {"fields": question_fields},
            ),
        ]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("person").prefetch_related("responses")

    def save_model(self, request, obj, form, change):
        if change:
            return
        declaration = declare(
            obj.person,
            form.answers(),
            version=form.cleaned_data["disclosure_text_version"],
            declared_at=form.cleaned_data.get("declared_at"),
            user=request.user,
            request=request,
        )
        obj.__dict__.update(declaration.__dict__)

    @admin.display(description="Answers")
    def answers_summary(self, obj):
        return obj.summary

    @admin.display(description="Valid to")
    def valid_to(self, obj):
        return obj.valid_until if obj.pk else "-"

    @admin.display(description="Answers")
    def answers(self, obj):
        if not obj.pk:
            return "-"
        rows = format_html_join(
            "",
            "<tr><td>{}</td><td><strong>{}</strong></td><td>{}</td></tr>",
            (
                (text, "Unanswered" if yes is None else ("Yes" if yes else "No"), details)
                for text, yes, details in obj.rendered()
            ),
        )
        note = "" if obj.is_complete else " This declaration is incomplete and is not in force."
        return format_html(
            "<table><thead><tr><th>Question (version {})</th><th>Answer</th><th>Details</th></tr>"
            "</thead><tbody>{}</tbody></table>{}",
            obj.disclosure_text_version,
            rows,
            note,
        )
