"""
Canonical forms used for matching.

What a person typed is stored unchanged; these produce the copy that
comparisons and uniqueness run on. One function per rule, so a change of
rule happens in one place.
"""


def normalize_licence(raw):
    """
    Matching form of a licence number: no whitespace, no leading zeros, uppercase.

    Returns None when there is nothing to match on. Never print the result:
    certificates show the number as entered.
    """
    if raw is None:
        return None
    compact = "".join(raw.split()).upper()
    if not compact:
        return None
    # An all-zero number keeps one digit rather than collapsing to nothing.
    return compact.lstrip("0") or "0"


def normalize_email(raw):
    """Stored form of an email address: trimmed and lowercased."""
    if raw is None:
        return None
    return raw.strip().lower()


def normalize_domain(raw):
    """Stored form of an email domain: trimmed, lowercased, no leading @."""
    if raw is None:
        return None
    return raw.strip().lower().lstrip("@")
