"""
Sessions get their own start and end times; events lose their "actual" times.

Existing sessions are given times laid end to end from the event's start,
using the durations they had. Those are a guess, so check any real event
that was entered before this migration.
"""
import datetime

import django.db.models.deletion
from django.db import migrations, models
from django.db.models import F, Q


def lay_out_sessions(apps, schema_editor):
    Session = apps.get_model("rounds", "Session")
    cursor_by_event = {}
    for session in Session.objects.select_related("event").order_by("event_id", "position"):
        start = cursor_by_event.get(session.event_id, session.event.start_at)
        end = start + datetime.timedelta(minutes=session.duration_minutes or 60)
        session.start_at = start
        session.end_at = end
        session.save(update_fields=["start_at", "end_at"])
        cursor_by_event[session.event_id] = end


def restore_durations(apps, schema_editor):
    Session = apps.get_model("rounds", "Session")
    for session in Session.objects.all():
        session.duration_minutes = int((session.end_at - session.start_at).total_seconds()) // 60
        session.save(update_fields=["duration_minutes"])


class Migration(migrations.Migration):
    dependencies = [
        ("rounds", "0001_initial"),
    ]

    operations = [
        migrations.RemoveField(model_name="roundsevent", name="actual_start_at"),
        migrations.RemoveField(model_name="roundsevent", name="actual_end_at"),
        migrations.AlterField(
            model_name="roundsevent",
            name="start_at",
            field=models.DateTimeField(help_text="Every session must fall between these two."),
        ),
        migrations.AlterField(
            model_name="roundsevent",
            name="end_at",
            field=models.DateTimeField(),
        ),
        migrations.AddField(
            model_name="session",
            name="start_at",
            field=models.DateTimeField(null=True),
        ),
        migrations.AddField(
            model_name="session",
            name="end_at",
            field=models.DateTimeField(null=True, blank=True),
        ),
        migrations.RunPython(lay_out_sessions, restore_durations),
        migrations.AlterField(
            model_name="session",
            name="start_at",
            field=models.DateTimeField(),
        ),
        migrations.AlterField(
            model_name="session",
            name="end_at",
            field=models.DateTimeField(
                blank=True, help_text="Blank means one hour after the start."
            ),
        ),
        migrations.RemoveField(model_name="session", name="duration_minutes"),
        migrations.AlterModelOptions(
            name="session",
            options={"ordering": ["event", "start_at", "position"]},
        ),
        migrations.AddConstraint(
            model_name="session",
            constraint=models.CheckConstraint(
                condition=Q(end_at__gt=F("start_at")), name="session_ends_after_start"
            ),
        ),
    ]
