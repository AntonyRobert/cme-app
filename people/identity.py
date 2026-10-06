"""
Following merges.

A merged duplicate points at its survivor through Person.merged_into.
These two functions are the only code that walks that link.
"""
from .models import Person

# A merge always points at the survivor's root, so real chains are one hop.
# The cap exists so a bad merge raises instead of looping.
MAX_MERGE_DEPTH = 10


class MergeChainError(Exception):
    """The merged_into chain loops or is deeper than MAX_MERGE_DEPTH."""


def resolve_root(person):
    """Return the surviving Person at the end of person's merged_into chain."""
    seen = {person.pk}
    current = person
    for _ in range(MAX_MERGE_DEPTH):
        if current.merged_into_id is None:
            return current
        if current.merged_into_id in seen:
            raise MergeChainError(f"Merge loop at person {current.pk}.")
        seen.add(current.merged_into_id)
        current = current.merged_into
    if current.merged_into_id is None:
        return current
    raise MergeChainError(
        f"Merge chain from person {person.pk} is deeper than {MAX_MERGE_DEPTH}."
    )


def identity_ids(person):
    """
    Ids of every Person record that is this human: the root and everything
    merged into it.

    A merge re-points the duplicate's rows to the survivor, so normally only
    the root has rows. Including the tombstones is the safety net for a row
    that was missed.
    """
    root = resolve_root(person)
    ids = {root.pk}
    frontier = [root.pk]
    for _ in range(MAX_MERGE_DEPTH):
        children = [
            pk
            for pk in Person.objects.filter(merged_into__in=frontier).values_list(
                "pk", flat=True
            )
            if pk not in ids
        ]
        if not children:
            return ids
        ids.update(children)
        frontier = children
    raise MergeChainError(
        f"Merge tree under person {root.pk} is deeper than {MAX_MERGE_DEPTH}."
    )
