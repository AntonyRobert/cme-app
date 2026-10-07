"""
The paper sign-in sheet, typed in.

Staff transcribe the sheet on a screen that lists the program's known people
with a checkbox per session. A tick is an AttendanceRecord with
source=signin_sheet claiming the whole session; it carries no upload and no
per-row reason, because the sheet is the reason and the sitting is logged
once. Ticks already recorded are shown locked: a tick entered by mistake is
withdrawn by superseding its row, never by deleting it.
"""
from dataclasses import dataclass, field

from django.db import transaction

from audit.log import record
from credits.models import EvaluationSubmission
from people.models import Person
from rounds.models import SessionPresenter

from .models import AttendanceRecord

SHEET = AttendanceRecord.Source.SIGNIN_SHEET
WALK_IN_ROWS = 5


def sheet_people(program):
    """
    Everyone known to the program: anyone who attended, evaluated or presented
    at one of its events. Merged duplicates are left out; their root is listed.
    """
    ids = set(
        AttendanceRecord.objects.active()
        .filter(event__program=program, person__isnull=False)
        .values_list("person", flat=True)
    )
    ids |= set(
        EvaluationSubmission.objects.filter(session__event__program=program).values_list(
            "person", flat=True
        )
    )
    ids |= set(
        SessionPresenter.objects.filter(session__event__program=program).values_list(
            "person", flat=True
        )
    )
    return list(
        Person.objects.filter(pk__in=ids, merged_into__isnull=True).order_by(
            "family_name", "given_name"
        )
    )


def existing_ticks(event):
    """{(person id, session id)} already recorded from a sheet for this event."""
    return set(
        AttendanceRecord.objects.active()
        .filter(event=event, source=SHEET, person__isnull=False)
        .values_list("person", "session")
    )


@dataclass
class SheetResult:
    rows: list = field(default_factory=list)  # AttendanceRecord written
    walk_ins: int = 0  # of those, unmatched rows typed by name
    already: int = 0  # ticks that were there before, left alone

    @property
    def written(self):
        return len(self.rows)


@transaction.atomic
def enter_sheet(event, ticks, walk_ins, *, user, request=None):
    """
    Write the ticks from one sitting with the sheet.

    `ticks` is {(person, session)} for people on the list; `walk_ins` is
    [(name, [session, ...])] for names typed in. Ticks already recorded are
    skipped, so re-submitting the same page adds nothing. Returns what was
    written; the sitting is audit-logged once with every row id.
    """
    result = SheetResult()
    have = existing_ticks(event)
    sessions = {s.pk: s for s in event.sessions.all()}
    for person, session in sorted(ticks, key=lambda t: (str(t[0]), t[1].position)):
        if session.pk not in sessions or session.event_id != event.pk:
            raise ValueError("That session is not part of this event.")
        if (person.pk, session.pk) in have:
            result.already += 1
            continue
        row = AttendanceRecord(
            event=event,
            session=session,
            person=person,
            source=SHEET,
            match_method=AttendanceRecord.MatchMethod.MANUAL,
            created_by=user,
        )
        row.full_clean()
        row.save()
        result.rows.append(row)
        have.add((person.pk, session.pk))
    for name, ticked in walk_ins:
        name = " ".join(name.split())
        if not name:
            continue
        for session in ticked:
            if session.pk not in sessions:
                raise ValueError("That session is not part of this event.")
            row = AttendanceRecord(
                event=event,
                session=session,
                person=None,
                raw_display_name=name[:300],
                source=SHEET,
                created_by=user,
            )
            row.full_clean()
            row.save()
            result.rows.append(row)
            result.walk_ins += 1
    record(
        "attendance.signin_sheet_entered",
        event,
        user=user,
        request=request,
        metadata={
            "rows": [str(r.pk) for r in result.rows],
            "written": result.written,
            "walk_ins": result.walk_ins,
            "already": result.already,
        },
    )
    return result
