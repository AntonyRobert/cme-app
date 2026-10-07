from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html, format_html_join

from audit.log import record
from core.admin import (
    AppendOnlyAdmin,
    BaseAdmin,
    NoDeleteMixin,
    ProgramScopedAdminMixin,
    SafeModelForm,
)

from .models import (
    CreditAdjustment,
    EvaluationForm,
    EvaluationFormVersion,
    EvaluationQuestion,
    EvaluationResponse,
    EvaluationSubmission,
    EvaluationWindow,
)
from .windows import ReopeningRefused, request_reopening


class EvaluationResponseInline(admin.TabularInline):
    model = EvaluationResponse
    form = SafeModelForm
    extra = 0
    fields = ["question_key", "objective", "rating", "free_text"]

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        # Only offer the objectives of the session being evaluated.
        if db_field.name == "objective":
            submission_id = request.resolver_match.kwargs.get("object_id")
            queryset = db_field.remote_field.model.objects.none()
            if submission_id:
                queryset = db_field.remote_field.model.objects.filter(
                    session__evaluations__pk=submission_id
                )
            kwargs["queryset"] = queryset
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


@admin.register(EvaluationSubmission)
class EvaluationSubmissionAdmin(ProgramScopedAdminMixin, BaseAdmin):
    """
    Evaluations normally come from the attendee's own form. Entering,
    editing or deleting one here is staff acting on their behalf, and is
    audit-logged.
    """

    list_display = [
        "person",
        "session",
        "submitted_at",
        "self_reported_session_minutes",
        "is_complete",
    ]
    admin_only_writes = True
    list_filter = ["session__event__program", "is_complete", "session__event"]
    date_hierarchy = "session__event__date"
    search_fields = ["person__family_name", "person__given_name", "session__title"]
    list_select_related = ["person", "session__event"]
    autocomplete_fields = ["person", "session"]
    inlines = [EvaluationResponseInline]
    fields = [
        "person",
        "session",
        "form_version",
        "submitted_at",
        "self_reported_session_minutes",
        "attestation",
        "is_complete",
        "answers",
    ]
    readonly_fields = ["form_version", "is_complete", "answers"]

    @admin.display(description="As asked")
    def answers(self, obj):
        """The questions as worded in the version answered, with the answers given."""
        if not obj.pk:
            return "Set once saved: the form this session resolves to."
        from .evaluation_forms import rendered_questions, response_for, responses_by_question

        responses = responses_by_question(obj)
        rows = []
        for q in rendered_questions(obj.form_version, obj.session):
            r = response_for(q, responses)
            if r is None or not r.answered:
                answer = "(not answered)"
            elif r.rating is not None:
                answer = r.rating
            elif r.selected:
                answer = ", ".join(r.selected)
            else:
                answer = r.free_text
            rows.append((q.prompt, " (required)" if q.required else "", answer))
        return format_html(
            "<p>{}</p><table><tbody>{}</tbody></table>",
            obj.form_version,
            format_html_join("", "<tr><td>{}<small>{}</small></td><td>{}</td></tr>", rows),
        )

    def save_model(self, request, obj, form, change):
        if not change and obj.form_version_id is None:
            from .evaluation_forms import resolve_form

            resolution = resolve_form(obj.session)
            if resolution.version is None:
                raise ValidationError("No active evaluation form resolves for this session.")
            obj.form_version = resolution.version
        super().save_model(request, obj, form, change)
        self._log_save(request, obj, form, change)

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        form.instance.recompute()  # the inline answers decide completeness

    def _snapshot(self, obj):
        return {
            "person": str(obj.person_id),
            "session": str(obj.session_id),
            "form_version": str(obj.form_version_id),
            "self_reported_session_minutes": obj.self_reported_session_minutes,
            "is_complete": obj.is_complete,
        }

    def _log_save(self, request, obj, form, change):
        record(
            "evaluation.edited_by_staff" if change else "evaluation.entered_by_staff",
            obj,
            request=request,
            metadata={**self._snapshot(obj), "changed": form.changed_data},
        )

    def delete_model(self, request, obj):
        snapshot = {
            **self._snapshot(obj),
            "responses": list(
                obj.responses.values("question_key", "objective_id", "rating", "free_text")
            ),
        }
        record("evaluation.deleted_by_staff", obj, request=request, metadata=snapshot)
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        for obj in queryset:
            self.delete_model(request, obj)


class WindowForm(SafeModelForm):
    class Meta:
        model = EvaluationWindow
        fields = ["person", "session", "reason"]


@admin.register(EvaluationWindow)
class EvaluationWindowAdmin(ProgramScopedAdminMixin, NoDeleteMixin, BaseAdmin):
    """
    Reopen an evaluation form for one person and one session.

    Adding one here is a staff override: it skips the self-service limits
    (three per session, and never past the end of the accreditation year)
    and is logged against you.
    """

    admin_only_writes = True
    list_display = ["person", "session", "opened_at", "expires_at", "closed_at", "granted_by", "state"]
    list_filter = ["session__event__program", "session__event"]
    date_hierarchy = "opened_at"
    search_fields = ["person__family_name", "person__given_name", "session__title", "reason"]
    list_select_related = ["person", "session__event", "granted_by"]
    autocomplete_fields = ["person", "session"]
    readonly_fields = ["opened_at", "expires_at", "granted_by", "closed_at"]

    def get_form(self, request, obj=None, **kwargs):
        if obj is None:
            kwargs["form"] = WindowForm
        return super().get_form(request, obj, **kwargs)

    def get_fields(self, request, obj=None):
        if obj is None:
            return ["person", "session", "reason"]
        return ["person", "session", "reason", "opened_at", "expires_at", "granted_by", "closed_at"]

    def has_change_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        if change:
            return
        try:
            window = request_reopening(
                obj.person,
                obj.session,
                reason=obj.reason,
                user=request.user,
                override=True,
                request=request,
            )
        except ReopeningRefused as refused:
            # The form has already validated; the only refusal left is
            # "already evaluated", which the message explains.
            self.message_user(request, str(refused), messages.ERROR)
            obj.pk = None
            return
        obj.__dict__.update(window.__dict__)
        self.message_user(
            request,
            f"Open until {window.expires_at:%Y-%m-%d %H:%M}. Logged as an override by you.",
            messages.SUCCESS,
        )

    def response_add(self, request, obj, post_url_continue=None):
        if obj.pk is None:
            return self.response_post_save_add(request, obj)
        return super().response_add(request, obj, post_url_continue)

    @admin.display(description="State")
    def state(self, obj):
        if obj.closed_at:
            return "Closed: evaluated"
        return "Open" if obj.is_open() else "Expired"


@admin.register(CreditAdjustment)
class CreditAdjustmentAdmin(ProgramScopedAdminMixin, AppendOnlyAdmin):
    """
    Add-only ledger. Use it when the hours are right and the credit still
    isn't. If the hours are wrong, correct the attendance instead.
    """

    list_display = ["person", "event", "kind", "delta_credits", "short_reason", "created_by", "created_at"]
    admin_only_writes = True
    list_filter = ["event__program", "kind", "event"]
    date_hierarchy = "created_at"
    search_fields = ["person__family_name", "person__given_name", "reason"]
    list_select_related = ["person", "event", "created_by"]
    autocomplete_fields = ["person"]
    fields = ["person", "event", "kind", "delta_credits", "reason", "created_by", "created_at"]
    readonly_fields = ["created_by", "created_at"]

    def get_fields(self, request, obj=None):
        fields = super().get_fields(request, obj)
        return fields if obj else [f for f in fields if f not in self.readonly_fields]

    @admin.display(description="Reason")
    def short_reason(self, obj):
        return obj.reason if len(obj.reason) <= 80 else obj.reason[:77] + "..."

    def save_model(self, request, obj, form, change):
        obj.created_by = request.user
        super().save_model(request, obj, form, change)
        record(
            "credit.adjusted",
            obj,
            request=request,
            metadata={
                "person": str(obj.person_id),
                "event": str(obj.event_id),
                "kind": obj.kind,
                "delta_credits": obj.delta_credits,
                "reason": obj.reason,
            },
        )


# --- Evaluation form templates ------------------------------------------------------


class QuestionInline(admin.TabularInline):
    model = EvaluationQuestion
    form = SafeModelForm
    extra = 0
    fields = ["position", "question_key", "kind", "required", "prompt", "help_text", "choices"]

    def has_add_permission(self, request, obj=None):
        return super().has_add_permission(request, obj) and not (obj and obj.is_locked)

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and not (obj and obj.is_locked)

    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj) and not (obj and obj.is_locked)


@admin.register(EvaluationFormVersion)
class EvaluationFormVersionAdmin(ProgramScopedAdminMixin, NoDeleteMixin, BaseAdmin):
    """
    One version's questions. Editable in place until an evaluation is
    submitted against it; from then on it is locked, and edits go to a new
    version made from the form's page.
    """

    admin_only_writes = True
    list_display = ["form", "number", "note", "question_count", "submission_count", "locked", "created_at"]
    list_filter = ["form__program", "form"]
    list_select_related = ["form__program"]
    readonly_fields = ["form", "number", "created_at", "created_by", "state"]
    fields = ["form", "number", "note", "created_at", "created_by", "state"]
    inlines = [QuestionInline]

    def has_add_permission(self, request):
        return False  # versions are made from the form page, never bare

    @admin.display(description="Questions")
    def question_count(self, obj):
        return obj.questions.count()

    @admin.display(description="Submissions")
    def submission_count(self, obj):
        return obj.submission_count

    @admin.display(description="Locked", boolean=True)
    def locked(self, obj):
        return obj.is_locked

    @admin.display(description="State")
    def state(self, obj):
        n = obj.submission_count
        if n:
            return format_html(
                "<strong>Locked: {} evaluation(s) answered this wording.</strong> To change the "
                'questions, <a href="{}">make a new version of the form</a>; this one stays as it is.',
                n,
                reverse("admin:credits_evaluationform_change", args=[obj.form_id]),
            )
        return "No submissions yet: the questions can be edited in place. The first evaluation locks it."

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        if obj is not None and obj.is_locked:
            fields.append("note")
        return fields


class VersionInline(admin.TabularInline):
    model = EvaluationFormVersion
    extra = 0
    can_delete = False
    fields = ["number", "note", "submission_count", "locked", "created_at", "open"]
    readonly_fields = fields
    ordering = ["-number"]

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description="Submissions")
    def submission_count(self, obj):
        return obj.submission_count

    @admin.display(description="Locked", boolean=True)
    def locked(self, obj):
        return obj.is_locked

    @admin.display(description="")
    def open(self, obj):
        return format_html(
            '<a href="{}">{}</a>',
            reverse("admin:credits_evaluationformversion_change", args=[obj.pk]),
            "view" if obj.is_locked else "edit questions",
        )


@admin.register(EvaluationForm)
class EvaluationFormAdmin(ProgramScopedAdminMixin, NoDeleteMixin, BaseAdmin):
    """
    A reusable evaluation template. Program admins make and edit them in
    their own program; coordinators see them. Questions live on versions.
    """

    admin_only_writes = True
    list_display = ["name", "program", "status", "current", "submissions", "used_by"]
    list_filter = ["program", "status"]
    search_fields = ["name"]
    readonly_fields = ["created_at", "created_by", "actions_box"]
    fields = ["program", "name", "status", "created_at", "created_by", "actions_box"]
    inlines = [VersionInline]

    def get_urls(self):
        return [
            path(
                "<uuid:pk>/new-version/",
                self.admin_site.admin_view(self.new_version_view),
                name="credits_evaluationform_new_version",
            ),
            path(
                "<uuid:pk>/duplicate/",
                self.admin_site.admin_view(self.duplicate_view),
                name="credits_evaluationform_duplicate",
            ),
            path(
                "<uuid:pk>/preview/",
                self.admin_site.admin_view(self.preview_view),
                name="credits_evaluationform_preview",
            ),
            *super().get_urls(),
        ]

    @admin.display(description="Current")
    def current(self, obj):
        v = obj.current_version
        return f"v{v.number} ({v.questions.count()} questions)" if v else "no version yet"

    @admin.display(description="Submissions")
    def submissions(self, obj):
        return EvaluationSubmission.objects.filter(form_version__form=obj).count()

    @admin.display(description="Used by")
    def used_by(self, obj):
        from rounds.models import RoundsEvent, Session

        parts = []
        if obj.program.default_evaluation_form_id == obj.pk:
            parts.append("program default")
        events = RoundsEvent.objects.filter(evaluation_form=obj).count()
        sessions = Session.objects.filter(evaluation_form=obj).count()
        if events:
            parts.append(f"{events} event(s)")
        if sessions:
            parts.append(f"{sessions} session(s)")
        return ", ".join(parts) or "-"

    @admin.display(description="Actions")
    def actions_box(self, obj):
        if not obj.pk:
            return "Save first. A first version is created with the form."
        v = obj.current_version
        note = ""
        if v is not None and v.is_locked:
            note = (
                f" v{v.number} has {v.submission_count} submission(s) and is locked: editing means "
                "a new version, and what was already answered keeps its wording."
            )
        return format_html(
            '<a class="button" href="{}">Preview as an attendee</a> &nbsp; '
            '<a class="button" href="{}">New version (copy of v{})</a> &nbsp; '
            '<a class="button" href="{}">Duplicate this form</a><p class="help">{}</p>',
            reverse("admin:credits_evaluationform_preview", args=[obj.pk]),
            reverse("admin:credits_evaluationform_new_version", args=[obj.pk]),
            v.number if v else "-",
            reverse("admin:credits_evaluationform_duplicate", args=[obj.pk]),
            note,
        )

    def save_model(self, request, obj, form, change):
        obj.created_by = obj.created_by or request.user
        super().save_model(request, obj, form, change)
        if not change:
            from .evaluation_forms import new_version

            new_version(obj, user=request.user, note="First version.", request=request)

    def _writable(self, request, pk):
        obj = self.get_queryset(request).filter(pk=pk).first()
        if obj is None:
            raise Http404
        if not self.has_change_permission(request, obj):
            raise PermissionDenied
        return obj

    def new_version_view(self, request, pk):
        from .evaluation_forms import new_version

        form = self._writable(request, pk)
        if request.method != "POST":
            return TemplateResponse(
                request,
                "admin/credits/evaluationform/confirm.html",
                {
                    **self.admin_site.each_context(request),
                    "title": f"New version of {form}",
                    "opts": self.model._meta,
                    "form_obj": form,
                    "question": (
                        f"Make v{(form.current_version.number + 1) if form.current_version else 1} "
                        "as a copy of the current questions? Evaluations already submitted keep "
                        "the version they answered."
                    ),
                    "action": "Create the new version",
                    "field": "note",
                    "field_label": "What is changing, and why",
                },
            )
        version = new_version(form, user=request.user, note=request.POST.get("note", "").strip(), request=request)
        self.message_user(request, f"Created {version}. Edit its questions now; it locks at the first submission.")
        return redirect("admin:credits_evaluationformversion_change", version.pk)

    def duplicate_view(self, request, pk):
        from .evaluation_forms import duplicate_form

        form = self._writable(request, pk)
        if request.method != "POST":
            return TemplateResponse(
                request,
                "admin/credits/evaluationform/confirm.html",
                {
                    **self.admin_site.each_context(request),
                    "title": f"Duplicate {form}",
                    "opts": self.model._meta,
                    "form_obj": form,
                    "question": "Copy the current questions into a new draft form in the same program?",
                    "action": "Duplicate",
                    "field": "name",
                    "field_label": "Name of the new form",
                    "default": f"Copy of {form.name}",
                },
            )
        try:
            copy = duplicate_form(form, name=request.POST.get("name", "").strip() or f"Copy of {form.name}", user=request.user, request=request)
        except ValidationError as error:
            self.message_user(request, " ".join(error.messages), messages.ERROR)
            return redirect("admin:credits_evaluationform_change", form.pk)
        self.message_user(request, f"Created {copy} as a draft. Activate it when it is ready.")
        return redirect("admin:credits_evaluationform_change", copy.pk)

    def preview_view(self, request, pk):
        """The form as an attendee sees it, for a chosen session so per-objective questions expand."""
        from rounds.models import Session

        from .evaluation_forms import rendered_questions

        form = self.get_queryset(request).filter(pk=pk).first()
        if form is None:
            raise Http404
        version = form.current_version
        sessions = Session.objects.filter(event__program=form.program).select_related("event").order_by("-start_at")[:50]
        chosen = request.GET.get("session")
        session = next((s for s in sessions if str(s.pk) == chosen), None) or (sessions[0] if sessions else None)
        context = {
            **self.admin_site.each_context(request),
            "title": f"Preview: {form} v{version.number if version else '-'}",
            "opts": self.model._meta,
            "form_obj": form,
            "version": version,
            "sessions": sessions,
            "session": session,
            "questions": rendered_questions(version, session) if (version and session) else [],
            "preview": True,
        }
        return TemplateResponse(request, "admin/credits/evaluationform/preview.html", context)
