"""
The operations on attendance that leave an audit trail.

The admin calls these rather than saving rows itself, so that every
match, manual row, correction and upload is logged the same way whatever
screen it came from.
"""
import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone

from audit.log import record
from people.identity import resolve_root
from people.models import PersonEmail
from people.normalize import normalize_email

from .models import AttendanceRecord, AttendanceSupersession, AttendanceUpload

Match = AttendanceRecord.MatchMethod

ALLOWED_UPLOAD_EXTENSIONS = {".csv", ".xlsx"}
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class MatchResult:
    row: AttendanceRecord
    email_added: str | None
    also_matched: int


def _usable_email(raw):
    email = normalize_email(raw or "")
    if not email:
        return None
    try:
        validate_email(email)
    except ValidationError:
        return None
    return email


@transaction.atomic
def match_record(row, person, *, user, request=None, method=Match.MANUAL):
    """
    Say who an attendance row belongs to, or (person=None) that nobody is known.

    Confirming a match also remembers the row's email address for that
    person, and gives them every other still-unmatched row carrying the same
    address, so one decision clears all of someone's rejoin rows and the next
    upload matches by itself.
    """
    previous_id = row.person_id
    if person is not None:
        person = resolve_root(person)
    row.person = person
    row.match_method = method if person is not None else Match.UNMATCHED
    row.matched_at = timezone.now()
    row.matched_by = user
    row.save()

    if person is None:
        action = "attendance.unmatched"
    elif previous_id is None:
        action = "attendance.matched"
    else:
        action = "attendance.rematched"
    record(
        action,
        row,
        user=user,
        request=request,
        metadata={
            "from_person": str(previous_id) if previous_id else None,
            "to_person": str(person.pk) if person else None,
            "method": row.match_method,
            "raw_display_name": row.raw_display_name,
            "raw_email": row.raw_email,
        },
    )

    email_added = None
    also_matched = 0
    email = _usable_email(row.raw_email) if person is not None else None
    if email:
        owner = PersonEmail.objects.filter(email=email).first()
        if owner is None:
            PersonEmail.objects.create(person=person, email=email)
            email_added = email
            record(
                "person.email_added",
                person,
                user=user,
                request=request,
                metadata={"email": email, "from_attendance_row": str(row.pk)},
            )
        if owner is None or owner.person_id == person.pk:
            siblings = AttendanceRecord.objects.unmatched().filter(raw_email__iexact=email)
            sibling_ids = [str(pk) for pk in siblings.values_list("pk", flat=True)]
            if sibling_ids:
                siblings.update(
                    person=person,
                    match_method=Match.EMAIL_ALIAS,
                    matched_at=row.matched_at,
                    matched_by=user,
                )
                also_matched = len(sibling_ids)
                record(
                    "attendance.matched",
                    person,
                    user=user,
                    request=request,
                    metadata={
                        "method": Match.EMAIL_ALIAS,
                        "raw_email": email,
                        "rows": sibling_ids,
                        "because_of_row": str(row.pk),
                    },
                )
    return MatchResult(row=row, email_added=email_added, also_matched=also_matched)


def log_manual_row(row, *, user, request=None):
    """Audit a hand-entered attendance row. These are the fraud surface."""
    return record(
        "attendance.manual_row_created",
        row,
        user=user,
        request=request,
        metadata={
            "source": row.source,
            "event": str(row.event_id),
            "person": str(row.person_id) if row.person_id else None,
            "join_at": row.join_at,
            "leave_at": row.leave_at,
            "duration_seconds": row.duration_seconds,
            "attributed_to": str(row.attributed_to_id) if row.attributed_to_id else None,
            "reason": row.reason,
        },
    )


@transaction.atomic
def supersede_rows(old_rows, new_row, *, user, request=None):
    """Mark each of old_rows as replaced by new_row. The old rows are not touched."""
    links = []
    for old in old_rows:
        link = AttendanceSupersession(old=old, new=new_row, created_by=user)
        link.full_clean()
        link.save()
        links.append(link)
    if links:
        record(
            "attendance.superseded",
            new_row,
            user=user,
            request=request,
            metadata={"replaced_rows": [str(link.old_id) for link in links]},
        )
    return links


class DuplicateUpload(Exception):
    def __init__(self, existing):
        self.existing = existing
        super().__init__(f"This exact file was already uploaded on {existing.uploaded_at:%Y-%m-%d}.")


@transaction.atomic
def store_upload(event, uploaded_file, *, user, request=None):
    """
    Keep a Teams export exactly as it arrived, and record it.

    The file is written once under UPLOAD_ROOT/teams/, named by its hash,
    and made read-only. Nothing ever opens it for writing again.
    """
    extension = Path(uploaded_file.name).suffix.lower()
    if extension not in ALLOWED_UPLOAD_EXTENSIONS:
        raise ValidationError("Teams exports are .csv or .xlsx files.")
    if uploaded_file.size > MAX_UPLOAD_BYTES:
        raise ValidationError("That file is too large to be a Teams attendance export.")

    digest = hashlib.sha256()
    chunks = []
    for chunk in uploaded_file.chunks():
        digest.update(chunk)
        chunks.append(chunk)
    sha256 = digest.hexdigest()

    existing = AttendanceUpload.objects.filter(sha256=sha256).first()
    if existing is not None:
        raise DuplicateUpload(existing)

    relative = PurePosixPath("teams") / f"{sha256}{extension}"
    target = Path(settings.UPLOAD_ROOT) / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        with open(target, "xb") as handle:
            for chunk in chunks:
                handle.write(chunk)
        os.chmod(target, stat.S_IREAD | stat.S_IRGRP)

    upload = AttendanceUpload.objects.create(
        event=event,
        original_filename=os.path.basename(uploaded_file.name)[:255],
        stored_path=str(relative),
        sha256=sha256,
        uploaded_by=user,
    )
    record(
        "attendance.upload_stored",
        upload,
        user=user,
        request=request,
        metadata={"event": str(event.pk), "filename": upload.original_filename, "sha256": sha256},
    )
    return upload
