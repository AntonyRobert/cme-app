# Separate from the data step: Postgres refuses to ALTER a table with pending
# trigger events in the same transaction.
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("credits", "0006_standard_form"),
    ]

    operations = [
        migrations.AlterField(
            model_name="evaluationsubmission",
            name="form_version",
            field=models.ForeignKey(
                help_text="The questions, as worded when this was answered. Never re-pointed.",
                on_delete=django.db.models.deletion.PROTECT,
                related_name="submissions",
                to="credits.evaluationformversion",
            ),
        ),
    ]
