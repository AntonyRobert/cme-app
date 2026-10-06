from django.contrib import admin, messages
from django.db.models import Exists, OuterRef
from django.template.response import TemplateResponse
from django.utils import timezone
from django.utils.html import format_html, format_html_join

from audit.log import record
from core.admin import (
    BaseAdmin,
    NoDeleteMixin,
    SafeModelForm,
    admin_link,
    admin_url,
    changelist_url,
)

from .merge import MergeError, find_collisions, merge_people, person_links
from .models import AllowedDomain, Person, PersonEmail, SignInRequest


class PersonEmailInline(admin.TabularInline):
    model = PersonEmail
    form = SafeModelForm
    extra = 0
    fields = ["email", "is_primary", "verified_at", "added_at"]
    readonly_fields = ["verified_at", "added_at"]


class RecordStateFilter(admin.SimpleListFilter):
    title = "record"
    parameter_name = "state"

    def lookups(self, request, model_admin):
        return [
            ("active", "Active"),
            ("duplicates", "Possible duplicates (same name)"),
            ("merged", "Merged away"),
        ]

    def queryset(self, request, queryset):
        if self.value() == "active":
            return queryset.filter(merged_into__isnull=True)
        if self.value() == "merged":
            return queryset.filter(merged_into__isnull=False)
        if self.value() == "duplicates":
            namesake = Person.objects.filter(
                merged_into__isnull=True,
                given_name__iexact=OuterRef("given_name"),
                family_name__iexact=OuterRef("family_name"),
            ).exclude(pk=OuterRef("pk"))
            return queryset.filter(merged_into__isnull=True).filter(Exists(namesake))
        return queryset


def linked_row_counts(person):
    """How many rows of each kind hang off this record, for the merge screen."""
    counts = []
    for model, field_name in person_links():
        count = model._base_manager.filter(**{field_name: person}).count()
        if count:
            counts.append((model, field_name, count))
    return counts


@admin.register(Person)
class PersonAdmin(NoDeleteMixin, BaseAdmin):
    list_display = [
        "family_name",
        "given_name",
        "credential",
        "role",
        "licence",
        "primary_email",
        "state",
        "created_at",
    ]
    list_display_links = ["family_name", "given_name"]
    list_filter = [RecordStateFilter, "role", "licence_jurisdiction"]
    search_fields = [
        "given_name",
        "family_name",
        "emails__email",
        "licence_number",
        "licence_number_normalized",
    ]
    ordering = ["family_name", "given_name", "created_at"]
    inlines = [PersonEmailInline]
    actions = ["merge_selected"]
    readonly_fields = [
        "licence_number_normalized",
        "merged_into_link",
        "merged_from_links",
        "linked_rows",
        "created_at",
        "updated_at",
    ]
    fieldsets = [
        (None, {"fields": ["given_name", "family_name", "credential", "role"]}),
        (
            "Licence",
            {
                "fields": ["licence_jurisdiction", "licence_number", "licence_number_normalized"],
                "description": "The number is kept exactly as entered and printed that way. "
                "The normalized form is only used to spot duplicates.",
            },
        ),
        (
            "Record",
            {
                "fields": [
                    "staff_user",
                    "merged_into_link",
                    "merged_from_links",
                    "linked_rows",
                    "created_at",
                    "updated_at",
                ]
            },
        ),
    ]
    autocomplete_fields = ["staff_user"]

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("emails").select_related("merged_into")

    def get_search_results(self, request, queryset, search_term):
        queryset, distinct = super().get_search_results(request, queryset, search_term)
        # Pickers on other screens should never offer a merged-away record.
        if request.path.endswith("/autocomplete/"):
            queryset = queryset.filter(merged_into__isnull=True)
        return queryset, distinct

    @admin.display(description="Licence", ordering="licence_number_normalized")
    def licence(self, obj):
        if not obj.licence_number:
            return "-"
        return f"{obj.licence_jurisdiction} {obj.licence_number}"

    @admin.display(description="Primary email")
    def primary_email(self, obj):
        emails = list(obj.emails.all())
        primary = next((e.email for e in emails if e.is_primary), None)
        extra = len(emails) - 1
        shown = primary or (emails[0].email if emails else "-")
        return f"{shown} (+{extra})" if extra > 0 else shown

    @admin.display(description="State")
    def state(self, obj):
        if obj.merged_into_id:
            return format_html("Merged into {}", admin_link(obj.merged_into))
        return "Active"

    @admin.display(description="Merged into")
    def merged_into_link(self, obj):
        return admin_link(obj.merged_into) if obj.merged_into_id else "-"

    @admin.display(description="Records merged into this one")
    def merged_from_links(self, obj):
        if not obj.pk:
            return "-"
        tombstones = list(obj.merged_from.all())
        if not tombstones:
            return "-"
        return format_html_join(", ", "{}", ((admin_link(t),) for t in tombstones))

    @admin.display(description="Linked rows")
    def linked_rows(self, obj):
        if not obj.pk:
            return "-"
        counts = linked_row_counts(obj)
        if not counts:
            return "None"
        parts = []
        for model, field_name, count in counts:
            label = model._meta.verbose_name if count == 1 else model._meta.verbose_name_plural
            if self.admin_site.is_registered(model):
                url = changelist_url(model, **{f"{field_name}__id__exact": obj.pk})
                parts.append(format_html('<a href="{}">{} {}</a>', url, count, label))
            else:
                parts.append(format_html("{} {}", count, label))
        return format_html_join(", ", "{}", ((part,) for part in parts))

    # --- Merging -------------------------------------------------------------

    def has_merge_permission(self, request):
        return request.user.has_perm("people.merge_person")

    @admin.action(description="Merge two selected people…", permissions=["merge"])
    def merge_selected(self, request, queryset):
        people = list(queryset.order_by("created_at"))
        if len(people) != 2:
            self.message_user(request, "Select exactly two people to merge.", messages.ERROR)
            return None
        if any(person.is_merged for person in people):
            self.message_user(
                request, "One of those records has already been merged away.", messages.ERROR
            )
            return None

        collisions = find_collisions(people[0], people[1])

        if request.POST.get("confirm") and not collisions:
            survivor = next((p for p in people if str(p.pk) == request.POST.get("survivor")), None)
            if survivor is None:
                self.message_user(request, "Choose which record to keep.", messages.ERROR)
            else:
                duplicate = next(p for p in people if p.pk != survivor.pk)
                try:
                    result = merge_people(survivor, duplicate, user=request.user, request=request)
                except MergeError as error:
                    self.message_user(request, str(error), messages.ERROR)
                    return None
                self.message_user(
                    request,
                    f"Merged into {survivor}: {len(result.moved_emails)} email(s) moved, "
                    f"{result.repointed_count} row(s) re-pointed.",
                    messages.SUCCESS,
                )
                if duplicate.licence_number and not survivor.licence_number:
                    self.message_user(
                        request,
                        f"The merged record held licence {duplicate.licence_jurisdiction} "
                        f"{duplicate.licence_number}. Licence numbers are not copied: "
                        f"enter it on {survivor} if it is theirs.",
                        messages.WARNING,
                    )
                return None

        context = {
            **self.admin_site.each_context(request),
            "title": "Merge two people",
            "opts": self.model._meta,
            "summaries": [
                {
                    "person": person,
                    "emails": list(person.emails.all()),
                    "counts": [
                        (model._meta.verbose_name_plural, count)
                        for model, _, count in linked_row_counts(person)
                        if model is not PersonEmail
                    ],
                }
                for person in people
            ],
            "collisions": [
                {
                    "description": collision.description,
                    "rows": [
                        (people[0], collision.survivor_row, admin_url(collision.survivor_row)),
                        (people[1], collision.duplicate_row, admin_url(collision.duplicate_row)),
                    ],
                }
                for collision in collisions
            ],
            "default_survivor": people[0].pk,
        }
        return TemplateResponse(request, "admin/people/person/merge.html", context)


@admin.register(AllowedDomain)
class AllowedDomainAdmin(BaseAdmin):
    list_display = ["domain", "auto_admit", "note"]
    list_filter = ["auto_admit"]
    search_fields = ["domain", "note"]


@admin.register(SignInRequest)
class SignInRequestAdmin(NoDeleteMixin, BaseAdmin):
    """The review queue for sign-ins from addresses outside the allowed domains."""

    list_display = ["email", "given_name", "family_name", "requested_at", "status", "decided_by"]
    list_filter = ["status"]
    search_fields = ["email", "given_name", "family_name"]
    fields = [
        "email",
        "given_name",
        "family_name",
        "requested_at",
        "status",
        "note",
        "decided_by",
        "decided_at",
    ]
    readonly_fields = [
        "email",
        "given_name",
        "family_name",
        "requested_at",
        "decided_by",
        "decided_at",
    ]

    def has_add_permission(self, request):
        return False

    def save_model(self, request, obj, form, change):
        if "status" in form.changed_data:
            obj.decided_by = request.user
            obj.decided_at = timezone.now()
        super().save_model(request, obj, form, change)
        if "status" in form.changed_data:
            record(
                "signin_request.decided",
                obj,
                request=request,
                metadata={"email": obj.email, "status": obj.status},
            )
