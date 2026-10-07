"""
Hours-only rows (no join/leave times) now say which session they belong
to. Existing ones are assigned to the first session of their event; check
any real rows entered before this migration.
"""
import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Q


def assign_first_session(apps, schema_editor):
    AttendanceRecord = apps.get_model("attendance", "AttendanceRecord")
    Session = apps.get_model("rounds", "Session")
    for row in AttendanceRecord.objects.filter(join_at__isnull=True, session__isnull=True):
        first = Session.objects.filter(event_id=row.event_id).order_by("start_at", "position").first()
        if first is None:
            raise RuntimeError(
                f"Attendance row {row.pk} has no times and its event has no sessions. "
                "Add a session to the event, then migrate again."
            )
        row.session = first
        row.save(update_fields=["session"])


class Migration(migrations.Migration):
    dependencies = [
        ("attendance", "0001_initial"),
        ("rounds", "0002_session_times"),
    ]

    operations = [
        migrations.AddField(
            model_name="attendancerecord",
            name="session",
            field=models.ForeignKey(
                blank=True,
                help_text="Only for a row without join and leave times: which session the "
                "minutes belong to. Timed rows are matched to sessions by their times.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="attendance_records",
                to="rounds.session",
            ),
        ),
        migrations.RunPython(assign_first_session, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="attendancerecord",
            constraint=models.CheckConstraint(
                condition=Q(join_at__isnull=False) | Q(session__isnull=False),
                name="attendancerecord_hours_only_needs_session",
            ),
        ),
    ]
