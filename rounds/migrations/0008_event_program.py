"""
Every event belongs to a program. Existing events go to a first program,
"Emergency Medicine" of the institution named in settings; rename it in
the admin if that is not right.
"""
from decimal import Decimal

import django.core.validators
import django.db.models.deletion
import core.constraints
from django.conf import settings
from django.db import migrations, models

DEFAULT_INSTITUTION = ("McGill University", "mcgill")
DEFAULT_PROGRAM = ("Emergency Medicine", "em")


def assign_default_program(apps, schema_editor):
    RoundsEvent = apps.get_model("rounds", "RoundsEvent")
    if not RoundsEvent.objects.exists():
        return
    Institution = apps.get_model("programs", "Institution")
    Program = apps.get_model("programs", "Program")
    institution, _ = Institution.objects.get_or_create(
        short_name=DEFAULT_INSTITUTION[1], defaults={"name": DEFAULT_INSTITUTION[0]}
    )
    program, _ = Program.objects.get_or_create(
        institution=institution,
        slug=DEFAULT_PROGRAM[1],
        defaults={
            "name": DEFAULT_PROGRAM[0],
            "series_name": settings.SERIES_NAME,
            "attendance_rate_per_hour": Decimal("1.00"),
            "teaching_rate_per_hour": Decimal("1.00"),
            "default_accredited_credits": Decimal("3.00"),
            "accreditation_year_end_month": settings.ACCREDITATION_YEAR_END[0],
            "accreditation_year_end_day": settings.ACCREDITATION_YEAR_END[1],
            "coi_question_version": settings.COI_CURRENT_VERSION,
        },
    )
    RoundsEvent.objects.filter(program__isnull=True).update(program=program)


class Migration(migrations.Migration):
    dependencies = [
        ("programs", "0001_initial"),
        ("rounds", "0007_teams_meeting_title"),
    ]

    operations = [
        migrations.AddField(
            model_name="roundsevent",
            name="program",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="events",
                to="programs.program",
            ),
        ),
        migrations.RunPython(assign_default_program, migrations.RunPython.noop),
    ]
