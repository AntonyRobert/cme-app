# The paper sign-in sheet is typed in from an entry screen, not uploaded as a
# spreadsheet, so only Teams rows carry an upload.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("attendance", "0004_sources_and_signoff"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="attendancerecord",
            name="attendancerecord_upload_iff_teams",
        ),
        migrations.AddConstraint(
            model_name="attendancerecord",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("source", "teams_upload"), ("upload__isnull", False)),
                    models.Q(models.Q(("source", "teams_upload"), _negated=True), ("upload__isnull", True)),
                    _connector="OR",
                ),
                name="attendancerecord_upload_iff_teams",
            ),
        ),
    ]
