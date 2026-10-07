"""
Reordering rows whose position is unique within their parent.

Swapping 1 and 2 means one row briefly holds the other's number. If the
unique constraint is checked row by row, the first save fails.
"""
import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from people.tests.factories import make_person
from rounds.models import LearningObjective, Session, SessionPresenter
from rounds.tests.factories import make_event, make_session

pytestmark = pytest.mark.django_db


@pytest.fixture
def boss(client):
    user = get_user_model().objects.create_superuser(username="boss", password="x" * 20)
    client.force_login(user)
    return user


def test_swapping_two_sessions_on_the_event_page(client, boss):
    event = make_event(minutes=120, credits="2.00", sessions=0)
    first = make_session(event, minutes=60, title="First")
    second = make_session(event, minutes=60, title="Second")
    local = lambda v: v.astimezone().strftime("%H:%M:%S")
    data = {
        "title": event.title, "date": event.date.isoformat(), "status": "draft",
        "accredited_credits": "2.00",
        "start_at_0": event.start_at.astimezone().date().isoformat(), "start_at_1": local(event.start_at),
        "end_at_0": event.end_at.astimezone().date().isoformat(), "end_at_1": local(event.end_at),
        "teams_join_url": "", "teams_meeting_id": "",
        "sessions-TOTAL_FORMS": 2, "sessions-INITIAL_FORMS": 2,
    }
    for i, (s, pos) in enumerate([(first, 2), (second, 1)]):
        data.update({
            f"sessions-{i}-id": s.pk, f"sessions-{i}-event": event.pk,
            f"sessions-{i}-position": pos, f"sessions-{i}-title": s.title,
            f"sessions-{i}-start_at_0": s.start_at.astimezone().date().isoformat(),
            f"sessions-{i}-start_at_1": local(s.start_at),
            f"sessions-{i}-end_at_0": s.end_at.astimezone().date().isoformat(),
            f"sessions-{i}-end_at_1": local(s.end_at),
        })
    response = client.post(reverse("admin:rounds_roundsevent_change", args=[event.pk]), data)
    assert response.status_code == 302, [
        f.errors for fs in response.context["inline_admin_formsets"] for f in fs.formset.forms
    ]
    first.refresh_from_db()
    second.refresh_from_db()
    assert (first.position, second.position) == (2, 1)


def test_swapping_presenters_and_objectives_on_the_session_page(client, boss):
    event = make_event()
    session = event.sessions.get()
    a, b = make_person(), make_person()
    pa = SessionPresenter.objects.create(session=session, person=a, position=1)
    pb = SessionPresenter.objects.create(session=session, person=b, position=2)
    oa = LearningObjective.objects.create(session=session, position=1, text="A")
    ob = LearningObjective.objects.create(session=session, position=2, text="B")
    local = lambda v: v.astimezone().strftime("%H:%M:%S")
    response = client.post(reverse("admin:rounds_session_change", args=[session.pk]), {
        "event": event.pk, "position": 1, "title": session.title,
        "start_at_0": session.start_at.astimezone().date().isoformat(), "start_at_1": local(session.start_at),
        "end_at_0": session.end_at.astimezone().date().isoformat(), "end_at_1": local(session.end_at),
        "draft_blurb": "", "published_blurb": "",
        "session_presenters-TOTAL_FORMS": 2, "session_presenters-INITIAL_FORMS": 2,
        "session_presenters-0-id": pa.pk, "session_presenters-0-person": a.pk, "session_presenters-0-position": 2,
        "session_presenters-1-id": pb.pk, "session_presenters-1-person": b.pk, "session_presenters-1-position": 1,
        "objectives-TOTAL_FORMS": 2, "objectives-INITIAL_FORMS": 2,
        "objectives-0-id": oa.pk, "objectives-0-position": 2, "objectives-0-text": "A",
        "objectives-1-id": ob.pk, "objectives-1-position": 1, "objectives-1-text": "B",
    })
    assert response.status_code == 302, [
        f.errors for fs in response.context["inline_admin_formsets"] for f in fs.formset.forms
    ]
    pa.refresh_from_db(); pb.refresh_from_db(); oa.refresh_from_db(); ob.refresh_from_db()
    assert (pa.position, pb.position, oa.position, ob.position) == (2, 1, 2, 1)


def test_two_rows_still_cannot_end_up_with_the_same_number(client, boss):
    event = make_event()
    session = event.sessions.get()
    oa = LearningObjective.objects.create(session=session, position=1, text="A")
    ob = LearningObjective.objects.create(session=session, position=2, text="B")
    local = lambda v: v.astimezone().strftime("%H:%M:%S")
    response = client.post(reverse("admin:rounds_session_change", args=[session.pk]), {
        "event": event.pk, "position": 1, "title": session.title,
        "start_at_0": session.start_at.astimezone().date().isoformat(), "start_at_1": local(session.start_at),
        "end_at_0": session.end_at.astimezone().date().isoformat(), "end_at_1": local(session.end_at),
        "draft_blurb": "", "published_blurb": "",
        "session_presenters-TOTAL_FORMS": 0, "session_presenters-INITIAL_FORMS": 0,
        "objectives-TOTAL_FORMS": 2, "objectives-INITIAL_FORMS": 2,
        "objectives-0-id": oa.pk, "objectives-0-position": 1, "objectives-0-text": "A",
        "objectives-1-id": ob.pk, "objectives-1-position": 1, "objectives-1-text": "B",
    })
    assert response.status_code == 200  # redisplayed with an error, not a crash
    ob.refresh_from_db()
    assert ob.position == 2
