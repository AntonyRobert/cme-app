"""
The sign-in flow, as functions. The views are thin.

    request_link(email, ...)      -> sends a link, or refuses (rate limit)
    redeem(token, ...)            -> Redemption: who it was for, and what next
    sign_in(request, person)      -> the session now belongs to this person
    sign_out_everywhere(person)   -> every session of theirs stops working
"""
import datetime
from dataclasses import dataclass

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from audit.log import record
from people.identity import resolve_root
from people.models import AllowedDomain, Person, PersonEmail, SignInRequest
from people.normalize import normalize_email

from .models import MagicLinkToken, hash_token

# Rate limits. Without them this is a mailer that sends from our domain to
# anyone, and SES will suspend the account for it.
LINKS_PER_EMAIL_PER_HOUR = 5
LINKS_PER_IP_PER_HOUR = 20

SESSION_PERSON_KEY = "person_id"
SESSION_SIGNED_IN_AT_KEY = "person_signed_in_at"
SESSION_PENDING_EMAIL_KEY = "pending_email"
SESSION_AGE = datetime.timedelta(days=90)


class RateLimited(Exception):
    """Too many links for this address or from this network recently."""


def _hour_ago():
    return timezone.now() - datetime.timedelta(hours=1)


def request_link(email, *, build_url, ip=None, user_agent="", request=None):
    """
    Send a sign-in link to `email`. Always behaves the same for a known and
    an unknown address, so nobody can learn who is on file by asking.
    `build_url(token)` turns the one-time token into the absolute link.
    """
    email = normalize_email(email)
    recent = MagicLinkToken.objects.filter(created_at__gte=_hour_ago())
    if recent.filter(email=email).count() >= LINKS_PER_EMAIL_PER_HOUR:
        raise RateLimited("address")
    if ip and recent.filter(requested_ip=ip).count() >= LINKS_PER_IP_PER_HOUR:
        raise RateLimited("network")

    row, token = MagicLinkToken.issue(email, ip=ip, user_agent=user_agent)
    url = build_url(token)
    send_mail(
        subject=f"Your sign-in link for {settings.SERIES_NAME}",
        message=(
            "Use this link to sign in. It works once and for the next 15 minutes.\n\n"
            f"{url}\n\n"
            "If you did not ask for it, ignore this message; nothing happens unless the "
            "link is opened."
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[email],
    )
    record(
        "signin.link_sent",
        row,
        system="signin",
        request=request,
        metadata={"email": email},
    )
    return row


@dataclass(frozen=True)
class Redemption:
    email: str
    person: Person | None  # known person, signed in
    needs_profile: bool = False  # allowed domain, nobody on file yet: ask their name
    queued: bool = False  # outside the allowed domains: waiting for review


def domain_of(email):
    return email.rsplit("@", 1)[-1]


def admission_for(email):
    """auto, review, or None when the domain is not allowed at all."""
    domain = domain_of(email)
    allowed = AllowedDomain.objects.filter(domain=domain).first()
    if allowed is None:
        return None
    return "auto" if allowed.auto_admit else "review"


@transaction.atomic
def redeem(token, *, request=None):
    """
    Turn a token from a link into a Redemption, or None when the link is
    unusable (unknown, expired or already used; the caller shows the same
    page for all three).
    """
    row = (
        MagicLinkToken.objects.select_for_update()
        .filter(token_hash=hash_token(token or ""))
        .first()
    )
    if row is None or not row.is_usable():
        return None
    row.consumed_at = timezone.now()
    row.save(update_fields=["consumed_at"])

    link = PersonEmail.objects.select_related("person").filter(email=row.email).first()
    if link is not None:
        person = resolve_root(link.person)
        if link.verified_at is None:
            link.verified_at = timezone.now()
            link.save(update_fields=["verified_at"])
        return Redemption(email=row.email, person=person)

    admission = admission_for(row.email)
    if admission == "auto":
        return Redemption(email=row.email, person=None, needs_profile=True)
    SignInRequest.objects.get_or_create(
        email=row.email,
        status=SignInRequest.Status.PENDING,
        defaults={"given_name": "", "family_name": ""},
    )
    record(
        "signin_request.created",
        system="signin",
        request=request,
        metadata={"email": row.email, "domain": domain_of(row.email), "allowed": admission},
    )
    return Redemption(email=row.email, person=None, queued=True)


@transaction.atomic
def create_person_for(email, *, given_name, family_name, role, request=None):
    """A verified address on an allowed domain, with no one on file: make them."""
    person = Person.objects.create(given_name=given_name, family_name=family_name, role=role)
    PersonEmail.objects.create(person=person, email=email, is_primary=True, verified_at=timezone.now())
    record("person.self_registered", person, person=person, request=request, metadata={"email": email})
    return person


def sign_in(request, person):
    """This session now belongs to `person`: new session key, 90 days."""
    request.session.cycle_key()  # keeps the data, changes the key
    request.session[SESSION_PERSON_KEY] = str(person.pk)
    request.session[SESSION_SIGNED_IN_AT_KEY] = timezone.now().isoformat()
    request.session.set_expiry(int(SESSION_AGE.total_seconds()))
    request.session.pop(SESSION_PENDING_EMAIL_KEY, None)
    record("attendee.signed_in", person, person=person, request=request)


def sign_out(request):
    request.session.flush()


def sign_out_everywhere(person, *, request=None):
    """
    Every session of this person stops working, including this one.
    Sessions carry the time they were signed in; the middleware rejects
    any signed in before this moment.
    """
    Person.objects.filter(pk=person.pk).update(sessions_revoked_at=timezone.now())
    record("attendee.signed_out_everywhere", person, person=person, request=request)
    if request is not None:
        request.session.flush()


def person_from_session(session):
    """The Person a session belongs to, or None. Honours sign-out-everywhere."""
    person_id = session.get(SESSION_PERSON_KEY)
    if not person_id:
        return None
    person = Person.objects.filter(pk=person_id).select_related("merged_into").first()
    if person is None:
        return None
    signed_in_at = session.get(SESSION_SIGNED_IN_AT_KEY)
    if person.sessions_revoked_at and (
        not signed_in_at
        or datetime.datetime.fromisoformat(signed_in_at) < person.sessions_revoked_at
    ):
        return None
    return resolve_root(person)
