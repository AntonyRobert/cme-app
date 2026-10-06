from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models import Q

from core.models import AppendOnlyMixin
from people.models import Person


class AuditLog(AppendOnlyMixin, models.Model):
    """
    What happened that someone might later dispute, and who did it.

    Append-only. Write through audit.log.record(), never directly.
    """

    class ActorType(models.TextChoices):
        STAFF = "staff", "Staff"
        ATTENDEE = "attendee", "Attendee"
        SYSTEM = "system", "System"

    actor_type = models.CharField(max_length=10, choices=ActorType.choices)
    # PROTECT, not SET NULL: an audit row is never updated, so an account
    # with history is deactivated rather than deleted.
    actor_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    actor_person = models.ForeignKey(
        Person, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    actor_label = models.CharField(
        max_length=320,
        help_text="Who it was, in words, as of that moment. Survives a rename.",
    )
    action = models.CharField(max_length=100)
    object_type = models.CharField(max_length=100, blank=True)
    object_id = models.CharField(max_length=64, blank=True)
    metadata = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)
    ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "audit log entry"
        verbose_name_plural = "audit log"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["object_type", "object_id"]),
            models.Index(fields=["action"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(actor_type="staff", actor_user__isnull=False, actor_person__isnull=True)
                | Q(actor_type="attendee", actor_user__isnull=True, actor_person__isnull=False)
                | Q(actor_type="system", actor_user__isnull=True, actor_person__isnull=True),
                name="auditlog_actor_matches_type",
            ),
            models.CheckConstraint(condition=~Q(actor_label=""), name="auditlog_actor_label_set"),
        ]

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.actor_label} {self.action}"
