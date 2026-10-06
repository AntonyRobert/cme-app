"""
The credit rules. One function per rule that might change.

Nothing here is stored. Credit is derived from attendance and evaluation
every time it is asked for; the only frozen numbers are on an issued
certificate.
"""
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from django.db.models import Sum

from attendance.aggregation import MinutesSource, attended_minutes
from core.constraints import QUARTER

from .models import CreditAdjustment, EvaluationSubmission

ZERO = Decimal("0.00")

# Recorded and self-reported minutes further apart than this get a human look.
REVIEW_THRESHOLD_MINUTES = 15


def round_credits(hours):
    """
    Round DOWN to the nearest quarter credit.

    Down, because overstating credit is the error that can't be recovered
    from. Called once per event, so a certificate's total is exactly the sum
    of its lines.
    """
    hours = Decimal(hours)
    if hours <= 0:
        return ZERO
    return ((hours / QUARTER).to_integral_value(rounding=ROUND_FLOOR) * QUARTER).quantize(ZERO)


def evaluation_gate(person, event):
    """
    Has this person done the evaluation that credit for this event requires?

    THE RULE IS STILL OPEN (docs/decisions.md). This is the lenient
    candidate: at least one complete evaluation for any session of the
    event. Change the rule here and nowhere else.
    """
    return (
        EvaluationSubmission.objects.for_person(person)
        .filter(session__event=event, is_complete=True)
        .exists()
    )


@dataclass(frozen=True)
class CreditableTime:
    minutes: int
    source: str | None  # a MinutesSource value; None when there is nothing at all
    recorded_minutes: int | None  # None when there are no attendance rows
    self_reported_minutes: int | None  # None when there are no evaluations
    needs_review: bool


def self_reported_minutes(person, event):
    """Sum of what the person claimed across the event's sessions, or None."""
    return (
        EvaluationSubmission.objects.for_person(person)
        .filter(session__event=event)
        .aggregate(total=Sum("self_reported_minutes"))["total"]
    )


def creditable_minutes(person, event):
    """
    The minutes credit is based on.

    Recorded attendance wins whenever any exists, even if it adds up to
    zero. Self-reported minutes are only the fallback for someone with no
    attendance rows at all (their connection died, or they phoned in under
    a name nobody could match).

    needs_review is set when the two disagree by more than
    REVIEW_THRESHOLD_MINUTES, and whenever credit rests on a self-report
    alone, so a person never gets credit from an unchecked claim silently.
    """
    recorded = attended_minutes(person, event)
    claimed = self_reported_minutes(person, event)
    if recorded.has_rows:
        diverges = claimed is not None and abs(claimed - recorded.minutes) > REVIEW_THRESHOLD_MINUTES
        return CreditableTime(
            minutes=recorded.minutes,
            source=recorded.source,
            recorded_minutes=recorded.minutes,
            self_reported_minutes=claimed,
            needs_review=diverges,
        )
    if claimed is None:
        return CreditableTime(0, None, None, None, False)
    window_start, window_end = event.credit_window()
    window_minutes = int((window_end - window_start).total_seconds()) // 60
    return CreditableTime(
        minutes=min(claimed, window_minutes),
        source=MinutesSource.SELF_REPORTED,
        recorded_minutes=None,
        self_reported_minutes=claimed,
        needs_review=True,
    )


def adjustment_credits(person, event):
    """Net of the CreditAdjustment ledger for this person and event."""
    total = (
        CreditAdjustment.objects.for_person(person)
        .filter(event=event)
        .aggregate(total=Sum("delta_credits"))["total"]
    )
    return (total or ZERO).quantize(ZERO)


@dataclass(frozen=True)
class CreditBreakdown:
    """Everything behind one person's credit for one event. What a certificate line snapshots."""

    time: CreditableTime
    gate_passed: bool
    computed_credits: Decimal
    adjustment_credits: Decimal

    @property
    def credits(self):
        return max(self.computed_credits + self.adjustment_credits, ZERO)


def credit_breakdown(person, event):
    """
    The credit calculation:

        computed = 0 if the evaluation gate fails, otherwise
                   min(round_credits(minutes / 60), event.accredited_credits)
        credits  = max(computed + adjustments, 0)
    """
    time = creditable_minutes(person, event)
    gate_passed = evaluation_gate(person, event)
    if gate_passed:
        computed = min(
            round_credits(Decimal(time.minutes) / 60), event.accredited_credits
        ).quantize(ZERO)
    else:
        computed = ZERO
    return CreditBreakdown(
        time=time,
        gate_passed=gate_passed,
        computed_credits=computed,
        adjustment_credits=adjustment_credits(person, event),
    )


def computed_credits(person, event):
    """Credit from attendance and evaluation alone, before adjustments."""
    return credit_breakdown(person, event).computed_credits


def event_credits(person, event):
    """What this person is owed for this event. The number a certificate line prints."""
    return credit_breakdown(person, event).credits
