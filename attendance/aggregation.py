"""
How long someone attended, session by session.

attended_minutes() is the single place attendance is added up. Nothing
else may sum AttendanceRecord rows.
"""
import logging
from dataclasses import dataclass, field

from django.db import models

from rounds.models import CREDIT_WINDOW_GRACE

from .models import AttendanceRecord

logger = logging.getLogger(__name__)

# A session counts as attended when this much of it was attended.
ATTENDED_FRACTION = 0.5


class MinutesSource(models.TextChoices):
    TEAMS = "teams", "Teams"
    SIGNIN_SHEET = "signin_sheet", "Sign-in sheet"
    QR = "qr", "QR sign-in"
    MANUAL = "manual", "Manual"
    MIXED = "mixed", "Mixed"
    SELF_REPORTED = "self_reported", "Self-reported"


# The three independent claims. Teams rows, manual rows and room-roster rows
# together form the RECORDED claim: a manual row is staff correcting or
# completing the record (a laptop died, someone phoned in), not a separate
# sensor, so within this claim rows combine as they always did. A sign-in
# sheet tick and a QR scan are each a separate claim about presence.
RECORDED = "recorded"
CLAIM_OF_SOURCE = {
    "teams_upload": RECORDED,
    "manual": RECORDED,
    "room_roster": RECORDED,
    "signin_sheet": "signin_sheet",
    "qr_signin": "qr_signin",
}
CLAIMS = (RECORDED, "signin_sheet", "qr_signin")
# Claims whose only evidence is presence: a tick or a scan.
PRESENCE_ONLY = {"signin_sheet", "qr_signin"}
SOURCE_LABELS = {
    "teams_upload": MinutesSource.TEAMS,
    "signin_sheet": MinutesSource.SIGNIN_SHEET,
    "qr_signin": MinutesSource.QR,
    "manual": MinutesSource.MANUAL,
    "room_roster": MinutesSource.MANUAL,
}


@dataclass(frozen=True)
class SessionAttendance:
    """
    One person's attendance at one session: what each source claims, and
    the proposed figure (the highest claim, capped at the session).
    """

    session: object
    seconds: int  # proposed: highest claim, capped
    claims: dict = field(default_factory=dict)  # {claim: seconds}, each capped
    row_ids: dict = field(default_factory=dict)  # {claim: [row ids]}
    # Seconds cut off because hours-only rows pushed one source's claim past
    # the session's length. Not zero usually means a duplicate manual row.
    capped_seconds: int = 0
    # Sources further apart than the program's threshold: a human decides.
    disagree: bool = False

    @property
    def minutes(self):
        """Whole minutes, rounded down."""
        return self.seconds // 60

    @property
    def claim_minutes(self):
        return {claim: seconds // 60 for claim, seconds in self.claims.items()}

    @property
    def sources(self):
        """The claims that say the person was there at all."""
        return [claim for claim, seconds in self.claims.items() if seconds]

    @property
    def tick_only(self):
        """
        Only a tick or a scan says they were there: the weakest evidence
        claiming the most minutes, with nothing to contradict it. Flagged
        for a human even though no source disagrees.
        """
        present = self.sources
        return bool(present) and all(source in PRESENCE_ONLY for source in present)

    @property
    def needs_human(self):
        return self.disagree or self.tick_only

    @property
    def fraction(self):
        length = self.session.length_seconds
        return self.seconds / length if length else 0.0

    @property
    def attended(self):
        """At least ATTENDED_FRACTION of the session."""
        return self.fraction >= ATTENDED_FRACTION


@dataclass(frozen=True)
class AttendedTime:
    sessions: list = field(default_factory=list)  # SessionAttendance, in session order
    source: str | None = None  # None when the person has no active rows at all
    row_count: int = 0

    @property
    def seconds(self):
        return sum(s.seconds for s in self.sessions)

    @property
    def minutes(self):
        """Whole minutes across the event, rounded down."""
        return self.seconds // 60

    @property
    def capped_seconds(self):
        return sum(s.capped_seconds for s in self.sessions)

    @property
    def has_rows(self):
        return self.row_count > 0

    def for_session(self, session):
        return next((s for s in self.sessions if s.session.pk == session.pk), None)

    @property
    def sessions_attended(self):
        return [s.session for s in self.sessions if s.attended]


def merged_intervals(intervals):
    """The union of (start, end) datetime intervals, as a sorted list of disjoint ones."""
    merged = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def merged_seconds(intervals):
    """
    Total length in seconds of the union of (start, end) datetime intervals.

    Overlapping and touching intervals are counted once.
    """
    return int(sum((end - start).total_seconds() for start, end in merged_intervals(intervals)))


def overlap_seconds(disjoint, start, end):
    """Seconds of the disjoint, sorted intervals that fall inside (start, end)."""
    total = 0.0
    for s, e in disjoint:
        lo, hi = max(s, start), min(e, end)
        if hi > lo:
            total += (hi - lo).total_seconds()
    return int(total)


def attended_minutes(person, event):
    """
    Recorded attendance of `person` at `event`, session by session.

    Do NOT simplify this to a sum of duration_seconds. Teams writes one row
    per join, and the same person is often connected twice at once (laptop
    and phone), so the rows overlap. Summing them credits time twice, and
    also credits time spent waiting before the first talk or during a break.

    The steps:

    1. Take the event's ACTIVE rows (not superseded by a correction) that
       belong to this person, following merges, grouped into CLAIMS. Teams
       rows, manual rows and room-roster rows form the recorded claim; a
       sign-in sheet and a QR scan are each their own claim. Claims are
       independent evidence about the same person and are never added
       together.
    2. Within the recorded claim, rows with join/leave times form the UNION
       of their intervals. duration_seconds is ignored for these rows.
    3. For each session, measure how much of each claim's union falls
       inside the session's own times. The first session opens
       CREDIT_WINDOW_GRACE early and the last closes CREDIT_WINDOW_GRACE
       late; time between sessions counts for nothing.
    4. Rows without times claim their session: an hours-only manual row
       claims its duration_seconds (added to the recorded claim); a sign-in
       sheet tick or a QR scan claims the whole session.
    5. Each claim is capped at the session's length; the PROPOSED
       figure is the HIGHEST claim across sources. Highest because every
       source under-reports; capped because no source can exceed the talk.
       Sources further apart than the program's disagreement threshold are
       flagged; so is a session whose only evidence is a tick or a scan.
       When hours-only rows pushed a claim over the session, that is logged
       and reported in capped_seconds.

    The proposed figure is not confirmed. Credit on a certificate counts
    only minutes a staff member has signed off (SessionAttendanceDecision).

    Self-reported minutes are not considered here; see
    credits.rules.creditable_time for that fallback.
    """
    sessions = list(event.sessions.order_by("start_at", "position"))
    rows = (
        AttendanceRecord.objects.active()
        .for_person(person)
        .filter(event=event)
        .values_list("pk", "source", "join_at", "leave_at", "duration_seconds", "session_id")
    )
    threshold = event.program.attendance_disagreement_minutes * 60

    # Within one source: timed rows form a union; rows without times claim
    # their session. Sources are kept apart; they are never added together.
    intervals = {}  # claim -> [(join, leave)]
    untimed = {}  # (claim, session id) -> seconds
    ids = {}  # (claim, session id or None) -> [row ids]
    row_count = 0
    for pk, source, join_at, leave_at, duration_seconds, session_id in rows:
        row_count += 1
        claim = CLAIM_OF_SOURCE[source]
        if join_at is not None and leave_at is not None:
            intervals.setdefault(claim, []).append((join_at, leave_at))
            ids.setdefault((claim, None), []).append(str(pk))
        else:
            untimed[(claim, session_id)] = untimed.get((claim, session_id), 0) + duration_seconds
            ids.setdefault((claim, session_id), []).append(str(pk))
    unions = {claim: merged_intervals(spans) for claim, spans in intervals.items()}

    per_session = []
    for index, session in enumerate(sessions):
        start, end = session.start_at, session.end_at
        if index == 0:
            start -= CREDIT_WINDOW_GRACE
        if index == len(sessions) - 1:
            end += CREDIT_WINDOW_GRACE
        length = session.length_seconds
        claims = {}
        row_ids = {}
        capped = 0
        for name in CLAIMS:
            # Grace can make up for leaving early, but not exceed the session.
            timed = min(overlap_seconds(unions.get(name, []), start, end), length)
            hours_only = untimed.get((name, session.pk), 0)
            claim = min(timed + hours_only, length)
            over = timed + hours_only - claim
            if over:
                capped += over
                logger.warning(
                    "Attendance capped at the session length: person=%s session=%s claim=%s "
                    "timed=%ss hours_only=%ss session=%ss. Probably a duplicate manual row.",
                    person.pk,
                    session.pk,
                    name,
                    timed,
                    hours_only,
                    length,
                )
            rows_here = ids.get((name, session.pk), []) + (
                ids.get((name, None), []) if timed else []
            )
            if claim or rows_here:
                claims[name] = claim
                row_ids[name] = rows_here
        claimed = [seconds for seconds in claims.values()]
        seconds = max(claimed, default=0)
        present = [seconds for seconds in claimed if seconds]
        disagree = len(present) > 1 and max(present) - min(present) > threshold
        per_session.append(
            SessionAttendance(
                session=session,
                seconds=seconds,
                claims=claims,
                row_ids=row_ids,
                capped_seconds=capped,
                disagree=disagree,
            )
        )

    labels = {SOURCE_LABELS[source] for source in {row[1] for row in rows}}
    if not labels:
        source = None
    elif len(labels) == 1:
        source = labels.pop()
    else:
        source = MinutesSource.MIXED
    return AttendedTime(sessions=per_session, source=source, row_count=row_count)


def sessions_attended(person, event):
    """
    The event's sessions this person attended at least ATTENDED_FRACTION of,
    by recorded attendance. What the evaluation reminder and the
    certificate line list.
    """
    return attended_minutes(person, event).sessions_attended


# What a claim group is called on the sign-off and preview screens.
CLAIM_LABELS = {
    RECORDED: "recorded (Teams and corrections)",
    "signin_sheet": "sign-in sheet",
    "qr_signin": "QR sign-in",
}
