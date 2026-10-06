import json

from django.apps import apps
from django.contrib import admin
from django.core.serializers.json import DjangoJSONEncoder
from django.utils.html import format_html

from core.admin import ReadOnlyAdmin, admin_link

from .models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(ReadOnlyAdmin):
    """View only. There is no way to edit or delete an entry."""

    list_display = ["created_at", "actor_label", "actor_type", "action", "subject", "ip"]
    list_filter = ["action", "actor_type"]
    date_hierarchy = "created_at"
    search_fields = ["actor_label", "action", "object_id", "metadata"]
    fields = [
        "created_at",
        "actor_type",
        "actor_label",
        "actor_user",
        "actor_person",
        "action",
        "subject",
        "object_type",
        "object_id",
        "pretty_metadata",
        "ip",
    ]
    readonly_fields = fields

    @admin.display(description="About")
    def subject(self, obj):
        if not obj.object_type or not obj.object_id:
            return "-"
        try:
            model = apps.get_model(obj.object_type)
            target = model._base_manager.filter(pk=obj.object_id).first()
        except (LookupError, ValueError):
            target = None
        if target is None:
            return f"{obj.object_type} {obj.object_id}"
        return admin_link(target)

    @admin.display(description="Details")
    def pretty_metadata(self, obj):
        text = json.dumps(obj.metadata, indent=2, cls=DjangoJSONEncoder, ensure_ascii=False)
        return format_html("<pre style=\"margin:0\">{}</pre>", text)
