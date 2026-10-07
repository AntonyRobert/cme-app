# Every program gets the provisional "Standard CME evaluation" as its default,
# and every existing submission is recorded against that form's v1: the
# questions it answered (objective ratings, overall) predate templates, and
# v1 is the closest statement of what was asked.
from django.db import migrations

from credits.evaluation_forms import STANDARD_FORM_NAME, STANDARD_QUESTIONS


def seed(apps, schema_editor):
    Program = apps.get_model("programs", "Program")
    EvaluationForm = apps.get_model("credits", "EvaluationForm")
    EvaluationFormVersion = apps.get_model("credits", "EvaluationFormVersion")
    EvaluationQuestion = apps.get_model("credits", "EvaluationQuestion")
    EvaluationSubmission = apps.get_model("credits", "EvaluationSubmission")
    for program in Program.objects.all():
        form, created = EvaluationForm.objects.get_or_create(
            program=program, name=STANDARD_FORM_NAME, defaults={"status": "active"}
        )
        if created:
            version = EvaluationFormVersion.objects.create(
                form=form, number=1, note="Provisional, pending McGill CPD."
            )
            for position, spec in enumerate(STANDARD_QUESTIONS, start=1):
                EvaluationQuestion.objects.create(
                    version=version,
                    position=position,
                    question_key=spec["question_key"],
                    kind=str(spec["kind"]),
                    required=spec["required"],
                    prompt=spec["prompt"],
                )
        else:
            version = form.versions.order_by("-number").first()
        if program.default_evaluation_form_id is None:
            program.default_evaluation_form = form
            program.save(update_fields=["default_evaluation_form"])
        EvaluationSubmission.objects.filter(
            session__event__program=program, form_version__isnull=True
        ).update(form_version=version)


def unseed(apps, schema_editor):
    EvaluationSubmission = apps.get_model("credits", "EvaluationSubmission")
    EvaluationSubmission.objects.update(form_version=None)
    Program = apps.get_model("programs", "Program")
    Program.objects.update(default_evaluation_form=None)


class Migration(migrations.Migration):

    dependencies = [
        ("credits", "0005_evaluation_forms"),
        ("programs", "0002_evaluation_forms"),
    ]

    operations = [migrations.RunPython(seed, unseed)]
