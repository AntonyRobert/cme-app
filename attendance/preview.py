"""
Preview an import before anything is stored.

A file waits in UPLOAD_ROOT/pending/ under its hash, with no database row,
until the reviewer confirms. The preview shows what the system understood:
which event matched and which date reading was chosen, every participant
with their minutes per session, and the rows worth a look.
"""
import datetime
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from people.models import PersonEmail
from people.normalize import normalize_email
from rounds.models import CREDIT_WINDOW_GRACE

from . import teams
from .aggregation import CLAIM_LABELS, attended_minutes, merged_intervals, overlap_seconds

PENDING_LIFETIME = datetime.timedelta(days=1)

RULE = (
    "The organizer and every participant in the export are attendees. Presenter hours "
    "come only from the sessions' presenter lists, never from this file."
)


def pending_dir():
    path = Path(settings.UPLOAD_ROOT) / "pending"
    path.mkdir(parents=True, exist_ok=True)
    return path


def stash(raw, filename, *, event_id, user_id):
    """Keep the bytes and what we know about them until confirmed. Returns the hash."""
    sha = hashlib.sha256(raw).hexdigest()
    (pending_dir() / f"{sha}.bin").write_bytes(raw)
    (pending_dir() / f"{sha}.json").write_text(
        json.dumps(
            {
                "filename": os.path.basename(filename)[:255],
                "event_id": str(event_id),
                "user_id": user_id,
                "stashed_at": timezone.now().isoformat(),
            }
        ),
        encoding="utf-8",
    )
    return sha


def load(sha):
    """(bytes, metadata) for a pending file, or None."""
    if not sha or any(c not in "0123456789abcdef" for c in sha):
        return None
    data = pending_dir() / f"{sha}.bin"
    meta = pending_dir() / f"{sha}.json"
    if not (data.exists() and meta.exists()):
        return None
    return data.read_bytes(), json.loads(meta.read_text(encoding="utf-8"))


def discard(sha):
    for suffix in (".bin", ".json"):
        path = pending_dir() / f"{sha}{suffix}"
        if path.exists():
            path.unlink()


def purge(older_than=PENDING_LIFETIME):
    """Remove pending files nobody confirmed. Returns how many."""
    cutoff = timezone.now() - older_than
    removed = 0
    for meta in pending_dir().glob("*.json"):
        try:
            stashed_at = datetime.datetime.fromisoformat(json.loads(meta.read_text(encoding="utf-8"))["stashed_at"])
        except (ValueError, KeyError):
            stashed_at = None
        if stashed_at is None or stashed_at < cutoff:
            discard(meta.stem)
            removed += 1
    return removed


@dataclass
class ParticipantPreview:
    display_name: str
    email: str
    person: object = None
    rows: int = 0
    minutes: dict = field(default_factory=dict)  # session -> minutes this file gives
    existing: dict = field(default_factory=dict)  # session -> {claim: minutes} already stored
    flags: list = field(default_factory=list)

    @property
    def total_minutes(self):
        return sum(self.minutes.values())

    @property
    def sessions_crossed(self):
        return [session for session, minutes in self.minutes.items() if minutes]

    @property
    def matched(self):
        return self.person is not None


@dataclass
class ImportPreview:
    event: object
    date_order: str
    export: object
    participants: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    rule: str = RULE

    @property
    def flagged(self):
        return [p for p in self.participants if p.flags]

    @property
    def unmatched(self):
        return [p for p in self.participants if not p.matched]

    @property
    def row_count(self):
        return sum(p.rows for p in self.participants)


def preview_teams(export, event, order):
    """
    What importing this export into this event would give, person by person,
    without writing anything.
    """
    sessions = list(event.sessions.order_by("start_at", "position"))
    _, _, resolved = teams.resolve_times(export, order)
    owners = {}
    emails = {normalize_email(row.email) for row, _, _ in resolved if row.email}
    for link in PersonEmail.objects.filter(email__in=emails).select_related("person"):
        owners[link.email] = link.person
    threshold = event.program.attendance_disagreement_minutes

    grouped = {}
    for row, join, leave in resolved:
        key = teams.person_key(row.email, row.display_name)
        entry = grouped.setdefault(
            key,
            ParticipantPreview(
                display_name=row.display_name,
                email=row.email,
                person=owners.get(normalize_email(row.email)) if row.email else None,
            ),
        )
        entry.rows += 1
        entry.__dict__.setdefault("_spans", []).append((join, leave))

    checksum_names = {w.split(":")[0] for w in export.warnings if ":" in w}
    for entry in grouped.values():
        union = merged_intervals(entry.__dict__.pop("_spans"))
        for index, session in enumerate(sessions):
            start, end = session.start_at, session.end_at
            if index == 0:
                start -= CREDIT_WINDOW_GRACE
            if index == len(sessions) - 1:
                end += CREDIT_WINDOW_GRACE
            seconds = min(overlap_seconds(union, start, end), session.length_seconds)
            entry.minutes[session] = seconds // 60
        if not entry.matched:
            entry.flags.append("matches nobody: will wait in the review queue")
        if entry.display_name in checksum_names:
            entry.flags.append("Teams' own total for them does not reconcile with its rows")
        if entry.matched:
            stored = attended_minutes(entry.person, event)
            for share in stored.sessions:
                claims = share.claim_minutes
                if claims:
                    entry.existing[share.session] = claims
                    new = entry.minutes.get(share.session, 0)
                    for claim, minutes in claims.items():
                        if minutes and new and abs(minutes - new) > threshold:
                            entry.flags.append(
                                f"{share.session.title}: this file says {new} min, "
                                f"the stored {CLAIM_LABELS[claim]} claim says {minutes}"
                            )
    participants = sorted(grouped.values(), key=lambda p: p.display_name.casefold())
    return ImportPreview(
        event=event,
        date_order=order,
        export=export,
        participants=participants,
        warnings=list(export.warnings),
    )
