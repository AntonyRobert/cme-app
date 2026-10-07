from django.contrib import admin
from django.core.exceptions import PermissionDenied

from core.admin import BaseAdmin, NoDeleteMixin, SafeModelForm
from core.authz import admin_programs, staff_programs

from .models import Institution, Program, ProgramRole


@admin.register(Institution)
class InstitutionAdmin(NoDeleteMixin, BaseAdmin):
    """One row per instance. Superusers edit it; everyone else reads it."""

    list_display = ["name", "short_name", "website"]
    search_fields = ["name", "short_name"]

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser


class ProgramRoleInline(admin.TabularInline):
    model = ProgramRole
    form = SafeModelForm
    extra = 0
    fields = ["user", "role", "granted_by", "granted_at"]
    readonly_fields = ["granted_by", "granted_at"]
    autocomplete_fields = ["user"]

    def has_add_permission(self, request, obj=None):
        return request.user.is_superuser or (
            obj is not None and admin_programs(request.user).filter(pk=obj.pk).exists()
        )

    has_change_permission = has_add_permission
    has_delete_permission = has_add_permission

    def formfield_for_choice_field(self, db_field, request, **kwargs):
        # Only superusers make program admins; a program admin grants the other two.
        if db_field.name == "role" and not request.user.is_superuser:
            kwargs["choices"] = [
                (value, label)
                for value, label in ProgramRole.Role.choices
                if value != ProgramRole.Role.PROGRAM_ADMIN
            ]
        return super().formfield_for_choice_field(db_field, request, **kwargs)


@admin.register(Program)
class ProgramAdmin(NoDeleteMixin, BaseAdmin):
    """
    A program's own settings: rates, defaults, the accreditation year, the
    questionnaire, the series name, retention. Visible to anyone with a role
    in it; editable by its program admins and superusers.
    """

    list_display = [
        "name",
        "institution",
        "slug",
        "attendance_rate_per_hour",
        "teaching_rate_per_hour",
        "default_accredited_credits",
        "is_active",
    ]
    list_filter = ["institution", "is_active"]
    search_fields = ["name", "slug", "series_name"]
    inlines = [ProgramRoleInline]
    fieldsets = [
        (None, {"fields": ["institution", "name", "slug", "series_name", "is_active"]}),
        (
            "Credit",
            {
                "fields": [
                    "attendance_rate_per_hour",
                    "teaching_rate_per_hour",
                    "default_accredited_credits",
                    "attendance_disagreement_minutes",
                ],
                "description": "Rates apply from now on. Every issued certificate line keeps "
                "the rate that applied when it was issued.",
            },
        ),
        (
            "Accreditation year and declarations",
            {
                "fields": [
                    "accreditation_year_end_month",
                    "accreditation_year_end_day",
                    "coi_question_version",
                    "retention_years",
                    "default_evaluation_form",
                    "require_evaluation_for_credit",
                    "activity_evaluation_form",
                    "activity_evaluation_cadence",
                    "accreditation_statement",
                ]
            },
        ),
    ]

    def get_queryset(self, request):
        return super().get_queryset(request).for_programs(staff_programs(request.user))

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        if not super().has_change_permission(request, obj):
            return False
        if request.user.is_superuser:
            return True
        if obj is None:
            return admin_programs(request.user).exists()
        return admin_programs(request.user).filter(pk=obj.pk).exists()

    def get_readonly_fields(self, request, obj=None):
        # The institution and slug are identity; a program admin changes the rest.
        return [] if request.user.is_superuser else ["institution", "slug"]

    def save_formset(self, request, form, formset, change):
        links = formset.save(commit=False)
        for link in links:
            if link.role == ProgramRole.Role.PROGRAM_ADMIN and not request.user.is_superuser:
                raise PermissionDenied("Only a superuser makes a program admin.")
            if link.granted_by_id is None:
                link.granted_by = request.user
            link.save()
        for link in formset.deleted_objects:
            link.delete()
        formset.save_m2m()


@admin.register(ProgramRole)
class ProgramRoleAdmin(BaseAdmin):
    """Who holds which role where. The same rows as the inline on the program."""

    list_display = ["user", "program", "role", "granted_by", "granted_at"]
    list_filter = ["role", "program"]
    search_fields = ["user__username", "user__email", "program__name"]
    autocomplete_fields = ["user"]
    readonly_fields = ["granted_by", "granted_at"]

    def get_queryset(self, request):
        return super().get_queryset(request).filter(program__in=staff_programs(request.user))

    def _may_manage(self, request, program):
        return request.user.is_superuser or (
            program is not None and admin_programs(request.user).filter(pk=program.pk).exists()
        )

    def has_add_permission(self, request):
        return request.user.is_superuser or admin_programs(request.user).exists()

    def has_change_permission(self, request, obj=None):
        if obj is None:
            return self.has_add_permission(request)
        return self._may_manage(request, obj.program)

    has_delete_permission = has_change_permission

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "program" and not request.user.is_superuser:
            kwargs["queryset"] = admin_programs(request.user)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def formfield_for_choice_field(self, db_field, request, **kwargs):
        if db_field.name == "role" and not request.user.is_superuser:
            kwargs["choices"] = [
                (value, label)
                for value, label in ProgramRole.Role.choices
                if value != ProgramRole.Role.PROGRAM_ADMIN
            ]
        return super().formfield_for_choice_field(db_field, request, **kwargs)

    def save_model(self, request, obj, form, change):
        if not self._may_manage(request, obj.program):
            raise PermissionDenied("Not your program.")
        if obj.role == ProgramRole.Role.PROGRAM_ADMIN and not request.user.is_superuser:
            raise PermissionDenied("Only a superuser makes a program admin.")
        if obj.granted_by_id is None:
            obj.granted_by = request.user
        super().save_model(request, obj, form, change)
