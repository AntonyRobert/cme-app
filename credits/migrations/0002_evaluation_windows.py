import uuid

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models
from django.db.models import F, Q


class Migration(migrations.Migration):
    dependencies = [
        ("credits", "0001_initial"),
        ("people", "0002_alter_person_options"),
        ("rounds", "0002_session_times"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RenameField(
            model_name="evaluationsubmission",
            old_name="self_reported_minutes",
            new_name="self_reported_session_minutes",
        ),
        migrations.AlterField(
            model_name="evaluationsubmission",
            name="self_reported_session_minutes",
            field=models.PositiveSmallIntegerField(
                help_text="How long they say they attended this session. Only used when "
                "there is no attendance record."
            ),
        ),
        migrations.CreateModel(
            name="EvaluationWindow",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("opened_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("expires_at", models.DateTimeField()),
                ("reason", models.TextField(blank=True)),
                (
                    "closed_at",
                    models.DateTimeField(
                        blank=True, help_text="Set when a complete evaluation is submitted.", null=True
                    ),
                ),
                (
                    "granted_by",
                    models.ForeignKey(
                        blank=True,
                        help_text="Blank when the attendee asked for it themselves.",
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "person",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="evaluation_windows",
                        to="people.person",
                    ),
                ),
                (
                    "session",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="evaluation_windows",
                        to="rounds.session",
                    ),
                ),
            ],
            options={
                "ordering": ["-opened_at"],
                "constraints": [
                    models.CheckConstraint(
                        condition=Q(expires_at__gt=F("opened_at")),
                        name="evaluationwindow_expires_later",
                    ),
                ],
            },
        ),
    ]
