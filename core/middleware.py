"""
Behind Caddy, gunicorn sees every request arrive over a Unix socket: there
is no client address at all, and the audit log would record nothing (or
127.0.0.1 behind a TCP proxy). Caddy puts the real client address in
X-Forwarded-For, and strips any such header a client sent, so the value
is trustworthy when, and only when, the request came through our Caddy.

Enabled by TRUST_X_FORWARDED_FOR, which prod.py sets and nothing else does:
with it on in a setting where clients reach gunicorn directly, anyone could
pick the address the audit log records.
"""
from django.conf import settings


class ForwardedForMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if getattr(settings, "TRUST_X_FORWARDED_FOR", False):
            forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
            if forwarded:
                # The rightmost address is the one our proxy appended; anything
                # to its left came from upstream and is not trusted.
                client = forwarded.split(",")[-1].strip()
                if client:
                    request.META["REMOTE_ADDR"] = client
        return self.get_response(request)
