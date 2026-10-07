"""Second half of the program move: the FK becomes required, title and credits optional."""
from decimal import Decimal

import core.constraints
import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("rounds", "0008_event_program")]

    operations = [
        migrations.AlterField(
            model_name="roundsevent",
            name="program",
            field=models.ForeignKey(
                help_text="Decides the credit rates, the series name and whose certificate this "
                "ends up on.",
                on_delete=django.db.models.deletion.PROTECT,
                related_name="events",
                to="programs.program",
            ),
        ),
        migrations.AlterField(
            model_name="roundsevent",
            name="title",
            field=models.CharField(
                blank=True,
                help_text="Blank means the program's series name. Prints on certificate lines.",
                max_length=200,
            ),
        ),
        migrations.AlterField(
            model_name="roundsevent",
            name="accredited_credits",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="The accreditor-set ceiling on attendance credit for this event, in "
                "steps of 0.25. Blank means the program's default.",
                max_digits=5,
                validators=[
                    django.core.validators.MinValueValidator(Decimal("0")),
                    core.constraints.validate_quarter_multiple,
                ],
            ),
        ),
    ]
