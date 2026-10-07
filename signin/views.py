"""
The attendee-facing pages: sign in by email, the credits page, sign out.

No page here takes an id in its URL except the link itself, whose token is
the credential. The credits page shows the signed-in person's own credit
and nothing else.
"""
from django import forms
from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST

from core.authz import public_object
from credits.windows import (
    STATE_CAN_REQUEST,
    STATE_OPEN,
    STATE_REOPENED,
    ReopeningRefused,
    request_reopening,
    sessions_needing_evaluation,
)
from people.models import Person
from rounds.models import Session

from . import services


class EmailForm(forms.Form):
    email = forms.EmailField(label="Your email address", max_length=254)


class ProfileForm(forms.Form):
    given_name = forms.CharField(label="Given name", max_length=200)
    family_name = forms.CharField(label="Family name", max_length=200)
    role = forms.ChoiceField(label="You are a", choices=Person.Role.choices)


def signed_in(view):
    """For attendee pages: send anyone not signed in to the sign-in page."""

    def wrapper(request, *args, **kwargs):
        if request.person is None:
            return redirect(f"{reverse('signin:start')}?next={request.get_full_path()}")
        return view(request, *args, **kwargs)

    wrapper.__name__ = view.__name__
    return wrapper


@require_http_methods(["GET", "POST"])
def start(request):
    form = EmailForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"]
        try:
            services.request_link(
                email,
                build_url=lambda token: request.build_absolute_uri(
                    reverse("signin:redeem", args=[token])
                ),
                ip=request.META.get("REMOTE_ADDR"),
                user_agent=request.META.get("HTTP_USER_AGENT", ""),
                request=request,
            )
        except services.RateLimited:
            form.add_error(
                None, "Too many links have been requested recently. Please try again later."
            )
        else:
            request.session[services.SESSION_PENDING_EMAIL_KEY] = services.normalize_email(email)
            return redirect("signin:sent")
    return render(request, "signin/start.html", {"form": form})


def sent(request):
    return render(
        request, "signin/sent.html", {"email": request.session.get(services.SESSION_PENDING_EMAIL_KEY)}
    )


@public_object("the token in the URL is the credential; it is single-use and expires")
def redeem(request, token):
    redemption = services.redeem(token, request=request)
    if redemption is None:
        return render(request, "signin/invalid.html", status=410)
    if redemption.person is not None:
        services.sign_in(request, redemption.person)
        return redirect("signin:me")
    if redemption.needs_profile:
        request.session[services.SESSION_PENDING_EMAIL_KEY] = redemption.email
        request.session["verified_email"] = redemption.email
        return redirect("signin:complete")
    return render(request, "signin/queued.html", {"email": redemption.email})


@require_http_methods(["GET", "POST"])
def complete(request):
    email = request.session.get("verified_email")
    if not email:
        return redirect("signin:start")
    form = ProfileForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        person = services.create_person_for(email, request=request, **form.cleaned_data)
        request.session.pop("verified_email", None)
        services.sign_in(request, person)
        return redirect("signin:me")
    return render(request, "signin/complete.html", {"form": form, "email": email})


@require_POST
def sign_out(request):
    services.sign_out(request)
    return render(request, "signin/signed_out.html")


@require_POST
@signed_in
def sign_out_everywhere(request):
    services.sign_out_everywhere(request.person, request=request)
    return render(request, "signin/signed_out.html", {"everywhere": True})


# --- The credits page -----------------------------------------------------------


@signed_in
def me(request):
    from credits.reports import person_standing_by_program

    programs = person_standing_by_program(request.person)
    to_evaluate = []
    for program in programs:
        for standing in program.standings:
            for session, state in sessions_needing_evaluation(request.person, standing.event):
                to_evaluate.append((standing.event, session, state))
    return render(
        request,
        "signin/me.html",
        {
            "person": request.person,
            "programs": programs,
            "to_evaluate": to_evaluate,
            "STATE_OPEN": STATE_OPEN,
            "STATE_REOPENED": STATE_REOPENED,
            "STATE_CAN_REQUEST": STATE_CAN_REQUEST,
        },
    )


@require_POST
@signed_in
def reopen(request):
    session_id = request.POST.get("session")
    session = Session.objects.filter(pk=session_id).first() if session_id else None
    if session is None:
        return redirect("signin:me")
    try:
        window = request_reopening(
            request.person, session, reason="Requested from the credits page", request=request
        )
    except ReopeningRefused as refused:
        messages.error(request, str(refused))
    else:
        messages.success(
            request, f"The form for {session.title} is open again until {window.expires_at:%d %B}."
        )
    return redirect("signin:me")
