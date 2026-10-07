"""
Sign-off: turning proposed minutes into confirmed minutes.

Credit on a certificate counts only confirmed minutes. A confirmation is a
SessionAttendanceDecision: one staff member, one person, one session. The
work this project exists to remove is several thousand clicks a year, so
one action confirms every row where the sources agree and holds back only
the rows a human should look at.
"""
from dataclasses import dataclass, field

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q

from audit.log import record
from people.identity import resolve_root
from people.models import Person

from .aggregation import attended_minutes
from .models import AttendanceRecord, SessionAttendanceDecision

Basis = SessionAttendanceDecision.Basis

HELD_DISAGREE = "sources disagree"
HELD_TICK_ONLY = "only a tick or a scan says they were there"
HELD_UNMATCHED = "the session has unmatched rows"
HELD_PRESENTER = "they presented this session"


def unmatched_sessions(event):
    """
    {session: count} of active, unmatched rows touching each session. A
    session with unmatched rows cannot be signed off: the row might be
    anyone's, including someone already on the list.
    """
    counts = {}
    sessions = list(event.sessions.order_by("start_at"))
    rows = AttendanceRecord.objects.active().unmatched().filter(event=event)
    for row in rows:
        for session in sessions:
            if row.session_id == session.pk or (
                row.join_at is not None
                and row.join_at < session.end_at
                and row.leave_at > session.start_at
            ):
                counts[session] = counts.get(session, 0) + 1
    return counts


def current_decisions(event):
    """{(person id, session id): decision} for the event's current decisions."""
    return {
        (d.person_id, d.session_id): d
        for d in SessionAttendanceDecision.objects.current()
        .filter(session__event=event)
        .select_related("session")
    }


def event_people(event):
    """Everyone with an active matched attendance row for the event, resolved to roots."""
    ids = set(
        AttendanceRecord.objects.active()
        .filter(event=event, person__isnull=False)
        .values_list("person", flat=True)
    )
    roots = {}
    for person in Person.objects.filter(pk__in=ids).select_related("merged_into"):
        root = resolve_root(person)
        roots[root.pk] = root
    return sorted(roots.values(), key=lambda p: (p.family_name.lower(), p.given_name.lower()))


@dataclass
class SessionReview:
    """One person's standing on one session, for the sign-off screen."""

    session: object
    attendance: object  # SessionAttendance (claims, proposed, flags)
    decision: object = None  # the current SessionAttendanceDecision, if any
    presented: bool = False
    unmatched_rows: int = 0

    @property
    def proposed_minutes(self):
        return self.attendance.minutes

    @property
    def held_reasons(self):
        reasons = []
        if self.presented:
            reasons.append(HELD_PRESENTER)
        if self.unmatched_rows:
            reasons.append(HELD_UNMATCHED)
        if self.attendance.disagree:
            reasons.append(HELD_DISAGREE)
        if self.attendance.tick_only:
            reasons.append(HELD_TICK_ONLY)
        return reasons

    @property
    def can_bulk_confirm(self):
        return not self.held_reasons and self.decision is None and self.attendance.sources

    @property
    def is_confirmed(self):
        return self.decision is not None

    @property
    def stale(self):
        """Confirmed, but the proposal has moved since (a correction or a new source)."""
        return self.decision is not None and self.decision.proposed_minutes != self.proposed_minutes


@dataclass
class PersonReview:
    person: object
    sessions: list = field(default_factory=list)  # SessionReview, in session order

    @property
    def confirmed(self):
        return all(s.is_confirmed or s.presented for s in self.sessions)

    @property
    def held(self):
        return [s for s in self.sessions if s.held_reasons and not s.is_confirmed and not s.presented]


def review(event):
    """[PersonReview] for the event: what the sign-off screen shows."""
    from credits.rules import presented_session_ids

    decisions = current_decisions(event)
    unmatched = unmatched_sessions(event)
    sessions = list(event.sessions.order_by("start_at", "position"))
    reviews = []
    for person in event_people(event):
        presented = presented_session_ids(person, event)
        recorded = attended_minutes(person, event)
        rows = []
        for session in sessions:
            rows.append(
                SessionReview(
                    session=session,
                    attendance=recorded.for_session(session),
                    decision=decisions.get((person.pk, session.pk)),
                    presented=session.pk in presented,
                    unmatched_rows=unmatched.get(session, 0),
                )
            )
        reviews.append(PersonReview(person=person, sessions=rows))
    return reviews


def _based_on(attendance):
    return {
        "claims": attendance.claim_minutes,
        "rows": attendance.row_ids,
        "disagree": attendance.disagree,
        "tick_only": attendance.tick_only,
    }


def _decide(person, session_review, minutes, basis, comment, user, supersedes=None):
    decision = SessionAttendanceDecision(
        person=person,
        session=session_review.session,
        confirmed_minutes=minutes,
        proposed_minutes=session_review.proposed_minutes,
        basis=basis,
        based_on=_based_on(session_review.attendance),
        comment=comment or "",
        confirmed_by=user,
        supersedes=supersedes,
    )
    decision.full_clean()
    decision.save()
    return decision


@dataclass
class ConfirmResult:
    confirmed: list = field(default_factory=list)  # decisions written
    held: list = field(default_factory=list)  # (person, SessionReview) held back
    skipped: int = 0  # already confirmed, or nothing to confirm

    @property
    def held_people(self):
        return sorted({person for person, _ in self.held}, key=lambda p: p.family_name)


@transaction.atomic
def confirm_event(event, *, user, request=None):
    """
    Confirm with exceptions. Every person-and-session whose sources agree
    (or that has one source and it is not just a tick) gets a decision with
    basis sources_agree. Rows that disagree, tick-only rows, sessions with
    unmatched rows and the person's own talks are held back and listed.

    Per session, not per person: a person with one agreeing and one
    disagreeing session gets the agreeing one confirmed and the other held.
    """
    result = ConfirmResult()
    for person_review in review(event):
        for session_review in person_review.sessions:
            if session_review.presented or session_review.is_confirmed:
                result.skipped += 1
                continue
            if not session_review.attendance.sources:
                result.skipped += 1
                continue
            if session_review.held_reasons:
                result.held.append((person_review.person, session_review))
                continue
            result.confirmed.append(
                _decide(
                    person_review.person,
                    session_review,
                    session_review.proposed_minutes,
                    Basis.SOURCES_AGREE,
                    "",
                    user,
                )
            )
    record(
        "attendance.event_confirmed",
        event,
        user=user,
        request=request,
        metadata={
            "confirmed": len(result.confirmed),
            "held": [
                {"person": str(p.pk), "session": str(s.session.pk), "reasons": s.held_reasons}
                for p, s in result.held
            ],
            "skipped": result.skipped,
        },
    )
    return result


@transaction.atomic
def confirm_person(event, person, choices, *, user, comment="", request=None):
    """
    Sign off one person, session by session. `choices` maps session id to
    the minutes to confirm (the proposal, a claim, or a figure typed in),
    or to None to leave that session alone. Sessions with unmatched rows
    are refused; a figure that differs from the proposal needs a comment.
    """
    person = resolve_root(person)
    by_session = {str(r.session.pk): r for pr in review(event) if pr.person == person for r in pr.sessions}
    if not by_session:
        raise ValidationError("This person has no attendance rows for this event.")
    written = []
    for session_id, minutes in choices.items():
        if minutes is None:
            continue
        session_review = by_session.get(str(session_id))
        if session_review is None:
            raise ValidationError("That session is not part of this event.")
        if session_review.presented:
            raise ValidationError(f"{session_review.session.title}: they presented it; that is teaching.")
        if session_review.unmatched_rows:
            raise ValidationError(
                f"{session_review.session.title} still has {session_review.unmatched_rows} "
                "unmatched row(s). Clear the match queue first."
            )
        minutes = int(minutes)
        if minutes == session_review.proposed_minutes:
            basis = Basis.SOURCES_AGREE if not session_review.held_reasons else Basis.HIGHEST_CLAIM
        elif minutes in session_review.attendance.claim_minutes.values():
            basis = Basis.HIGHEST_CLAIM
        else:
            basis = Basis.MANUAL
        written.append(
            _decide(
                person,
                session_review,
                minutes,
                basis,
                comment,
                user,
                supersedes=session_review.decision,
            )
        )
    record(
        "attendance.person_confirmed",
        person,
        user=user,
        request=request,
        metadata={
            "event": str(event.pk),
            "decisions": [
                {
                    "session": str(d.session_id),
                    "confirmed": d.confirmed_minutes,
                    "proposed": d.proposed_minutes,
                    "basis": d.basis,
                    "supersedes": str(d.supersedes_id) if d.supersedes_id else None,
                }
                for d in written
            ],
            "comment": comment,
        },
    )
    return written


def confirmed_minutes(person, event):
    """{session id: minutes} from the current decisions, for the credit rules."""
    return {
        d.session_id: d.confirmed_minutes
        for d in SessionAttendanceDecision.objects.current()
        .for_person(person)
        .filter(session__event=event)
    }


def unconfirmed_sessions(person, event):
    """Sessions where this person has proposed minutes but no current decision."""
    decided = set(confirmed_minutes(person, event))
    recorded = attended_minutes(person, event)
    return [s.session for s in recorded.sessions if s.sources and s.session.pk not in decided]
