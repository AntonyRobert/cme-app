# Every program moves to the National Standard disclosure form. The old
# version stays in settings so existing declarations render as made; a
# program that must stay on it can set coi_question_version back by hand.
from django.db import migrations


def forwards(apps, schema_editor):
    Program = apps.get_model("programs", "Program")
    Program.objects.filter(coi_question_version="2026-10").update(
        coi_question_version="v2026-national-standard"
    )


def backwards(apps, schema_editor):
    Program = apps.get_model("programs", "Program")
    Program.objects.filter(coi_question_version="v2026-national-standard").update(
        coi_question_version="2026-10"
    )


class Migration(migrations.Migration):

    dependencies = [("programs", "0003_evaluation_gate_and_statement")]

    operations = [migrations.RunPython(forwards, backwards)]
