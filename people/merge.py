"""
Merging a duplicate Person into the surviving one.

See docs/data-model.md, "Merging". The short version: check for
collisions first and refuse rather than pick a winner, move the emails,
re-point everything else, leave the duplicate as a tombstone, and write
one audit entry that lists what moved so a wrong merge can be undone.
"""
from dataclasses import dataclass, field

from django.db import transaction
from django.db.models import UniqueConstraint

from audit.log import record

from .models import Person, PersonEmail

# Tables whose link to a Person is history rather than ownership. An audit
# entry keeps pointing at whoever acted, even after they are merged away.
NOT_REPOINTED = {"audit.auditlog"}


class MergeError(Exception):
    """The merge was refused. The message is safe to show to staff."""


@dataclass(frozen=True)
class Collision:
    """Both records hold a row where only one is allowed. A human must choose."""

    model_label: str
    description: str
    survivor_row: object
    duplicate_row: object


class MergeCollision(MergeError):
    def __init__(self, collisions):
        self.collisions = collisions
        super().__init__(
            "Both records have "
            + "; ".join(sorted({c.description for c in collisions}))
            + ". Resolve these by hand first: merging would have to discard one."
        )


@dataclass
class MergeResult:
    survivor: Person
    duplicate: Person
    moved_emails: list = field(default_factory=list)
    repointed: dict = field(default_factory=dict)

    @property
    def repointed_count(self):
        return sum(len(ids) for ids in self.repointed.values())


def person_links():
    """
    Every (model, field name) that points at a Person and should follow a
    merge. Found by introspection, so a table added later is covered
    without anyone remembering to list it here.
    """
    links = []
    for relation in Person._meta.related_objects:
        model = relation.related_model
        if relation.many_to_many or model is Person:
            continue
        if model._meta.label_lower in NOT_REPOINTED:
            continue
        links.append((model, relation.field.name))
    return sorted(links, key=lambda link: (link[0]._meta.label_lower, link[1]))


# Conditional unique constraints on a person link, and the condition that
# selects the rows it applies to. Any other conditional constraint makes the
# merge refuse until someone decides how it behaves.
CONDITIONAL_UNIQUE = {
    # One scan per person per session. Two merged records that both scanned
    # the same session are a collision a human resolves, like two evaluations.
    "attendancerecord_one_qr_scan_per_session": {"source": "qr_signin"},
}


def _unique_sets(model, field_name):
    """(other fields, extra filter) groups that, with field_name, must be unique."""
    if model._meta.get_field(field_name).unique:
        yield [], {}
    for constraint in model._meta.constraints:
        if isinstance(constraint, UniqueConstraint) and field_name in constraint.fields:
            if constraint.condition is not None:
                # PersonEmail's one-primary rule is handled by demoting moved
                # addresses; the others are listed above or refused.
                if model is PersonEmail:
                    continue
                if constraint.name not in CONDITIONAL_UNIQUE:
                    raise NotImplementedError(
                        f"{constraint.name}: conditional uniqueness on a person link "
                        "needs explicit handling in people/merge.py."
                    )
                extra = CONDITIONAL_UNIQUE[constraint.name]
            else:
                extra = {}
            yield [name for name in constraint.fields if name != field_name], extra


def find_collisions(survivor, duplicate):
    """Rows the two records both hold where the schema allows only one."""
    collisions = []
    for model, field_name in person_links():
        for others, extra in _unique_sets(model, field_name):
            other_fields = [model._meta.get_field(name) for name in others]
            for duplicate_row in model._base_manager.filter(**{field_name: duplicate}, **extra):
                same = {f.attname: getattr(duplicate_row, f.attname) for f in other_fields}
                survivor_row = model._base_manager.filter(
                    **{field_name: survivor}, **same, **extra
                ).first()
                if survivor_row is not None:
                    names = " and ".join(f.verbose_name for f in other_fields)
                    collisions.append(
                        Collision(
                            model_label=model._meta.label_lower,
                            description=(
                                f"a {model._meta.verbose_name} for the same {names}"
                                if names
                                else f"a {model._meta.verbose_name}"
                            ),
                            survivor_row=survivor_row,
                            duplicate_row=duplicate_row,
                        )
                    )
    return collisions


def check_mergeable(survivor, duplicate):
    """Raise MergeError if this merge must not happen. Changes nothing."""
    if survivor.pk == duplicate.pk:
        raise MergeError("A record cannot be merged into itself.")
    if survivor.is_merged:
        raise MergeError(f"{survivor} is itself merged away. Merge into the record it points at.")
    if duplicate.is_merged:
        raise MergeError(f"{duplicate} has already been merged.")
    if survivor.staff_user_id and duplicate.staff_user_id:
        raise MergeError("Both records are linked to a staff account. Unlink one first.")
    collisions = find_collisions(survivor, duplicate)
    if collisions:
        raise MergeCollision(collisions)


@transaction.atomic
def merge_people(survivor, duplicate, *, user, request=None):
    """
    Fold `duplicate` into `survivor`. All or nothing.

    Raises MergeCollision when both hold a row only one may hold, such as an
    evaluation of the same session. It never picks a winner: that would
    silently discard what someone submitted.
    """
    locked = {
        person.pk: person
        for person in Person.objects.select_for_update().filter(
            pk__in=[survivor.pk, duplicate.pk]
        )
    }
    survivor, duplicate = locked[survivor.pk], locked.get(duplicate.pk, survivor)
    check_mergeable(survivor, duplicate)
    result = MergeResult(survivor=survivor, duplicate=duplicate)

    # Emails move so matching hits the survivor directly from now on.
    emails = PersonEmail.objects.filter(person=duplicate)
    result.moved_emails = sorted(emails.values_list("email", flat=True))
    if survivor.emails.filter(is_primary=True).exists():
        emails.update(person=survivor, is_primary=False)
    else:
        emails.update(person=survivor)

    # `person` is an interpretation, so re-pointing is a legitimate update
    # even on tables whose rows are otherwise frozen. update() is deliberate.
    for model, field_name in person_links():
        if model is PersonEmail:
            continue
        rows = model._base_manager.filter(**{field_name: duplicate})
        ids = [str(pk) for pk in rows.values_list("pk", flat=True)]
        if ids:
            rows.update(**{field_name: survivor})
            result.repointed[f"{model._meta.label_lower}.{field_name}"] = ids

    # Never leave a chain: earlier tombstones of the duplicate point at the root too.
    earlier = Person.objects.filter(merged_into=duplicate)
    earlier_ids = [str(pk) for pk in earlier.values_list("pk", flat=True)]
    earlier.update(merged_into=survivor)

    staff_user_id = duplicate.staff_user_id
    duplicate.staff_user = None
    duplicate.merged_into = survivor
    duplicate.save()
    if staff_user_id:
        survivor.staff_user_id = staff_user_id
        survivor.save()

    record(
        "person.merged",
        survivor,
        user=user,
        request=request,
        metadata={
            "duplicate_id": str(duplicate.pk),
            "duplicate_name": duplicate.full_name,
            "duplicate_licence": [duplicate.licence_jurisdiction, duplicate.licence_number],
            "moved_emails": result.moved_emails,
            "repointed": result.repointed,
            "earlier_tombstones_repointed": earlier_ids,
            "staff_user_moved": staff_user_id,
        },
    )
    return result
