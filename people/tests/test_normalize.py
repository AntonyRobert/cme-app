import pytest

from people.normalize import normalize_domain, normalize_email, normalize_licence


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("12345", "12345"),
        ("01234", "1234"),
        ("000123", "123"),
        (" 12 345 ", "12345"),
        ("12\t345\n", "12345"),
        ("ab123", "AB123"),
        ("0a12", "A12"),
        ("10200", "10200"),  # only leading zeros go
        ("0", "0"),
        ("000", "0"),
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_normalize_licence(raw, expected):
    assert normalize_licence(raw) == expected


def test_normalize_licence_is_idempotent():
    once = normalize_licence(" 00ab 12 ")
    assert normalize_licence(once) == once == "AB12"


def test_normalize_email():
    assert normalize_email("  Marie.Curie@McGill.CA ") == "marie.curie@mcgill.ca"
    assert normalize_email(None) is None


def test_normalize_domain():
    assert normalize_domain(" @MUHC.McGill.ca ") == "muhc.mcgill.ca"
