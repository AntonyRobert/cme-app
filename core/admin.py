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
