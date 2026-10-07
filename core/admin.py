"""Shared pieces for the admin classes in every app."""
from django import forms
from django.contrib import admin
from django.core.exceptions import NON_FIELD_ERRORS
from django.db import models
from django.urls import NoReverseMatch, reverse
from django.utils.html import format_html


class SafeModelForm(forms.ModelForm):
    """
    A model's clean() may complain about a field this form doesn't show
    (a read-only or derived one). Django would crash on that; show the
    message at the top of the form instead.
    """

    def _update_errors(self, errors):
        error_dict = getattr(errors, "error_dict", None)
        if error_dict:
            for name in [n for n in error_dict if n != NON_FIELD_ERRORS and n not in self.fields]:
                error_dict.setdefault(NON_FIELD_ERRORS, []).extend(error_dict.pop(name))
        super()._update_errors(errors)


class PositionedRowForm(SafeModelForm):
    """An inline row whose only entry is its number is an empty row."""

    def has_changed(self):
        if self.instance._state.adding and set(self.changed_data) <= {"position"}:
            return False
        return super().has_changed()


class AutoPositionFormSet(forms.models.BaseInlineFormSet):
    """
    Inline rows with a `position` field: a new row left blank takes the next
    number after everything already saved and everything numbered above it,
    in the order the rows appear. Typed numbers are kept.
    """

    position_field = "position"

    def clean(self):
        name = self.position_field
        taken = set()
        if self.instance.pk:
            taken.update(
                self.queryset.exclude(pk__in=[f.instance.pk for f in self.initial_forms])
                .values_list(name, flat=True)
            )
        live = [f for f in self.forms if f.has_changed() or f.instance.pk]
        live = [f for f in live if not self._should_delete_form(f) and hasattr(f, "cleaned_data")]
        taken.update(f.cleaned_data.get(name) for f in live if f.cleaned_data.get(name))
        for form in live:
            if not form.cleaned_data.get(name):
                number = max(taken, default=0) + 1
                form.cleaned_data[name] = number
                setattr(form.instance, name, number)
                taken.add(number)
        super().clean()


class BaseAdmin(admin.ModelAdmin):
    form = SafeModelForm
    # A URL typed without a scheme is taken as https (Django 6's default).
    formfield_overrides = {models.URLField: {"assume_scheme": "https"}}
    save_on_top = False
    list_per_page = 50


class NoDeleteMixin:
    """For tables whose rows are never deleted. Also removes the bulk delete action."""

    def has_delete_permission(self, request, obj=None):
        return False


class AppendOnlyAdmin(NoDeleteMixin, BaseAdmin):
    """Rows can be added and viewed, never edited or deleted."""

    def has_change_permission(self, request, obj=None):
        return False


class ReadOnlyAdmin(AppendOnlyAdmin):
    """Rows are created by the application only."""

    def has_add_permission(self, request):
        return False


def admin_url(obj):
    """URL of obj's admin page, or None if it has none."""
    try:
        return reverse(
            f"admin:{obj._meta.app_label}_{obj._meta.model_name}_change", args=[obj.pk]
        )
    except NoReverseMatch:
        return None


def admin_link(obj, label=None):
    """A link to obj's admin page, falling back to plain text."""
    if obj is None:
        return "-"
    url = admin_url(obj)
    text = label if label is not None else str(obj)
    return format_html('<a href="{}">{}</a>', url, text) if url else text


def changelist_url(model, **filters):
    """URL of a model's admin list, filtered."""
    url = reverse(f"admin:{model._meta.app_label}_{model._meta.model_name}_changelist")
    if filters:
        url += "?" + "&".join(f"{key}={value}" for key, value in filters.items())
    return url


# --- Program scope in the admin -------------------------------------------------


class ProgramScopedAdminMixin:
    """
    For a ModelAdmin whose model's manager has for_programs().

    - The changelist, change form, autocomplete and actions only ever see
      rows in programs the staff member holds a role in; another program's
      id in the URL is a 404.
    - Adding or changing a row needs a write role (coordinator or program
      admin) in that row's program, or `admin_only_writes` for the fraud
      surface (adjustments, windows, evaluations, certificates), which
      needs the program-admin role there.
    - Foreign keys to program-owned models offer only rows in writable
      programs, so a form cannot point a new row at another program.

    `program_lookup` is read from the model's queryset.
    """

    admin_only_writes = False

    def _program_lookup(self):
        return self.model._default_manager.all().program_lookup

    def _programs_for(self, request):
        from core.authz import admin_programs, staff_programs, writable_programs

        if self.admin_only_writes:
            return staff_programs(request.user), admin_programs(request.user)
        return staff_programs(request.user), writable_programs(request.user)

    def get_queryset(self, request):
        visible, _ = self._programs_for(request)
        return super().get_queryset(request).for_programs(visible)

    def _can_write(self, request, obj):
        from core.authz import program_of

        _, writable = self._programs_for(request)
        if obj is None:
            return writable.exists()
        program = program_of(obj, self._program_lookup())
        return program is not None and writable.filter(pk=program.pk).exists()

    def has_add_permission(self, request):
        return super().has_add_permission(request) and self._can_write(request, None)

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and self._can_write(request, obj)

    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj) and self._can_write(request, obj)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        related = db_field.remote_field.model
        manager = related._default_manager
        if hasattr(manager, "for_programs"):
            _, writable = self._programs_for(request)
            kwargs["queryset"] = manager.for_programs(writable)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def get_search_results(self, request, queryset, search_term):
        # Autocomplete pickers on other screens see the same rows as the list.
        queryset, distinct = super().get_search_results(request, queryset, search_term)
        return queryset, distinct

    def save_model(self, request, obj, form, change):
        from django.core.exceptions import PermissionDenied

        if not self._can_write(request, obj):
            raise PermissionDenied("Not your program.")
        super().save_model(request, obj, form, change)
