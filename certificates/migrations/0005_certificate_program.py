"""One certificate per program, with the program's rates snapshotted on every line."""
import django.db.models.deletion
from django.db import migrations, models


def assign_program(apps, schema_editor):
    Certificate = apps.get_model("certificates", "Certificate")
    for certificate in Certificate.objects.filter(program__isnull=True):
        line = certificate.lines.select_related("event__program__institution").first()
        if line is None:
            raise RuntimeError(
                f"Certificate {certificate.pk} has no lines and no program; assign one by hand."
            )
        program = line.event.program
        certificate.program = program
        certificate.program_name = program.name
        certificate.institution_name = program.institution.name
        certificate.save(update_fields=["program", "program_name", "institution_name"])


class Migration(migrations.Migration):
    dependencies = [
        ("programs", "0001_initial"),
        ("rounds", "0009_event_program_required"),
        ("certificates", "0004_alter_certificate_attendance_credits_and_more"),
    ]

    operations = [
        migrations.AddField(
            "certificate", "program",
            models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="certificates",
                to="programs.program",
            ),
        ),
        migrations.AddField(
            "certificate", "program_name",
            models.CharField(default="", help_text="Snapshotted. Programs get renamed.", max_length=200),
            preserve_default=False,
        ),
        migrations.AddField(
            "certificate", "institution_name",
            models.CharField(default="", help_text="Snapshotted.", max_length=200),
            preserve_default=False,
        ),
        migrations.RunPython(assign_program, migrations.RunPython.noop),
    ]
