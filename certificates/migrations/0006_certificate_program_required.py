"""Second half: the program FK becomes required; lines snapshot the rates."""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("certificates", "0005_certificate_program")]

    operations = [
        migrations.AlterField(
            "certificate", "program",
            models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="certificates",
                to="programs.program",
            ),
        ),
        migrations.AddField(
            "certificateline", "attendance_rate_per_hour",
            models.DecimalField(decimal_places=2, default=1, max_digits=4),
        ),
        migrations.AddField(
            "certificateline", "teaching_rate_per_hour",
            models.DecimalField(decimal_places=2, default=1, max_digits=4),
        ),
    ]
