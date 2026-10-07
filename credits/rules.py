"""
The credit rules. One function per rule that might change.

Nothing here is stored. Credit is derived from attendance and evaluation
every time it is asked for; the only frozen numbers are on an issued
certificate.
"""
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal

from django.db.models import Sum

from attendance.aggregation import MinutesSource, attended_minutes

from .models import CreditAdjustment, EvaluationSubmission

ZERO = Decimal("0.00")

# A self-report this much higher than the recorded minutes gets a human look.
REVIEW_THRESHOLD_MINUTES = 15

REVIEW_CLAIMS_MORE = "claims more than was recorded"
REVIEW_SELF_REPORTED_ONLY = "self-reported only"
REVIEW_OVER_SESSION_LENGTH = "rows add up to more than a session lasted (duplicate manual row?)"


def credits_for_minutes(minutes):
    """
    Credit is hours attended: minutes / 60, to the hundredth.

    59 minutes of a 60-minute talk is 59/60 of a credit, 0.98. No rounding
    to a quarter. The only rounding is to two decimal places, and that is
    downward, so a printed figure never overstates.
    """
    if minutes <= 0:
        return ZERO
    return (Decimal(minutes) / 60).quantize(ZERO, rounding=ROUND_FLOOR)


def evaluation_gate(person, session):
    """
    Is this session's credit unlocked? Yes when the person has a complete
    evaluation of THAT session. Each session is gated on its own.
    """
    return (
        EvaluationSubmission.objects.for_person(person)
        .filter(session=session, is_complete=True)
        .exists()
    )


@dataclass(frozen=True)
class SessionCredit:
    """One person's standing on one session."""

    session: object
    minutes: int  # creditable minutes: recorded, or the self-report fallback
    source: str | None  # a MinutesSource value; None when there is nothing at all
    attended: bool  # at least half the session, by recorded attendance
    evaluated: bool  # a complete evaluation of this session exists
    self_reported_minutes: int | None  # None when there is no evaluation
    review_reasons: tuple = ()

    @property
    def credited_minutes(self):
        """
        What counts toward the event's credit: the minutes as recorded, however
        few (five minutes of a talk is five minutes), and nothing until the
        session is evaluated.
        """
        return self.minutes if self.evaluated else 0


@dataclass(frozen=True)
class CreditBreakdown:
    """Everything behind one person's credit for one event. What a certificate line snapshots."""

    sessions: list = field(default_factory=list)  # SessionCredit, in session order
    source: str | None = None
    computed_credits: Decimal = ZERO
    adjustment_credits: Decimal = ZERO

    @property
    def credits(self):
        return max(self.computed_credits + self.adjustment_credits, ZERO)

    @property
    def minutes(self):
        return sum(s.minutes for s in self.sessions)

    @property
    def credited_minutes(self):
        return sum(s.credited_minutes for s in self.sessions)

    @property
    def sessions_attended(self):
        return [s.session for s in self.sessions if s.attended]

    @property
    def sessions_evaluated(self):
        return [s.session for s in self.sessions if s.evaluated]

    @property
    def review_reasons(self):
        seen = []
        for s in self.sessions:
            for reason in s.review_reasons:
                if reason not in seen:
                    seen.append(reason)
        return tuple(seen)

    @property
    def needs_review(self):
        return bool(self.review_reasons)

    @property
    def self_reported_minutes(self):
        claimed = [
            s.self_reported_minutes for s in self.sessions if s.self_reported_minutes is not None
        ]
        return sum(claimed) if claimed else None


def adjustment_credits(person, event):
    """Net of the CreditAdjustment ledger for this person and event."""
    total = (
        CreditAdjustment.objects.for_person(person)
        .filter(event=event)
        .aggregate(total=Sum("delta_credits"))["total"]
    )
    return (total or ZERO).quantize(ZERO)


def creditable_time(person, event):
    """
    The minutes credit is based on, per session: a list of SessionCredit
    and the overall source.

    Recorded attendance wins whenever any exists for the event, even if a
    session's share is zero. Self-reported minutes are only the fallback
    for someone with no attendance rows at all (their connection died, or
    they phoned in under a name nobody could match), and only for the
    sessions they evaluated.

    review_reasons: a self-report more than REVIEW_THRESHOLD_MINUTES above
    the recorded minutes (the sign of a missing or unmatched row), credit
    resting on a self-report alone, or hours-only rows adding up to more
    than a session lasted.
    """
    recorded = attended_minutes(person, event)
    submissions = {
        s.session_id: s
        for s in EvaluationSubmission.objects.for_person(person).filter(session__event=event)
    }
    result = []
    for session in event.sessions.order_by("start_at", "position"):
        submission = submissions.get(session.pk)
        claimed = submission.self_reported_session_minutes if submission else None
        evaluated = bool(submission and submission.is_complete)
        reasons = []
        if recorded.has_rows:
            share = recorded.for_session(session)
            minutes = share.minutes
            source = recorded.source
            attended = share.attended
            if claimed is not None and claimed - minutes > REVIEW_THRESHOLD_MINUTES:
                reasons.append(REVIEW_CLAIMS_MORE)
            if share.capped_seconds:
                reasons.append(REVIEW_OVER_SESSION_LENGTH)
        elif claimed is not None:
            minutes = min(claimed, session.length_minutes)
            source = MinutesSource.SELF_REPORTED
            attended = False
            reasons.append(REVIEW_SELF_REPORTED_ONLY)
        else:
            minutes, source, attended = 0, None, False
        result.append(
            SessionCredit(
                session=session,
                minutes=minutes,
                source=source,
                attended=attended,
                evaluated=evaluated,
                self_reported_minutes=claimed,
                review_reasons=tuple(reasons),
            )
        )
    source = recorded.source
    if source is None and any(s.source == MinutesSource.SELF_REPORTED for s in result):
        source = MinutesSource.SELF_REPORTED
    return result, source


def credit_breakdown(person, event):
    """
    The credit calculation:

        per session: the minutes attended, counted only if that session has
                     a complete evaluation
        computed   = min(credits_for_minutes(sum of counted minutes),
                         event.accredited_credits)
        credits    = max(computed + adjustments, 0)

    Minutes are summed across the event first, so the hundredths are cut
    once, not once per session.
    """
    sessions, source = creditable_time(person, event)
    credited = sum(s.credited_minutes for s in sessions)
    computed = min(credits_for_minutes(credited), event.accredited_credits).quantize(ZERO)
    return CreditBreakdown(
        sessions=sessions,
        source=source,
        computed_credits=computed,
        adjustment_credits=adjustment_credits(person, event),
    )


def computed_credits(person, event):
    """Credit from attendance and evaluation alone, before adjustments."""
    return credit_breakdown(person, event).computed_credits


def event_credits(person, event):
    """What this person is owed for this event. The number a certificate line prints."""
    return credit_breakdown(person, event).credits
