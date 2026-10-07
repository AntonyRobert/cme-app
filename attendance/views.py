"""
Attendee-facing QR sign-in. Staff screens are in admin.py.
"""
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET

from core.authz import public_object
from signin.views import signed_in

from . import qr


@require_GET
@public_object("the token in the URL is the credential; it rotates every thirty seconds")
def scan(request, session_id, window, token):
    try:
        session = qr.check_scan(session_id, window, token)
    except qr.ScanRefused as refused:
        return render(request, "attendance/scan_refused.html", {"reason": str(refused)}, status=410)
    if request.person is not None:
        row, created = qr.record_scan(session, request.person, request=request)
        return render(
            request,
            "attendance/scan_done.html",
            {"session": session, "person": request.person, "created": created},
        )
    # Valid scan, nobody signed in: remember it, sign them in, then finish.
    qr.remember_scan(request, session)
    return redirect(f"{reverse('signin:start')}?next={reverse('attendance:scan_complete')}")


@require_GET
@signed_in
def scan_complete(request):
    """After sign-in: record the scan remembered in this browser, if any."""
    pending = qr.pending_scan(request)
    if pending is None:
        return redirect("signin:me")
    session, scanned_at = pending
    qr.forget_scan(request)
    row, created = qr.record_scan(session, request.person, scanned_at=scanned_at, request=request)
    return render(
        request,
        "attendance/scan_done.html",
        {"session": session, "person": request.person, "created": created},
    )
