from django.db import models


class PersonOwnedQuerySet(models.QuerySet):
    """
    Base queryset for every table whose rows belong to a person.

    for_person() is the one way a view narrows a table to what the
    signed-in person may see. core.authz.get_owned_or_404 refuses to work
    with a model that doesn't have it.
    """

    # Lookup path from this model to its Person. Override where the link is
    # indirect, e.g. "submission__person".
    person_lookup = "person"

    def for_person(self, person):
        # Imported here because people.models imports this module.
        from .identity import identity_ids

        return self.filter(**{f"{self.person_lookup}__in": identity_ids(person)})
