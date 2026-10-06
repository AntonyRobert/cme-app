"""
Object-level authorization.

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

The admin is exempt: it has its own permission system and is staff-only.
"""
from functools import wraps

from django.core.exceptions import ImproperlyConfigured
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.urls import URLPattern, URLResolver, get_resolver

OWNED = "owned"
PUBLIC = "public"

EXEMPT_APP_NAMES = {"admin"}


def current_person(request):
    """
    The signed-in Person, or 404.

    Sign-in doesn't exist yet. When it does, its middleware sets
    request.person, and this stays the only place views read it.
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
    object is authorized. Should always be empty.
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
                if marker not in (OWNED, PUBLIC):
                    found.append(route)

    walk(get_resolver(urlconf).url_patterns, "", False)
    return found
