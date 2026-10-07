"""
Certificates carry attendance and teaching credit separately, never one
blended figure. Existing figures were all attendance.
"""
from django.db import migrations, models
from django.db.models import F, Q


def existing_were_attendance(apps, schema_editor):
    Certificate = apps.get_model("certificates", "Certificate")
    Certificate.objects.update(attendance_credits=F("total_credits"), teaching_credits=0)


class Migration(migrations.Migration):
    dependencies = [("certificates", "0002_certificate_pdf_path")]

    operations = [
        migrations.RemoveConstraint("certificateline", "certificateline_credits_not_negative"),
        migrations.RenameField("certificateline", "credits", "attendance_credits"),
        migrations.RenameField("certificateline", "computed_credits", "attendance_computed"),
        migrations.RenameField("certificateline", "adjustment_credits", "attendance_adjustment"),
        migrations.AddField(
            "certificateline", "presented_session_titles",
            models.JSONField(default=list, help_text="Print-only snapshot."),
        ),
        migrations.AddField("certificateline", "teaching_minutes", models.PositiveIntegerField(default=0)),
        migrations.AddField(
            "certificateline", "teaching_computed",
            models.DecimalField(decimal_places=2, default=0, max_digits=5),
        ),
        migrations.AddField(
            "certificateline", "teaching_adjustment",
            models.DecimalField(decimal_places=2, default=0, max_digits=5),
        ),
        migrations.AddField(
            "certificateline", "teaching_credits",
            models.DecimalField(decimal_places=2, default=0, max_digits=5),
        ),
        migrations.AddConstraint(
            "certificateline",
            models.CheckConstraint(
                condition=Q(attendance_credits__gte=0, teaching_credits__gte=0),
                name="certificateline_credits_not_negative",
            ),
        ),
        migrations.AddField(
            "certificate", "attendance_credits",
            models.DecimalField(decimal_places=2, default=0, max_digits=6),
            preserve_default=False,
        ),
        migrations.AddField(
            "certificate", "teaching_credits",
            models.DecimalField(decimal_places=2, default=0, max_digits=6),
            preserve_default=False,
        ),
        migrations.RunPython(existing_were_attendance, migrations.RunPython.noop),
        migrations.AddConstraint(
            "certificate",
            models.CheckConstraint(
                condition=Q(total_credits=F("attendance_credits") + F("teaching_credits")),
                name="certificate_total_is_sum_of_kinds",
            ),
        ),
    ]
