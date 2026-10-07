"""
The credit rules. One function per rule that might change.

Nothing here is stored. Credit is derived from attendance, presenting and
evaluation every time it is asked for; the only frozen numbers are on an
issued certificate.

There are two kinds of credit, kept apart all the way to the certificate:

- TEACHING: for each session the person presented (SessionPresenter),
  the session's full length. Granted on presenting alone, no evaluation
  needed (provisional, pending McGill CPD). Not Teams minutes: a
  presenter is by definition present for their own talk.
- ATTENDANCE: for every other session, the minutes attended, counted
  only once that session's evaluation is complete. Time inside a session
  the person presented is never attendance.

Who presented comes ONLY from SessionPresenter. The Teams meeting role on
attendance rows is a screen-sharing permission and is never read here.
"""
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal

from django.db.models import Sum

from attendance.aggregation import MinutesSource, attended_minutes
from people.identity import identity_ids
from rounds.models import SessionPresenter

from .models import CreditAdjustment, CreditKind, EvaluationSubmission

ZERO = Decimal("0.00")

ATTENDANCE = CreditKind.ATTENDANCE
TEACHING = CreditKind.TEACHING

# A self-report this much higher than the recorded minutes gets a human look.
REVIEW_THRESHOLD_MINUTES = 15

REVIEW_CLAIMS_MORE = "claims more than was recorded"
REVIEW_SELF_REPORTED_ONLY = "self-reported only"
REVIEW_OVER_SESSION_LENGTH = "rows add up to more than a session lasted (duplicate manual row?)"
REVIEW_SOURCES_DISAGREE = "attendance sources disagree"
REVIEW_TICK_ONLY = "only a sign-in tick or scan says they were there"


def rate_per_hour(program, kind):
    """Credits per hour for a kind of credit, from the program's own row."""
    if kind == TEACHING:
        return Decimal(program.teaching_rate_per_hour)
    return Decimal(program.attendance_rate_per_hour)


def credits_for_minutes(minutes, rate=Decimal("1")):
    """
    Credit is hours times the rate: minutes / 60 * rate, to the hundredth.

    59 minutes of a 60-minute talk is 0.98 at a rate of 1. No rounding to a
    quarter. The only rounding is to two decimal places, and that is
    downward, so a figure never overstates.
    """
    if minutes <= 0:
        return ZERO
    return (Decimal(minutes) / 60 * Decimal(rate)).quantize(ZERO, rounding=ROUND_FLOOR)


def evaluation_gate(person, session):
    """
    Is this session's ATTENDANCE credit unlocked? Yes when the person has a
    complete evaluation of that session. Teaching credit has no gate.
    """
    return (
        EvaluationSubmission.objects.for_person(person)
        .filter(session=session, is_complete=True)
        .exists()
    )


def presented_session_ids(person, event):
    """Sessions of the event this person presented, following merges."""
    return set(
        SessionPresenter.objects.filter(
            session__event=event, person_id__in=identity_ids(person)
        ).values_list("session_id", flat=True)
    )


@dataclass(frozen=True)
class SessionCredit:
    """One person's standing on one session."""

    session: object
    minutes: int  # minutes in the room: recorded, or the self-report fallback
    source: str | None  # a MinutesSource value; None when there is nothing at all
    attended: bool  # at least half the session as an attendee (never their own talk)
    evaluated: bool  # a complete evaluation of this session exists
    self_reported_minutes: int | None  # None when there is no evaluation
    presented: bool = False  # in SessionPresenter for this session
    # Minutes a staff member has signed off, or None while nothing has been.
    confirmed_minutes: int | None = None
    review_reasons: tuple = ()

    @property
    def credited_minutes(self):
        """
        Attendance minutes that count toward PROPOSED credit: the minutes as
        recorded, however few, once the session is evaluated. None for a
        session they presented: that time is teaching.
        """
        if self.presented:
            return 0
        return self.minutes if self.evaluated else 0

    @property
    def confirmed_credited_minutes(self):
        """Attendance minutes that count toward CONFIRMED credit: signed off and evaluated."""
        if self.presented or self.confirmed_minutes is None:
            return 0
        return self.confirmed_minutes if self.evaluated else 0

    @property
    def awaiting_signoff(self):
        """Has minutes on record but nobody has signed them off yet."""
        return not self.presented and self.minutes > 0 and self.confirmed_minutes is None

    @property
    def blocks_certificate(self):
        """
        Awaiting sign-off AND evaluated: the minutes would carry credit, so a
        certificate cannot print until someone signs them. Unevaluated minutes
        earn nothing either way and block nothing.
        """
        return self.awaiting_signoff and self.evaluated

    @property
    def teaching_minutes(self):
        """The whole session, for a session they presented. Not Teams minutes."""
        return self.session.length_minutes if self.presented else 0


@dataclass(frozen=True)
class CreditBreakdown:
    """
    Everything behind one person's credit for one event, by kind. What a
    certificate line snapshots. There is deliberately no blended "credits"
    figure to print; `total_credits` exists for sorting and summaries only.
    """

    sessions: list = field(default_factory=list)  # SessionCredit, in session order
    source: str | None = None
    # The program's rates when this was computed; what a certificate line snapshots.
    attendance_rate_per_hour: Decimal = Decimal("1.00")
    teaching_rate_per_hour: Decimal = Decimal("1.00")
    attendance_computed: Decimal = ZERO  # from proposed minutes
    attendance_confirmed_computed: Decimal = ZERO  # from signed-off minutes only
    attendance_adjustment: Decimal = ZERO
    teaching_computed: Decimal = ZERO
    teaching_adjustment: Decimal = ZERO

    @property
    def attendance_credits(self):
        """Proposed attendance credit: what the credits page shows, marked pending."""
        return max(self.attendance_computed + self.attendance_adjustment, ZERO)

    @property
    def attendance_confirmed_credits(self):
        """Confirmed attendance credit: what a certificate prints."""
        return max(self.attendance_confirmed_computed + self.attendance_adjustment, ZERO)

    @property
    def awaiting_signoff(self):
        return [s.session for s in self.sessions if s.awaiting_signoff]

    @property
    def fully_confirmed(self):
        return not self.awaiting_signoff

    @property
    def blocking_certificate(self):
        return [s.session for s in self.sessions if s.blocks_certificate]

    @property
    def teaching_credits(self):
        return max(self.teaching_computed + self.teaching_adjustment, ZERO)

    @property
    def total_credits(self):
        return self.attendance_credits + self.teaching_credits

    @property
    def minutes(self):
        return sum(s.minutes for s in self.sessions)

    @property
    def credited_minutes(self):
        """Attendance minutes that count toward credit."""
        return sum(s.credited_minutes for s in self.sessions)

    @property
    def teaching_minutes(self):
        return sum(s.teaching_minutes for s in self.sessions)

    @property
    def sessions_attended(self):
        return [s.session for s in self.sessions if s.attended]

    @property
    def sessions_presented(self):
        return [s.session for s in self.sessions if s.presented]

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


def adjustment_credits(person, event, kind=ATTENDANCE):
    """Net of the CreditAdjustment ledger for this person, event and kind."""
    total = (
        CreditAdjustment.objects.for_person(person)
        .filter(event=event, kind=kind)
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

    Sessions the person presented are marked `presented`; their time is
    teaching, so they are never "attended", never need an evaluation and
    raise no review flags.

    review_reasons: a self-report more than REVIEW_THRESHOLD_MINUTES above
    the recorded minutes (the sign of a missing or unmatched row), credit
    resting on a self-report alone, or hours-only rows adding up to more
    than a session lasted.
    """
    from attendance.signoff import confirmed_minutes as signed_off

    recorded = attended_minutes(person, event)
    presented = presented_session_ids(person, event)
    confirmed = signed_off(person, event)
    submissions = {
        s.session_id: s
        for s in EvaluationSubmission.objects.for_person(person).filter(session__event=event)
    }
    result = []
    for session in event.sessions.order_by("start_at", "position"):
        is_presenter = session.pk in presented
        submission = submissions.get(session.pk)
        claimed = submission.self_reported_session_minutes if submission else None
        evaluated = bool(submission and submission.is_complete)
        reasons = []
        if recorded.has_rows:
            share = recorded.for_session(session)
            minutes = share.minutes
            source = recorded.source
            attended = share.attended and not is_presenter
            if not is_presenter:
                if claimed is not None and claimed - minutes > REVIEW_THRESHOLD_MINUTES:
                    reasons.append(REVIEW_CLAIMS_MORE)
                if share.capped_seconds:
                    reasons.append(REVIEW_OVER_SESSION_LENGTH)
                if share.disagree:
                    reasons.append(REVIEW_SOURCES_DISAGREE)
                if share.tick_only:
                    reasons.append(REVIEW_TICK_ONLY)
        elif claimed is not None and not is_presenter:
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
                presented=is_presenter,
                confirmed_minutes=confirmed.get(session.pk),
                review_reasons=tuple(reasons),
            )
        )
    source = recorded.source
    if source is None and any(s.source == MinutesSource.SELF_REPORTED for s in result):
        source = MinutesSource.SELF_REPORTED
    return result, source


def credit_breakdown(person, event):
    """
    The credit calculation, per kind:

        attendance = min(credits_for_minutes(sum of evaluated, non-presented
                         session minutes), event.accredited_credits)
                     + attendance adjustments, never below 0
        teaching   = credits_for_minutes(sum of presented session lengths)
                     + teaching adjustments, never below 0

    accredited_credits caps attendance only: it is what the event is
    accredited for as an attended activity. Minutes are summed across the
    event first, so hundredths are cut once per kind, not per session.
    The rates come from the event's program and are returned alongside, so
    a certificate line can snapshot what they were.

    Attendance exists twice: PROPOSED, from attended_minutes, which the
    credits page shows as pending; and CONFIRMED, from the current
    SessionAttendanceDecision per session, which a certificate prints.
    Teaching needs no sign-off: it is the length of the talks they gave.
    """
    sessions, source = creditable_time(person, event)
    program = event.program
    attendance_rate = rate_per_hour(program, ATTENDANCE)
    teaching_rate = rate_per_hour(program, TEACHING)
    attendance = min(
        credits_for_minutes(sum(s.credited_minutes for s in sessions), attendance_rate),
        event.accredited_credits,
    ).quantize(ZERO)
    confirmed = min(
        credits_for_minutes(sum(s.confirmed_credited_minutes for s in sessions), attendance_rate),
        event.accredited_credits,
    ).quantize(ZERO)
    teaching = credits_for_minutes(sum(s.teaching_minutes for s in sessions), teaching_rate)
    return CreditBreakdown(
        sessions=sessions,
        source=source,
        attendance_rate_per_hour=attendance_rate,
        teaching_rate_per_hour=teaching_rate,
        attendance_computed=attendance,
        attendance_confirmed_computed=confirmed,
        attendance_adjustment=adjustment_credits(person, event, ATTENDANCE),
        teaching_computed=teaching,
        teaching_adjustment=adjustment_credits(person, event, TEACHING),
    )


def computed_credits(person, event):
    """Attendance credit from attendance and evaluation alone, before adjustments."""
    return credit_breakdown(person, event).attendance_computed


def event_credits(person, event):
    """Attendance credit this person is owed for this event."""
    return credit_breakdown(person, event).attendance_credits


def teaching_credits(person, event):
    """Teaching credit this person is owed for this event."""
    return credit_breakdown(person, event).teaching_credits
