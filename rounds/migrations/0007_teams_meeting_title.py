"""Teams exports carry no meeting ID; events are matched on title plus date."""
from django.db import migrations, models


def blank_nulls(apps, schema_editor):
    apps.get_model("rounds", "RoundsEvent").objects.filter(teams_meeting_title__isnull=True).update(
        teams_meeting_title=""
    )


class Migration(migrations.Migration):
    dependencies = [("rounds", "0006_deferred_position_constraints")]

    operations = [
        migrations.RenameField("roundsevent", "teams_meeting_id", "teams_meeting_title"),
        migrations.RunPython(blank_nulls, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="roundsevent",
            name="teams_meeting_title",
            field=models.CharField(
                blank=True,
                help_text="The meeting title exactly as Teams shows it. An attendance export is "
                "matched to this event on this title plus the date. Teams exports carry no meeting ID.",
                max_length=300,
            ),
        ),
    ]
