from django.contrib import admin, messages

from audit.log import record
from core.admin import (
    AppendOnlyAdmin,
    BaseAdmin,
    NoDeleteMixin,
    ProgramScopedAdminMixin,
    SafeModelForm,
)

from .models import CreditAdjustment, EvaluationResponse, EvaluationSubmission, EvaluationWindow
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
        "submitted_at",
        "self_reported_session_minutes",
        "attestation",
        "is_complete",
    ]

    def _snapshot(self, obj):
        return {
            "person": str(obj.person_id),
            "session": str(obj.session_id),
            "self_reported_session_minutes": obj.self_reported_session_minutes,
            "is_complete": obj.is_complete,
        }

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
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
