from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    """Staff accounts. Deactivate rather than delete: the audit log points at them."""

    list_display = ["username", "email", "first_name", "last_name", "is_active", "is_superuser", "role_names"]
    list_filter = ["is_active", "is_superuser", "groups"]

    @admin.display(description="Roles")
    def role_names(self, obj):
        return ", ".join(obj.groups.values_list("name", flat=True)) or "-"
