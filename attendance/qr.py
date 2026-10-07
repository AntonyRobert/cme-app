"""
QR sign-in: one scan per session, credited as the whole session.

The room shows a code whose URL carries the session id and a token tied
to a thirty-second window; the token is valid for the current window and
the one before, so a photographed code texted to someone at home stops
working within a minute. The page it opens needs the attendee signed in,
so a scan is tied to a Person, never to a typed name.

A magic-link sign-in takes longer than a window, so the scan is remembered
in the browser session (session id and time of the valid scan) and
completed after sign-in. The scan time is what was checked against the
token; the completion only has to happen soon after.
"""
import datetime

from django.conf import settings
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

from audit.log import record
from rounds.models import Session

from .models import AttendanceRecord

WINDOW_SECONDS = 30
# A scan is accepted from a little before the session until a little after.
SCAN_GRACE = datetime.timedelta(minutes=15)
# How long a valid scan waits for the attendee to finish signing in.
PENDING_LIFETIME = datetime.timedelta(minutes=20)
PENDING_KEY = "pending_qr_scan"
TOKEN_LENGTH = 32


def window_at(now=None):
    """The thirty-second window a moment falls in, as an integer."""
    now = now or timezone.now()
    return int(now.timestamp()) // WINDOW_SECONDS


def token_for(session_id, window):
    """HMAC(SECRET_KEY, session id, window), hex, shortened. Not a secret itself."""
    return salted_hmac(
        "attendance.qr", f"{session_id}:{window}", secret=settings.SECRET_KEY
    ).hexdigest()[:TOKEN_LENGTH]


def token_is_valid(session_id, window, token, now=None):
    """True for the current window and the one before it."""
    current = window_at(now)
    if window not in (current, current - 1):
        return False
    return constant_time_compare(token, token_for(session_id, window))


def scan_path(session, window=None):
    window = window_at() if window is None else window
    return reverse("attendance:scan", args=[session.pk, window, token_for(session.pk, window)])


def session_is_open(session, now=None):
    """Scans are accepted from SCAN_GRACE before the session to SCAN_GRACE after."""
    now = now or timezone.now()
    return session.start_at - SCAN_GRACE <= now <= session.end_at + SCAN_GRACE


def current_session(event, now=None):
    """
    The session to show the code for: the one open now, else the next one
    today, else the last one. The display page picks this on each refresh,
    so one page left open on the room laptop follows the morning along.
    """
    now = now or timezone.now()
    sessions = list(event.sessions.order_by("start_at", "position"))
    if not sessions:
        return None
    # Running beats anything else. In a break, the grace after one talk
    # overlaps the grace before the next; people scan on the way in, so
    # the upcoming talk wins over the one that just ended.
    for session in sessions:
        if session.start_at <= now <= session.end_at:
            return session
    for session in sessions:
        if session.start_at > now and session_is_open(session, now):
            return session
    for session in sessions:
        if session_is_open(session, now):
            return session
    for session in sessions:
        if session.start_at > now:
            return session
    return sessions[-1]


def svg_for(url, scale=10):
    """The code as inline SVG markup."""
    import segno

    return segno.make(url, error="m").svg_inline(scale=scale, dark="#000", light=None)


class ScanRefused(Exception):
    """The code is stale or the session is not open; nothing was recorded."""


def check_scan(session_id, window, token, now=None):
    """The Session for a valid, timely scan, or ScanRefused."""
    now = now or timezone.now()
    if not token_is_valid(session_id, window, token, now):
        raise ScanRefused("That code has expired. Scan the one on the screen now.")
    session = Session.objects.filter(pk=session_id).select_related("event__program").first()
    if session is None:
        raise ScanRefused("That code does not belong to a session.")
    if not session_is_open(session, now):
        raise ScanRefused(f"{session.title} is not open for sign-in right now.")
    return session


def record_scan(session, person, *, scanned_at=None, request=None):
    """
    One qr_signin row for this person and session, created if there was
    none. Returns (row, created). The row carries no staff created_by: the
    attendee is the actor, and the audit entry says so.
    """
    existing = (
        AttendanceRecord.objects.active()
        .filter(session=session, person=person, source=AttendanceRecord.Source.QR_SIGNIN)
        .first()
    )
    if existing is not None:
        return existing, False
    row = AttendanceRecord(
        event=session.event,
        session=session,
        person=person,
        source=AttendanceRecord.Source.QR_SIGNIN,
        match_method=AttendanceRecord.MatchMethod.SELF,
        created_by=None,
    )
    row.full_clean()
    row.save()
    record(
        "attendance.qr_scanned",
        row,
        person=person,
        request=request,
        metadata={
            "session": str(session.pk),
            "event": str(session.event_id),
            "scanned_at": (scanned_at or timezone.now()).isoformat(),
        },
    )
    return row, True


# --- The scan that waits for sign-in ------------------------------------------------


def remember_scan(request, session, now=None):
    now = now or timezone.now()
    request.session[PENDING_KEY] = {"session": str(session.pk), "at": now.isoformat()}


def pending_scan(request, now=None):
    """(session, scanned_at) remembered in this browser, if still fresh; else None."""
    now = now or timezone.now()
    data = request.session.get(PENDING_KEY)
    if not data:
        return None
    try:
        scanned_at = datetime.datetime.fromisoformat(data["at"])
        session = Session.objects.select_related("event__program").get(pk=data["session"])
    except (KeyError, ValueError, Session.DoesNotExist):
        request.session.pop(PENDING_KEY, None)
        return None
    if now - scanned_at > PENDING_LIFETIME:
        request.session.pop(PENDING_KEY, None)
        return None
    return session, scanned_at


def forget_scan(request):
    request.session.pop(PENDING_KEY, None)
