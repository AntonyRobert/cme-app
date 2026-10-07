"""URL configurations used only by test_authz.py."""
from django.http import HttpResponse
from django.urls import include, path

from core.authz import owned_object, program_scoped, public_object
from people.models import PersonEmail
from rounds.models import RoundsEvent


def plain(request, **kwargs):
    return HttpResponse("ok")


@owned_object(PersonEmail)
def email_detail(request, obj):
    return HttpResponse(obj.email)


@public_object("verification codes are meant to be looked up by anyone")
def verify(request, code):
    return HttpResponse(code)


@program_scoped(RoundsEvent)
def event_detail(request, obj):
    return HttpResponse(obj.title)


good_patterns = [
    path("no-params/", plain),
    path("emails/<uuid:id>/", email_detail),
    path("staff/events/<uuid:id>/", event_detail),
    path("verify/<str:code>/", verify),
]

bad_nested = [path("detail/", plain), path("<uuid:id>/edit/", plain)]

bad_patterns = [
    path("emails/<uuid:id>/", plain),
    path("people/<uuid:person_id>/", include(bad_nested)),
]


class Good:
    urlpatterns = good_patterns


class Bad:
    urlpatterns = bad_patterns
