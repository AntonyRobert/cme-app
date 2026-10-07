"""
COI declarations become a questionnaire: one COIResponse per question.

Existing declarations, made under the single-checkbox form, are converted
to the 2026-10 questionnaire: a "no conflict" becomes an explicit no to
every question; a "conflict" becomes a yes under "other" with the details
as given, and no to the rest. valid_until goes; validity is now a year
from declared_at.
"""
import uuid

import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Q

CONVERTED_VERSION = "2026-10"
CONVERTED_QUESTIONS = [
    "research_funding",
    "consulting",
    "speaker_fees",
    "equity",
    "employment",
    "intellectual_property",
    "other",
]


def convert_declarations(apps, schema_editor):
    COIDeclaration = apps.get_model("rounds", "COIDeclaration")
    COIResponse = apps.get_model("rounds", "COIResponse")
    for declaration in COIDeclaration.objects.all():
        for key in CONVERTED_QUESTIONS:
            yes = declaration.has_conflict and key == "other"
            COIResponse.objects.create(
                declaration=declaration,
                question_key=key,
                has_conflict=yes,
                details=declaration.details if yes else "",
            )
        declaration.disclosure_text_version = CONVERTED_VERSION
        declaration.save(update_fields=["disclosure_text_version"])


def unconvert_declarations(apps, schema_editor):
    COIDeclaration = apps.get_model("rounds", "COIDeclaration")
    for declaration in COIDeclaration.objects.prefetch_related("responses"):
        yes = [r for r in declaration.responses.all() if r.has_conflict]
        declaration.has_conflict = bool(yes)
        declaration.details = "; ".join(r.details for r in yes)
        declaration.save(update_fields=["has_conflict", "details"])


class Migration(migrations.Migration):
    dependencies = [
        ("rounds", "0004_blank_positions"),
    ]

    operations = [
        migrations.CreateModel(
            name="COIResponse",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("question_key", models.CharField(max_length=50)),
                ("has_conflict", models.BooleanField()),
                ("details", models.TextField(blank=True, help_text="Required when the answer is yes.")),
                (
                    "declaration",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="responses",
                        to="rounds.coideclaration",
                    ),
                ),
            ],
            options={
                "verbose_name": "COI response",
                "ordering": ["declaration", "question_key"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=["declaration", "question_key"], name="coiresponse_one_per_question"
                    ),
                    models.CheckConstraint(
                        condition=Q(has_conflict=False) | ~Q(details=""),
                        name="coiresponse_yes_needs_details",
                    ),
                ],
            },
        ),
        migrations.RunPython(convert_declarations, unconvert_declarations),
        migrations.RemoveConstraint(
            model_name="coideclaration", name="coideclaration_conflict_needs_details"
        ),
        migrations.RemoveField(model_name="coideclaration", name="has_conflict"),
        migrations.RemoveField(model_name="coideclaration", name="details"),
        migrations.RemoveField(model_name="coideclaration", name="valid_until"),
        migrations.AlterField(
            model_name="coideclaration",
            name="disclosure_text_version",
            field=models.CharField(
                help_text="Which questionnaire was answered. The declaration always shows that "
                "version's wording.",
                max_length=50,
            ),
        ),
    ]
