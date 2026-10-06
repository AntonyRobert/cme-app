from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import DecimalField, F, Value
from django.db.models.functions import Mod
from django.db.models.lookups import Exact

QUARTER = Decimal("0.25")

_decimal = DecimalField(max_digits=5, decimal_places=2)


def is_quarter_multiple(field_name):
    """Check-constraint condition: the field is a whole number of quarter credits."""
    return Exact(
        Mod(F(field_name), Value(QUARTER, output_field=_decimal), output_field=_decimal),
        Value(Decimal("0"), output_field=_decimal),
    )


def validate_quarter_multiple(value):
    """Form-level twin of is_quarter_multiple, so the admin shows a message instead of a 500."""
    if value is not None and value % QUARTER != 0:
        raise ValidationError("Credits go in steps of 0.25.")
