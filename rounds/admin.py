from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.db.models import Count, Q
from django.utils import timezone
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe

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


class SignoffFilter(admin.SimpleListFilter):
    """Find the events a program admin owes a look, without opening each one."""

    title = "sign-off"
    parameter_name = "signoff"

    def lookups(self, request, model_admin):
        from attendance import signoff

        return [
            ("attention", "Needs attention (any)"),
            (signoff.STATE_NEEDS_ANOTHER_LOOK, "Signed off, then changed"),
            (signoff.STATE_NOT_STARTED, "Not started"),
            (signoff.STATE_IN_PROGRESS, "In progress"),
            (signoff.STATE_SIGNED_OFF, "Signed off"),
        ]

    def queryset(self, request, queryset):
        from attendance.signoff import signoff_status

        wanted = self.value()
        if not wanted:
            return queryset
        keep = []
        for event in queryset:
            status = signoff_status(event)
            if wanted == "attention" and status.needs_attention:
                keep.append(event.pk)
            elif status.state == wanted:
                keep.append(event.pk)
        return queryset.filter(pk__in=keep)


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
        "signoff_state",
    ]
    list_filter = ["program", "status", SignoffFilter]
    date_hierarchy = "date"
    search_fields = ["title", "teams_meeting_title", "sessions__title"]
    inlines = [SessionInline]
    readonly_fields = ["credit_summary", "attendance_links"]
    fieldsets = [
        (
            None,
            {
                "fields": ["program", "title", "date", "status", "accredited_credits", "evaluation_form", "attendance_links"],
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

    # --- Sign-off ------------------------------------------------------------

    def get_urls(self):
        return [
            path(
                "<uuid:pk>/signoff/",
                self.admin_site.admin_view(self.signoff_view),
                name="rounds_roundsevent_signoff",
            ),
            path(
                "<uuid:pk>/signin-sheet/",
                self.admin_site.admin_view(self.signin_sheet_view),
                name="rounds_roundsevent_signin_sheet",
            ),
            path(
                "<uuid:pk>/qr/",
                self.admin_site.admin_view(self.qr_view),
                name="rounds_roundsevent_qr",
            ),
            *super().get_urls(),
        ]

    @admin.display(description="Attendance")
    def attendance_links(self, obj):
        if not obj.pk:
            return "-"
        return format_html(
            '<a class="button" href="{}" target="_blank">Show QR code for the room</a> &nbsp; '
            '<a class="button" href="{}">Import paper sign-in sheet</a> &nbsp; '
            '<a class="button" href="{}">Review and sign off attendance</a>',
            reverse("admin:rounds_roundsevent_qr", args=[obj.pk]),
            reverse("admin:rounds_roundsevent_signin_sheet", args=[obj.pk]),
            reverse("admin:rounds_roundsevent_signoff", args=[obj.pk]),
        )

    def qr_view(self, request, pk):
        """
        The code for the room, full screen, refreshing itself every window.
        Anyone who can see the event may show it: showing the code lets
        people in the room sign in, which is what the room is for.
        """
        from attendance import qr

        event = self.get_queryset(request).filter(pk=pk).first()
        if event is None:
            raise Http404
        if not self.has_view_permission(request, event):
            raise PermissionDenied
        sessions = list(event.sessions.order_by("start_at", "position"))
        chosen = request.GET.get("session")
        session = next((s for s in sessions if str(s.pk) == chosen), None) or qr.current_session(event)
        context = {
            "event": event,
            "sessions": sessions,
            "session": session,
            "open": session is not None and qr.session_is_open(session),
            "svg": qr.svg_for(request.build_absolute_uri(qr.scan_path(session))) if session else "",
            "window_seconds": qr.WINDOW_SECONDS,
            "refresh_seconds": qr.WINDOW_SECONDS,
            "grace_minutes": int(qr.SCAN_GRACE.total_seconds() // 60),
            "server_time_ms": int(timezone.now().timestamp() * 1000),
            "clock_tolerance_seconds": qr.CLOCK_TOLERANCE_SECONDS,
            "back_url": reverse("admin:rounds_roundsevent_change", args=[event.pk]),
        }
        return TemplateResponse(request, "admin/rounds/roundsevent/qr.html", context)

    def _writable_event(self, request, pk):
        event = self.get_queryset(request).filter(pk=pk).first()
        if event is None:
            raise Http404
        if not self.has_change_permission(request, event):
            raise PermissionDenied
        return event

    def signin_sheet_view(self, request, pk):
        from attendance.models import AttendanceRecord
        from attendance.sheet import WALK_IN_ROWS, enter_sheet, existing_ticks, sheet_people

        event = self._writable_event(request, pk)
        sessions = list(event.sessions.order_by("start_at", "position"))
        people = sheet_people(event.program)

        if request.method == "POST":
            by_pk = {str(p.pk): p for p in people}
            ticks = set()
            for person in people:
                for session in sessions:
                    if request.POST.get(f"tick_{person.pk}_{session.pk}"):
                        ticks.add((person, session))
            walk_ins = []
            for i in range(WALK_IN_ROWS):
                name = request.POST.get(f"walkin_{i}_name", "")
                ticked = [s for s in sessions if request.POST.get(f"walkin_{i}_{s.pk}")]
                if name.strip() and ticked:
                    walk_ins.append((name, ticked))
            result = enter_sheet(event, ticks, walk_ins, user=request.user, request=request)
            message = f"Recorded {result.written} tick(s)"
            if result.walk_ins:
                message += f", {result.walk_ins} of them for names not on the list, now in the match queue"
            if result.already:
                message += f"; {result.already} already recorded and left alone"
            self.message_user(request, message + ".", messages.SUCCESS)
            return redirect("admin:rounds_roundsevent_signin_sheet", event.pk)

        have = existing_ticks(event)
        rows = [
            {
                "person": person,
                "cells": [
                    {"session": session, "recorded": (person.pk, session.pk) in have}
                    for session in sessions
                ],
            }
            for person in people
        ]
        context = {
            **self.admin_site.each_context(request),
            "title": f"Paper sign-in sheet: {event}",
            "opts": self.model._meta,
            "event": event,
            "sessions": sessions,
            "rows": rows,
            "walk_in_rows": range(WALK_IN_ROWS),
            "records_url": changelist_url(AttendanceRecord, event__id__exact=event.pk),
            "signoff_url": reverse("admin:rounds_roundsevent_signoff", args=[event.pk]),
        }
        return TemplateResponse(request, "admin/rounds/roundsevent/signin_sheet.html", context)

    @admin.display(description="Sign-off")
    def signoff_state(self, obj):
        """
        Where the event stands, with the one case that otherwise goes unseen
        until a certificate is refused in December: signed off, then rows
        arrived or a proposal moved.
        """
        from attendance.signoff import STATE_NEEDS_ANOTHER_LOOK, STATE_NONE, STATE_SIGNED_OFF, signoff_status

        status = signoff_status(obj)
        if status.state == STATE_NONE:
            return "-"
        if status.state == STATE_SIGNED_OFF:
            return f"signed off ({status.decided})"
        url = reverse("admin:rounds_roundsevent_signoff", args=[obj.pk])
        detail = f"{status.decided}/{status.total}"
        if status.state == STATE_NEEDS_ANOTHER_LOOK:
            parts = []
            if status.new:
                parts.append(f"{status.new} arrived since")
            if status.stale:
                parts.append(f"{status.stale} changed since")
            detail = ", ".join(parts)
        if status.blocking:
            detail += f"; {status.blocking} blocking a certificate"
        return format_html(
            '<a href="{}"{}>{}: {}</a>',
            url,
            mark_safe(' style="color:#ba2121;font-weight:bold"') if status.state == STATE_NEEDS_ANOTHER_LOOK else "",
            status.state,
            detail,
        )

    def signoff_view(self, request, pk):
        from attendance.signoff import (
            can_sign_off,
            confirm_event,
            confirm_person,
            review,
            unmatched_sessions,
        )
        from people.models import Person

        event = self._writable_event(request, pk)
        signer = can_sign_off(request.user, event.program)

        if request.method == "POST":
            if not signer:
                raise PermissionDenied
            action = request.POST.get("action")
            if action == "confirm_event":
                result = confirm_event(event, user=request.user, request=request)
                self.message_user(
                    request,
                    f"Confirmed {len(result.confirmed)} session attendance(s) where the sources "
                    f"agree. {len(result.held)} held back for a look.",
                    messages.SUCCESS,
                )
            elif action == "confirm_person":
                person = Person.objects.filter(pk=request.POST.get("person")).first()
                choices = {}
                for session in event.sessions.all():
                    key = str(session.pk)
                    if request.POST.get(f"include_{key}"):
                        value = request.POST.get(f"minutes_{key}", "").strip()
                        choices[key] = int(value) if value.isdigit() else None
                try:
                    if person is None or not choices:
                        raise ValidationError("Choose at least one session to confirm.")
                    written = confirm_person(
                        event, person, choices,
                        user=request.user, comment=request.POST.get("comment", "").strip(),
                        request=request,
                    )
                except ValidationError as error:
                    for message in error.messages:
                        self.message_user(request, message, messages.ERROR)
                except ValueError:
                    self.message_user(request, "Minutes must be whole numbers.", messages.ERROR)
                else:
                    self.message_user(
                        request, f"Signed off {len(written)} session(s) for {person}.", messages.SUCCESS
                    )
            return redirect("admin:rounds_roundsevent_signoff", event.pk)

        from attendance.models import AttendanceRecord

        reviews = review(event)
        unmatched = unmatched_sessions(event)
        context = {
            **self.admin_site.each_context(request),
            "title": f"Sign off attendance: {event}",
            "opts": self.model._meta,
            "event": event,
            "sessions": list(event.sessions.order_by("start_at", "position")),
            "reviews": reviews,
            "unmatched_total": AttendanceRecord.objects.active().unmatched().filter(event=event).count(),
            "unmatched_by_session": unmatched,
            "queue_url": changelist_url(AttendanceRecord, event__id__exact=event.pk, matched="no"),
            "bulk_ready": sum(1 for pr in reviews for s in pr.sessions if s.can_bulk_confirm),
            "held_people": [pr for pr in reviews if pr.held],
            "signer": signer,
        }
        return TemplateResponse(request, "admin/rounds/roundsevent/signoff.html", context)

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
    readonly_fields = ["resolved_evaluation_form"]
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
            "Evaluation form",
            {
                "fields": ["evaluation_form", "resolved_evaluation_form"],
                "description": "Leave blank to use the event's form, then the program's default.",
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

    @admin.display(description="Resolves to")
    def resolved_evaluation_form(self, obj):
        """Which form this session's evaluation uses, and where that comes from."""
        if not obj.pk:
            return "Save first."
        from credits.evaluation_forms import resolve_form

        r = resolve_form(obj)
        parts = []
        if r.form is None:
            parts.append("<strong>No active form resolves: attendees cannot evaluate this session.</strong>")
        else:
            parts.append(
                format_html(
                    '<a href="{}">{}</a> v{} (set on the {})',
                    reverse("admin:credits_evaluationform_change", args=[r.form.pk]),
                    r.form,
                    r.version.number,
                    r.level,
                )
            )
        for level, form in r.skipped:
            parts.append(format_html("Skipped: {} on the {} is {}.", form, level, form.status))
        return format_html_join(mark_safe("<br>"), "{}", ((mark_safe(p),) for p in parts))

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
