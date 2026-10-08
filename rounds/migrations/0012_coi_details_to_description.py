# Legacy declarations carried one explanation per yes; the National Standard
# form has two columns. The old text is the description of the relationship.
from django.db import migrations


def forwards(apps, schema_editor):
    COIResponse = apps.get_model("rounds", "COIResponse")
    for response in COIResponse.objects.exclude(details=""):
        response.relationship_description = response.details
        response.save(update_fields=["relationship_description"])


def backwards(apps, schema_editor):
    COIResponse = apps.get_model("rounds", "COIResponse")
    for response in COIResponse.objects.exclude(relationship_description=""):
        response.details = response.relationship_description
        response.save(update_fields=["details"])


class Migration(migrations.Migration):

    dependencies = [("rounds", "0011_national_standard_fields")]

    operations = [migrations.RunPython(forwards, backwards)]
