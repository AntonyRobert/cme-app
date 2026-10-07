import hashlib
import itertools

from attendance.models import AttendanceRecord, AttendanceSupersession, AttendanceUpload
from people.tests.factories import make_staff
from rounds.tests.factories import at

_counter = itertools.count(1)

Source = AttendanceRecord.Source


def make_upload(event, user=None):
    n = next(_counter)
    return AttendanceUpload.objects.create(
        event=event,
        original_filename=f"export{n}.csv",
        stored_path=f"teams/export{n}.csv",
        sha256=hashlib.sha256(str(n).encode()).hexdigest(),
        uploaded_by=user or make_staff(),
    )


def teams_row(event, person, start, end, *, upload=None, user=None, **extra):
    """A Teams join from `start` to `end` minutes after the scheduled start."""
    upload = upload or event.uploads.first() or make_upload(event, user)
    return AttendanceRecord.objects.create(
        source=Source.TEAMS_UPLOAD,
        upload=upload,
        event=event,
        raw_display_name=extra.pop("raw_display_name", str(person) if person else "Unknown"),
        join_at=at(start),
        leave_at=at(end),
        person=person,
        match_method=extra.pop(
            "match_method",
            AttendanceRecord.MatchMethod.EMAIL_EXACT
            if person
            else AttendanceRecord.MatchMethod.UNMATCHED,
        ),
        created_by=user or upload.uploaded_by,
        **extra,
    )


def manual_row(event, person, *, minutes=None, start=None, end=None, user=None, **extra):
    """
    A hand-entered row: hours-only when `minutes` is given (for the event's
    first session unless `session` is passed), timed otherwise.
    """
    if minutes is not None and "session" not in extra:
        extra["session"] = event.sessions.order_by("start_at").first()
    return AttendanceRecord.objects.create(
        source=Source.MANUAL,
        event=event,
        person=person,
        duration_seconds=extra.pop(
            "duration_seconds", minutes * 60 if minutes is not None else None
        ),
        join_at=at(start) if start is not None else None,
        leave_at=at(end) if end is not None else None,
        reason=extra.pop("reason", "Confirmed by the chair"),
        created_by=user or make_staff(),
        **extra,
    )


def roster_row(device_row, person, *, start=None, end=None, user=None, **extra):
    """Someone who sat in the room behind `device_row`."""
    return AttendanceRecord.objects.create(
        source=Source.ROOM_ROSTER,
        event=device_row.event,
        attributed_to=device_row,
        person=person,
        join_at=at(start) if start is not None else None,
        leave_at=at(end) if end is not None else None,
        reason=extra.pop("reason", "Signed the room sheet"),
        created_by=user or make_staff(),
        **extra,
    )


def supersede(old_rows, new_row, user=None):
    user = user or make_staff()
    return [
        AttendanceSupersession.objects.create(old=old, new=new_row, created_by=user)
        for old in old_rows
    ]
