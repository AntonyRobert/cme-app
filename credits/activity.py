"""
The overall-activity evaluation: the standard asks for an opportunity to
evaluate each session and the CPD activity as a whole. Offered per event
when the program has an activity form; never gates credit.

Program.activity_evaluation_cadence is stored for the day CPD says whether
it wants this per event or once a year; only per event is built.
"""
from attendance.aggregation import attended_minutes

from .evaluation_forms import resolve_activity_form
from .models import EvaluationSubmission
from .reports import person_events


def activity_offered(event):
    """Is an overall-activity evaluation offered for this event?"""
    return resolve_activity_form(event.program).form is not None and not event.is_closed


def activities_to_evaluate(person):
    """[event] the person attended, with an activity form offered, not yet completely evaluated."""
    done = set(
        EvaluationSubmission.objects.for_person(person)
        .for_events()
        .filter(is_complete=True)
        .values_list("event", flat=True)
    )
    events = []
    for event in person_events(person):
        if event.pk in done or not activity_offered(event):
            continue
        if attended_minutes(person, event).sessions_attended:
            events.append(event)
    return events
