"""
Three claims about one person, reconciled by the highest claim capped at
the session; then sign-off, which is what a certificate counts.

The event: three one-hour talks at 0, 60 and 120 minutes.
"""
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from attendance.aggregation import RECORDED, MinutesSource, attended_minutes
from attendance.models import AttendanceRecord, SessionAttendanceDecision
from attendance.signoff import (
    HELD_DISAGREE,
    HELD_TICK_ONLY,
    HELD_UNMATCHED,
    confirm_event,
    confirm_person,
    review,
    unconfirmed_sessions,
)
from audit.models import AuditLog
from core.models import ImmutableRowError
from credits.rules import REVIEW_SOURCES_DISAGREE, REVIEW_TICK_ONLY, credit_breakdown
from credits.tests.factories import evaluate
from people.tests.factories import make_person, make_staff
from programs.tests.factories import give_role, make_program, make_signer
from rounds.models import SessionPresenter
from rounds.tests.factories import make_event, make_session

from .factories import manual_row, teams_row

pytestmark = pytest.mark.django_db

D = Decimal


@pytest.fixture
def event():
    event = make_event(minutes=180, credits="3.00", sessions=0)
    for _ in range(3):
        make_session(event, minutes=60)
    return event


@pytest.fixture
def person():
    return make_person()


@pytest.fixture
def staff():
    return make_signer(username="signer")


def talks(event):
    return list(event.sessions.order_by("start_at"))


def scan(event, person, session, user=None):
    return AttendanceRecord.objects.create(
        source="qr_signin", event=event, session=session, person=person,
        match_method="manual", created_by=user or make_staff(),
    )


def tick(event, person, session, user=None):
    return AttendanceRecord.objects.create(
        source="signin_sheet", event=event, session=session, person=person,
        match_method="manual", created_by=user or make_staff(),
    )


def claims(person, event, index):
    return attended_minutes(person, event).sessions[index].claim_minutes


# --- Claims ------------------------------------------------------------------------


def test_a_tick_and_a_scan_each_claim_the_whole_session(event, person):
    first, second, _ = talks(event)
    tick(event, person, first)
    scan(event, person, second)
    result = attended_minutes(person, event)
    assert [s.minutes for s in result.sessions] == [60, 60, 0]
    assert claims(person, event, 0) == {"signin_sheet": 60}
    assert claims(person, event, 1) == {"qr_signin": 60}
    assert result.source == MinutesSource.MIXED


def test_three_sources_give_three_claims_and_one_figure_not_the_sum(event, person):
    first = talks(event)[0]
    teams_row(event, person, 0, 45)
    tick(event, person, first)
    scan(event, person, first)
    share = attended_minutes(person, event).sessions[0]
    assert share.claim_minutes == {RECORDED: 45, "signin_sheet": 60, "qr_signin": 60}
    assert share.minutes == 60  # the highest, not 165
    assert set(share.sources) == {RECORDED, "qr_signin", "signin_sheet"}


def test_the_highest_claim_is_capped_at_the_session(event, person):
    first = talks(event)[0]
    manual_row(event, person, minutes=90, session=first)  # a typo; sessions are 60 long
    share = attended_minutes(person, event).sessions[0]
    assert (share.minutes, share.capped_seconds) == (60, 30 * 60)


def test_teams_and_a_manual_correction_are_one_recorded_claim(event, person):
    """Staff completing the record is not a competing sensor: the laptop died, they phoned in."""
    first = talks(event)[0]
    teams_row(event, person, 0, 20)
    manual_row(event, person, minutes=35, session=first, reason="Phoned in after the laptop died")
    share = attended_minutes(person, event).sessions[0]
    assert share.claim_minutes == {RECORDED: 55}
    assert share.disagree is False


def test_sources_further_apart_than_the_programs_threshold_disagree(event, person):
    first = talks(event)[0]
    teams_row(event, person, 0, 40)
    scan(event, person, first)  # claims 60
    share = attended_minutes(person, event).sessions[0]
    assert share.minutes == 60 and share.disagree is True
    assert REVIEW_SOURCES_DISAGREE in credit_breakdown(person, event).review_reasons


def test_the_threshold_is_per_program(event, person):
    first = talks(event)[0]
    teams_row(event, person, 0, 56)
    scan(event, person, first)  # 4 minutes apart
    assert attended_minutes(person, event).sessions[0].disagree is False  # default 5
    event.program.attendance_disagreement_minutes = 3
    event.program.save()
    event.refresh_from_db()
    assert attended_minutes(person, event).sessions[0].disagree is True


def test_a_tick_only_session_is_flagged_even_though_nothing_disagrees(event, person):
    """The weakest evidence claiming the most minutes, with nothing to contradict it."""
    first, second, _ = talks(event)
    tick(event, person, first)
    teams_row(event, person, 60, 120)
    shares = attended_minutes(person, event).sessions
    assert (shares[0].tick_only, shares[0].disagree) == (True, False)
    assert shares[1].tick_only is False
    assert REVIEW_TICK_ONLY in credit_breakdown(person, event).review_reasons


def test_a_scan_backed_by_teams_is_not_tick_only(event, person):
    first = talks(event)[0]
    scan(event, person, first)
    teams_row(event, person, 0, 58)
    share = attended_minutes(person, event).sessions[0]
    assert (share.tick_only, share.disagree, share.minutes) == (False, False, 60)


def test_one_scan_per_person_per_session(event, person):
    first = talks(event)[0]
    scan(event, person, first)
    with pytest.raises(IntegrityError), transaction.atomic():
        scan(event, person, first)
    scan(event, person, talks(event)[1])


def test_whole_session_rows_need_a_session_and_have_no_times(event, person):
    with pytest.raises(IntegrityError), transaction.atomic():
        AttendanceRecord.objects.create(
            source="qr_signin", event=event, person=person, duration_seconds=60,
            match_method="manual", created_by=make_staff(),
        )


# --- Sign-off --------------------------------------------------------------------


def test_confirm_event_signs_off_agreeing_rows_and_holds_the_rest(event, staff):
    first, second, third = talks(event)
    agreeing = make_person()
    teams_row(event, agreeing, 0, 180)
    disagreeing = make_person()
    teams_row(event, disagreeing, 0, 30)
    scan(event, disagreeing, first)  # 30 vs 60
    ticked = make_person()
    tick(event, ticked, second)

    result = confirm_event(event, user=staff)

    assert len(result.confirmed) == 3  # the agreeing person's three sessions
    assert all(d.basis == "sources_agree" and d.confirmed_by == staff for d in result.confirmed)
    held = {(p, s.session): s.held_reasons for p, s in result.held}
    assert held[(disagreeing, first)] == [HELD_DISAGREE]
    assert held[(ticked, second)] == [HELD_TICK_ONLY]
    assert SessionAttendanceDecision.objects.filter(person=disagreeing).count() == 0
    entry = AuditLog.objects.get(action="attendance.event_confirmed")
    assert (entry.metadata["confirmed"], len(entry.metadata["held"])) == (3, 2)


def test_one_person_with_an_agreeing_and_a_disagreeing_session(event, person, staff):
    """The agreeing session is confirmed and the other held: one odd row does not block the person."""
    first, second, _ = talks(event)
    teams_row(event, person, 0, 120)
    scan(event, person, first)  # agrees: 60 and 60
    tick(event, person, second)
    manual_row(event, person, minutes=20, session=second, reason="Left to take a call")
    # second: recorded 60 (Teams) + 20 manual, capped 60; tick 60: agree.
    # Make them disagree instead: supersede nothing, just shorten Teams.
    AttendanceRecord.objects.filter(person=person, source="teams_upload").update(
        leave_at=first.end_at.replace(minute=30)
    )
    result = confirm_event(event, user=staff)
    confirmed_sessions = {d.session for d in result.confirmed if d.person == person}
    held_sessions = {s.session for p, s in result.held if p == person}
    assert first in confirmed_sessions or first in held_sessions  # whichever disagrees
    assert confirmed_sessions and held_sessions  # one of each, not all-or-nothing
    assert confirmed_sessions.isdisjoint(held_sessions)


def test_confirm_event_is_idempotent_and_skips_what_is_done(event, person, staff):
    teams_row(event, person, 0, 180)
    first = confirm_event(event, user=staff)
    second = confirm_event(event, user=staff)
    assert (len(first.confirmed), len(second.confirmed), second.skipped) == (3, 0, 3)
    assert SessionAttendanceDecision.objects.count() == 3


def test_unmatched_rows_block_sign_off_of_that_session(event, person, staff):
    first, second, _ = talks(event)
    teams_row(event, person, 0, 120)
    teams_row(event, None, 0, 30, raw_display_name="iPhone")  # unmatched, touches talk 1
    result = confirm_event(event, user=staff)
    assert [d.session for d in result.confirmed] == [second]
    (held_person, held_session), = [(p, s.session) for p, s in result.held]
    assert (held_person, held_session) == (person, first)
    assert dict(review(event)[0].sessions[0].held_reasons and [(0, HELD_UNMATCHED)]) == {0: HELD_UNMATCHED}
    with pytest.raises(ValidationError, match="unmatched"):
        confirm_person(event, person, {first.pk: 60}, user=staff)


def test_a_presenters_own_talk_is_not_signed_off_as_attendance(event, person, staff):
    first, second, _ = talks(event)
    SessionPresenter.objects.create(session=first, person=person)
    teams_row(event, person, 0, 120)
    result = confirm_event(event, user=staff)
    assert [d.session for d in result.confirmed] == [second]
    with pytest.raises(ValidationError, match="teaching"):
        confirm_person(event, person, {first.pk: 60}, user=staff)


def test_confirm_person_session_by_session_with_a_typed_figure(event, person, staff):
    first, second, third = talks(event)
    teams_row(event, person, 0, 30)
    scan(event, person, first)
    teams_row(event, person, 60, 180)
    with pytest.raises(ValidationError, match="comment"):
        confirm_person(event, person, {first.pk: 45, second.pk: 60}, user=staff)
    written = confirm_person(
        event, person, {first.pk: 45, second.pk: 60, third.pk: None},
        user=staff, comment="Was in the room; scan backs it, Teams dropped",
    )
    by_session = {d.session: d for d in written}
    assert by_session[first].confirmed_minutes == 45 and by_session[first].basis == "manual"
    assert by_session[second].basis == "sources_agree"
    assert third not in by_session
    assert by_session[first].based_on["claims"] == {RECORDED: 30, "qr_signin": 60}
    assert AuditLog.objects.filter(action="attendance.person_confirmed").exists()


def test_choosing_a_claim_is_recorded_as_such(event, person, staff):
    first = talks(event)[0]
    teams_row(event, person, 0, 30)
    scan(event, person, first)
    [decision] = confirm_person(event, person, {first.pk: 30}, user=staff, comment="Teams is right")
    assert decision.basis == "highest_claim"  # one of the claims, not the proposal


def test_a_correction_supersedes_and_needs_its_own_signoff(event, person, staff):
    first = talks(event)[0]
    teams_row(event, person, 0, 60)
    [original] = confirm_person(event, person, {first.pk: 60}, user=staff)
    [correction] = confirm_person(
        event, person, {first.pk: 40}, user=make_signer(username="second-signer"), comment="Left at 12:40 per the chair"
    )
    original.refresh_from_db()
    assert correction.supersedes == original and not original.is_current
    assert SessionAttendanceDecision.objects.current().filter(person=person).get() == correction
    # The original is untouched, append-only.
    original.confirmed_minutes = 1
    with pytest.raises(ImmutableRowError):
        original.save()
    with pytest.raises(ImmutableRowError):
        correction.delete()


def test_a_decision_cannot_exceed_the_session(event, person, staff):
    first = talks(event)[0]
    teams_row(event, person, 0, 60)
    with pytest.raises(ValidationError):
        confirm_person(event, person, {first.pk: 61}, user=staff, comment="typo")


# --- Credit: proposed versus confirmed -----------------------------------------


def test_credit_is_proposed_until_signed_off_and_then_confirmed(event, person, staff):
    first, second, third = talks(event)
    teams_row(event, person, 0, 180)
    for talk in talks(event):
        evaluate(person, talk)
    before = credit_breakdown(person, event)
    assert (before.attendance_credits, before.attendance_confirmed_credits) == (D("3.00"), D("0.00"))
    assert before.awaiting_signoff == [first, second, third]
    confirm_event(event, user=staff)
    after = credit_breakdown(person, event)
    assert (after.attendance_credits, after.attendance_confirmed_credits) == (D("3.00"), D("3.00"))
    assert after.fully_confirmed


def test_confirmed_credit_follows_the_confirmed_figure_not_the_proposal(event, person, staff):
    first = talks(event)[0]
    teams_row(event, person, 0, 60)
    evaluate(person, first)
    confirm_person(event, person, {first.pk: 30}, user=staff, comment="Half")
    b = credit_breakdown(person, event)
    assert (b.attendance_credits, b.attendance_confirmed_credits) == (D("1.00"), D("0.50"))


def test_sign_off_does_not_replace_the_evaluation_gate(event, person, staff):
    teams_row(event, person, 0, 60)
    confirm_event(event, user=staff)
    assert credit_breakdown(person, event).attendance_confirmed_credits == D("0.00")


def test_teaching_needs_no_sign_off(event, person):
    SessionPresenter.objects.create(session=talks(event)[0], person=person)
    b = credit_breakdown(person, event)
    assert (b.teaching_credits, b.fully_confirmed) == (D("1.00"), True)


def test_unconfirmed_sessions_lists_what_is_waiting(event, person, staff):
    first, second, _ = talks(event)
    teams_row(event, person, 0, 120)
    assert unconfirmed_sessions(person, event) == [first, second]
    confirm_person(event, person, {first.pk: 60}, user=staff)
    assert unconfirmed_sessions(person, event) == [second]


# --- Who may sign off ------------------------------------------------------------


def test_sign_off_is_program_admin_work_and_program_scoped(event, person):
    """
    Sign-off decides the numbers certificates are built from, so it needs
    the sign_off_attendance permission (Program admin group only) AND the
    program-admin role in that event's program. A program admin elsewhere,
    even one who is a coordinator here, may not sign here.
    """
    from django.core.exceptions import PermissionDenied

    first, _, _ = talks(event)
    teams_row(event, person, 0, 60)
    here, elsewhere = event.program, make_program("Elsewhere")

    coordinator = make_staff("coordinator")
    give_role(coordinator, here, "coordinator")
    admin_elsewhere = make_signer(elsewhere, username="admin-elsewhere")
    give_role(admin_elsewhere, here, "coordinator")
    reader = make_staff("reader")
    give_role(reader, here, "read_only")

    for user in (coordinator, admin_elsewhere, reader, make_staff("nobody")):
        with pytest.raises(PermissionDenied):
            confirm_event(event, user=user)
        with pytest.raises(PermissionDenied):
            confirm_person(event, person, {first.pk: 60}, user=user)
    assert SessionAttendanceDecision.objects.count() == 0

    signer = make_signer(here, username="admin-here")
    assert len(confirm_event(event, user=signer).confirmed) == 1
    assert not admin_elsewhere.has_perm("attendance.sign_off_attendance") is False  # has the perm...
    from attendance.signoff import can_sign_off

    assert can_sign_off(admin_elsewhere, elsewhere) and not can_sign_off(admin_elsewhere, here)
    assert not can_sign_off(coordinator, here)


def test_the_permission_belongs_to_the_program_admin_group_only():
    from accounts.roles import COORDINATOR, PROGRAM_ADMIN, READ_ONLY, role_permissions

    perms = role_permissions()
    assert "attendance.sign_off_attendance" in perms[PROGRAM_ADMIN]
    assert "attendance.sign_off_attendance" not in perms[COORDINATOR]
    assert "attendance.sign_off_attendance" not in perms[READ_ONLY]


# --- Where an event stands, for the event list -------------------------------------


def test_status_tells_held_rows_from_rows_that_arrived_after_sign_off(event, person, staff):
    from attendance.signoff import (
        STATE_IN_PROGRESS,
        STATE_NEEDS_ANOTHER_LOOK,
        STATE_NONE,
        STATE_NOT_STARTED,
        STATE_SIGNED_OFF,
        signoff_status,
    )

    first, second, third = talks(event)
    assert signoff_status(event).state == STATE_NONE

    teams_row(event, person, 0, 60)  # first: clean
    teams_row(event, person, 60, 90)  # second: Teams says 30
    tick(event, person, second)  # the sheet says 60: held as a disagreement
    assert signoff_status(event).state == STATE_NOT_STARTED

    confirm_event(event, user=staff)
    status = signoff_status(event)
    assert (status.total, status.decided, status.undecided, status.new) == (2, 1, 1, 0)
    assert status.state == STATE_IN_PROGRESS  # the held row is being worked, nothing new

    confirm_person(event, person, {second.pk: 60}, user=staff)
    assert signoff_status(event).state == STATE_SIGNED_OFF

    # A sheet typed in a week later: rows arrived after the last sign-off.
    tick(event, person, third)
    status = signoff_status(event)
    assert (status.undecided, status.new, status.state) == (1, 1, STATE_NEEDS_ANOTHER_LOOK)
    assert status.blocking == 0  # not evaluated: earns nothing, blocks nothing
    evaluate(person, third)
    assert signoff_status(event).blocking == 1


def test_status_notices_a_proposal_that_moved_under_a_decision(event, person, staff):
    from attendance.signoff import STATE_NEEDS_ANOTHER_LOOK, signoff_status
    from attendance.services import supersede_rows

    first, _, _ = talks(event)
    row = teams_row(event, person, 0, 60)
    confirm_event(event, user=staff)
    correction = manual_row(event, person, start=0, end=30, reason="Left at half past, per the chair")
    supersede_rows([row], correction, user=staff)

    status = signoff_status(event)
    assert (status.decided, status.stale, status.state) == (1, 1, STATE_NEEDS_ANOTHER_LOOK)
