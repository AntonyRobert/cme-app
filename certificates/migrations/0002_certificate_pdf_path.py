from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("certificates", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="certificate",
            name="pdf_path",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Relative to UPLOAD_ROOT. The artefact of record.",
                max_length=500,
            ),
            preserve_default=False,
        ),
    ]
