"""
Object-level authorization, in two scopes.

The rule (CLAUDE.md, non-negotiable 1): every route that takes an id must
verify the object belongs to the requesting person. The pattern:

1. Every person-owned model's manager has for_person() (see
   people.ownership.PersonOwnedQuerySet).
2. A view that takes an id is wrapped in @owned_object(Model). The wrapper
   does the lookup through for_person() and hands the view the object, so
   the view never sees a raw id.
3. A route that is public on purpose, like /verify/<code>, is wrapped in
   @public_object("why").
4. unmarked_routes() lists every parameterized route that is neither, and
   a test fails if that list is not empty.

The same shape applies to STAFF and PROGRAMS. Staff hold roles in one or
more programs (programs.ProgramRole). Every program-owned model's manager
has for_programs() (ProgramScopedQuerySet below); a staff view that takes
an id is wrapped in @program_scoped(Model), which looks the row up through
the programs the staff member may see. An Emergency Medicine coordinator
never sees Internal Medicine's match queue, and another program's id in a
URL is a 404, not a 403.

The admin is exempt from the URL walk because it has its own permission
system; its program scope is applied by core.admin.ProgramScopedAdminMixin
and checked by a separate test that walks the admin registry.
"""
from functools import wraps

from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.db import models
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.urls import URLPattern, URLResolver, get_resolver

OWNED = "owned"
PUBLIC = "public"
PROGRAM = "program"

EXEMPT_APP_NAMES = {"admin"}


# --- Person scope -----------------------------------------------------------------


def current_person(request):
    """
    The signed-in Person, or 404.

    The sign-in middleware sets request.person; this is the only place
    views read it.
    """
    person = getattr(request, "person", None)
    if person is None:
        raise Http404
    return person


def get_owned_or_404(model, pk, person):
    """
    Fetch model row pk only if it belongs to person.

    Someone else's row and a missing row both give 404, so the response
    never confirms that an id exists.
    """
    manager = model._default_manager
    if not hasattr(manager, "for_person"):
        raise ImproperlyConfigured(
            f"{model.__name__} has no for_person(); it cannot be served by id."
        )
    return get_object_or_404(manager.for_person(person), pk=pk)


def owned_object(model, *, url_kwarg="id", arg_name="obj"):
    """
    Wrap a view that takes an object id in its URL.

    The view receives the object itself as arg_name, already checked to
    belong to the signed-in person.
    """

    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            person = current_person(request)
            kwargs[arg_name] = get_owned_or_404(model, kwargs.pop(url_kwarg), person)
            return view(request, *args, **kwargs)

        wrapper.object_authorization = OWNED
        return wrapper

    return decorator


# --- Program scope ----------------------------------------------------------------


class ProgramScopedQuerySet(models.QuerySet):
    """
    Base queryset for every table whose rows belong to a program.

    for_programs() is the one way staff screens narrow a table to what the
    signed-in staff member may see. program_lookup is the path from the
    model to its Program.
    """

    program_lookup = "program"

    def for_programs(self, programs):
        ids = [getattr(p, "pk", p) for p in programs]
        return self.filter(**{f"{self.program_lookup}__in": ids})


def staff_programs(user, roles=None):
    """
    The programs a staff user may see, or, with `roles`, the programs they
    hold one of those roles in. Superusers see every program.
    """
    from programs.models import Program, ProgramRole

    if not user.is_authenticated or not user.is_active:
        return Program.objects.none()
    if user.is_superuser:
        return Program.objects.all()
    links = ProgramRole.objects.filter(user=user)
    if roles is not None:
        links = links.filter(role__in=roles)
    return Program.objects.filter(pk__in=links.values("program"))


def writable_programs(user):
    """Programs the user may change things in: coordinator or program admin."""
    from programs.models import ProgramRole

    return staff_programs(user, ProgramRole.WRITE_ROLES)


def admin_programs(user):
    """Programs the user is a program admin of."""
    from programs.models import ProgramRole

    return staff_programs(user, {ProgramRole.Role.PROGRAM_ADMIN})


def program_of(obj, lookup):
    """Follow a program_lookup path ('event__program') from an instance."""
    value = obj
    for step in lookup.split("__"):
        value = getattr(value, step)
        if value is None:
            return None
    return value


def can_write_in(user, program):
    return user.is_superuser or writable_programs(user).filter(pk=program.pk).exists()


def is_admin_of(user, program):
    return user.is_superuser or admin_programs(user).filter(pk=program.pk).exists()


def get_in_programs_or_404(model, pk, user):
    """Fetch model row pk only if it is in a program the staff user may see."""
    manager = model._default_manager
    if not hasattr(manager, "for_programs"):
        raise ImproperlyConfigured(
            f"{model.__name__} has no for_programs(); it cannot be served to staff by id."
        )
    return get_object_or_404(manager.for_programs(staff_programs(user)), pk=pk)


def program_scoped(model, *, url_kwarg="id", arg_name="obj"):
    """
    Wrap a staff view that takes an object id in its URL.

    The view receives the object, already checked to be in a program the
    signed-in staff member has a role in. Anyone who is not staff gets 403.
    """

    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if not (request.user.is_authenticated and request.user.is_staff):
                raise PermissionDenied
            kwargs[arg_name] = get_in_programs_or_404(model, kwargs.pop(url_kwarg), request.user)
            return view(request, *args, **kwargs)

        wrapper.object_authorization = PROGRAM
        return wrapper

    return decorator


# --- Public routes and the walk ---------------------------------------------------


def public_object(reason):
    """Mark a parameterized route as public on purpose. The reason is required."""
    if not isinstance(reason, str) or not reason.strip():
        raise ImproperlyConfigured("public_object needs a reason.")

    def decorator(view):
        view.object_authorization = PUBLIC
        view.public_reason = reason
        return view

    return decorator


def unmarked_routes(urlconf=None):
    """
    Every route that captures a URL parameter without declaring how the
    object is authorized: owned by the person, scoped to the staff member's
    programs, or public on purpose. Should always be empty.
    """
    found = []

    def walk(patterns, prefix, parent_has_params):
        for entry in patterns:
            has_params = parent_has_params or bool(entry.pattern.regex.groups)
            route = prefix + str(entry.pattern)
            if isinstance(entry, URLResolver):
                if entry.app_name in EXEMPT_APP_NAMES:
                    continue
                walk(entry.url_patterns, route, has_params)
            elif isinstance(entry, URLPattern) and has_params:
                view = entry.callback
                marker = getattr(view, "object_authorization", None) or getattr(
                    getattr(view, "view_class", None), "object_authorization", None
                )
                if marker not in (OWNED, PUBLIC, PROGRAM):
                    found.append(route)

    walk(get_resolver(urlconf).url_patterns, "", False)
    return found
