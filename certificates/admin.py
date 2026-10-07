from django.contrib import admin

from core.admin import ProgramScopedAdminMixin, ReadOnlyAdmin, admin_link

from .models import Certificate, CertificateLine


class CertificateLineInline(admin.TabularInline):
    model = CertificateLine
    extra = 0
    can_delete = False
    fields = [
        "event_date",
        "event_title",
        "session_titles",
        "attended_minutes",
        "minutes_source",
        "attendance_credits",
        "presented_session_titles",
        "teaching_minutes",
        "teaching_credits",
    ]
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class StateFilter(admin.SimpleListFilter):
    title = "state"
    parameter_name = "state"

    def lookups(self, request, model_admin):
        return [("valid", "Valid"), ("superseded", "Superseded"), ("revoked", "Revoked")]

    def queryset(self, request, queryset):
        if self.value() == "valid":
            return queryset.filter(revoked_at__isnull=True, superseded_by__isnull=True)
        if self.value() == "superseded":
            return queryset.filter(superseded_by__isnull=False)
        if self.value() == "revoked":
            return queryset.filter(revoked_at__isnull=False)
        return queryset


@admin.register(Certificate)
class CertificateAdmin(ProgramScopedAdminMixin, ReadOnlyAdmin):
    """
    View only. Certificates are snapshots: they are issued, reissued or
    revoked by the application, never edited. Issuing arrives with the PDF.
    """

    admin_only_writes = True
    list_display = [
        "verification_code",
        "recipient_name",
        "program",
        "certificate_type",
        "period_start",
        "period_end",
        "attendance_credits",
        "teaching_credits",
        "total_credits",
        "issued_at",
        "state",
    ]
    list_filter = ["program", StateFilter, "certificate_type", "period_end"]
    date_hierarchy = "issued_at"
    search_fields = [
        "verification_code",
        "recipient_name",
        "person__family_name",
        "person__given_name",
        "person__emails__email",
    ]
    list_select_related = ["person"]
    inlines = [CertificateLineInline]
    readonly_fields = ["state", "supersedes_link", "superseded_by_link"]

    @admin.display(description="State")
    def state(self, obj):
        if obj.is_revoked:
            return "Revoked"
        return "Superseded" if obj.is_superseded else "Valid"

    @admin.display(description="Replaces")
    def supersedes_link(self, obj):
        return admin_link(obj.supersedes) if obj.supersedes_id else "-"

    @admin.display(description="Replaced by")
    def superseded_by_link(self, obj):
        return admin_link(obj.superseded_by) if obj.is_superseded else "-"
