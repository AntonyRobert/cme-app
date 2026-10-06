from django.contrib import admin

from audit.log import record
from core.admin import AppendOnlyAdmin, BaseAdmin, SafeModelForm

from .models import CreditAdjustment, EvaluationResponse, EvaluationSubmission


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
class EvaluationSubmissionAdmin(BaseAdmin):
    """
    Evaluations normally come from the attendee's own form. Entering,
    editing or deleting one here is staff acting on their behalf, and is
    audit-logged.
    """

    list_display = [
        "person",
        "session",
        "submitted_at",
        "self_reported_minutes",
        "is_complete",
    ]
    list_filter = ["is_complete", "session__event"]
    date_hierarchy = "session__event__date"
    search_fields = ["person__family_name", "person__given_name", "session__title"]
    list_select_related = ["person", "session__event"]
    autocomplete_fields = ["person", "session"]
    inlines = [EvaluationResponseInline]
    fields = [
        "person",
        "session",
        "submitted_at",
        "self_reported_minutes",
        "attestation",
        "is_complete",
    ]

    def _snapshot(self, obj):
        return {
            "person": str(obj.person_id),
            "session": str(obj.session_id),
            "self_reported_minutes": obj.self_reported_minutes,
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


@admin.register(CreditAdjustment)
class CreditAdjustmentAdmin(AppendOnlyAdmin):
    """
    Add-only ledger. Use it when the hours are right and the credit still
    isn't. If the hours are wrong, correct the attendance instead.
    """

    list_display = ["person", "event", "delta_credits", "short_reason", "created_by", "created_at"]
    list_filter = ["event"]
    date_hierarchy = "created_at"
    search_fields = ["person__family_name", "person__given_name", "reason"]
    list_select_related = ["person", "event", "created_by"]
    autocomplete_fields = ["person"]
    fields = ["person", "event", "delta_credits", "reason", "created_by", "created_at"]
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
                "delta_credits": obj.delta_credits,
                "reason": obj.reason,
            },
        )
