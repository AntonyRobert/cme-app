"""Certificate rules that might change. One function each."""
import secrets
from decimal import ROUND_HALF_UP, Decimal

from people.models import Person

# No 0/O, 1/I/L or U: characters people misread off a printed page.
VERIFICATION_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
VERIFICATION_GROUPS = 3
VERIFICATION_GROUP_LENGTH = 4

CME = "cme"
ATTENDANCE = "attendance"


def generate_verification_code():
    """A random, unguessable code such as 7KQ2-M9XA-4RTP. Never sequential."""
    return "-".join(
        "".join(secrets.choice(VERIFICATION_ALPHABET) for _ in range(VERIFICATION_GROUP_LENGTH))
        for _ in range(VERIFICATION_GROUPS)
    )


def certificate_total(line_credits):
    """
    The credit total printed on a certificate: the year's exact credits
    added up and rounded to the nearest whole credit, halves up.

    This is the only place credit is ever rounded. The lines keep their
    exact figures, so they will not always add up to the printed total.
    """
    exact = sum((Decimal(value) for value in line_credits), Decimal("0"))
    return max(exact, Decimal("0")).quantize(Decimal("1"), rounding=ROUND_HALF_UP).quantize(
        Decimal("0.00")
    )


def certificate_type_for(person):
    """
    Which certificate a person gets.

    Physicians get a CME credit certificate. Everyone else, trainees
    included, gets an attendance certificate. The trainee case is waiting
    on McGill's CPD office (docs/decisions.md).
    """
    return CME if person.role == Person.Role.PHYSICIAN else ATTENDANCE
