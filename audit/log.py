from .models import AuditLog


def _user_label(user):
    username = user.get_username()
    return f"{username} <{user.email}>" if user.email else username


def _person_label(person):
    email = person.emails.filter(is_primary=True).values_list("email", flat=True).first()
    return f"{person.full_name} <{email}>" if email else person.full_name


def record(action, obj=None, *, user=None, person=None, system=None, metadata=None, request=None):
    """
    Write one audit entry.

    Exactly one actor: `user` for a staff action, `person` for an attendee
    action, or `system="job-name"` for something no human did. Passing
    `request` fills in the IP, and the staff user if none was given.

    Log what would be disputed (a match, a manual row, an adjustment, a
    merge, a certificate), not page views.
    """
    if (
        user is None
        and person is None
        and system is None
        and request is not None
        and request.user.is_authenticated
    ):
        user = request.user
    given = [actor for actor in (user, person, system) if actor is not None]
    if len(given) != 1:
        raise ValueError("An audit entry needs exactly one of user, person or system.")

    if user is not None:
        actor = dict(actor_type=AuditLog.ActorType.STAFF, actor_user=user, actor_label=_user_label(user))
    elif person is not None:
        actor = dict(
            actor_type=AuditLog.ActorType.ATTENDEE,
            actor_person=person,
            actor_label=_person_label(person),
        )
    else:
        actor = dict(actor_type=AuditLog.ActorType.SYSTEM, actor_label=f"system:{system}")

    return AuditLog.objects.create(
        action=action,
        object_type=obj._meta.label_lower if obj is not None else "",
        object_id=str(obj.pk) if obj is not None else "",
        metadata=metadata or {},
        ip=request.META.get("REMOTE_ADDR") if request is not None else None,
        **actor,
    )
