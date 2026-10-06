import uuid

from django.db import models


class ImmutableRowError(Exception):
    """Code tried to change or delete something the data model says is permanent."""


class UUIDModel(models.Model):
    """Base for every table: an internal, random id that nothing user-supplied can replace."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Meta:
        abstract = True


class AppendOnlyMixin(models.Model):
    """
    Rows are inserted once, then never edited or deleted.

    This guards the normal save()/delete() path. queryset.update() still
    works and is used on purpose in exactly one place: a merge re-pointing
    `person`, which is an interpretation rather than part of the record.
    """

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ImmutableRowError(f"{type(self).__name__} rows are append-only.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ImmutableRowError(f"{type(self).__name__} rows are never deleted.")


class FrozenFieldsMixin(models.Model):
    """
    Rows whose FROZEN_FIELDS never change after insert, and which are never
    deleted. Fields not listed stay editable.
    """

    FROZEN_FIELDS = ()

    class Meta:
        abstract = True

    def changed_frozen_fields(self):
        attnames = [self._meta.get_field(name).attname for name in self.FROZEN_FIELDS]
        stored = type(self)._base_manager.filter(pk=self.pk).values(*attnames).first()
        if stored is None:
            return []
        return [name for name in attnames if stored[name] != getattr(self, name)]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            changed = self.changed_frozen_fields()
            if changed:
                raise ImmutableRowError(
                    f"{type(self).__name__}: {', '.join(changed)} cannot change after insert."
                )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ImmutableRowError(f"{type(self).__name__} rows are never deleted.")
