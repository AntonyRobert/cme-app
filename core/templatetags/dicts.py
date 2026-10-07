from django import template

register = template.Library()


@register.filter
def get_item(mapping, key):
    """{{ mapping|get_item:key }} for keys that are not plain strings (sessions, ids)."""
    if not hasattr(mapping, "get"):  # None, or an undefined variable rendered as ""
        return None
    return mapping.get(key)


@register.filter
def claim_label(claim):
    """A claim group's name for the screen: 'recorded', 'sign-in sheet', 'QR sign-in'."""
    from attendance.aggregation import CLAIM_LABELS

    return CLAIM_LABELS.get(claim, claim)


@register.filter
def yesno_label(value, question_key=None):
    """A trigger value for the screen: 0/1 read as no/yes, anything else as itself."""
    return {"0": "no", "1": "yes"}.get(str(value), str(value))
