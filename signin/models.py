"""
Magic-link sign-in for attendees and presenters.

There are no passwords. A person types their email address, gets a link,
and the link signs them in. The link is the entire credential, so the
token is treated the way a password hash would be: generated with
secrets, stored only as a hash, single use, fifteen minutes, bound to the
address it was sent to.
"""
import hashlib
import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone

from core.models import UUIDModel

TOKEN_LIFETIME_MINUTES = 15


def hash_token(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()


class MagicLinkToken(UUIDModel):
    """One sign-in link. The token itself is never stored; only its hash."""

    email = models.EmailField(max_length=254, help_text="The address the link was sent to.")
    token_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)
    requested_ip = models.GenericIPAddressField(null=True, blank=True)
    requested_ua = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["email", "created_at"])]

    def __str__(self):
        return f"link for {self.email} ({'used' if self.consumed_at else 'unused'})"

    @classmethod
    def issue(cls, email, *, ip=None, user_agent=""):
        """Create a token. Returns (row, the one-time token to put in the link)."""
        token = secrets.token_urlsafe(32)
        row = cls.objects.create(
            email=email,
            token_hash=hash_token(token),
            expires_at=timezone.now() + timezone.timedelta(minutes=TOKEN_LIFETIME_MINUTES),
            requested_ip=ip,
            requested_ua=(user_agent or "")[:300],
        )
        return row, token

    def is_usable(self, at=None):
        at = at or timezone.now()
        return self.consumed_at is None and at < self.expires_at
