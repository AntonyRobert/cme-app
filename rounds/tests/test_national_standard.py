"""
The National Standard disclosure form: its five categories render with
their own version's wording, two columns per yes, the top-level binary,
role, speaker-only questions, the review flag, the slide text, per-activity
confirmation, and the publishing block.
"""
import datetime

import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditLog
from people.models import AllowedDomain
from people.tests.factories import make_person
from programs.tests.factories import make_program
from rounds.coi import confirm_for_session, declare, declare_no_conflicts, slide_text
from rounds.models import COIDeclaration, RoundsEvent, SessionPresenter, coi_questions
from rounds.tests.factories import EVENT_START, make_event, make_session

pytestmark = pytest.mark.django_db

V = "v2026-national-standard"
NONE = {key: (False, "", "") for key, _ in coi_questions(V)}


def speaker_declaration(person, conflicts=None, **extra):
    answers = dict(NONE)
    for key, (orgs, desc) in (conflicts or {}).items():
        answers[key] = (True, orgs, desc)
    kwargs = dict(role="speaker", has_relationships=bool(conflicts), off_label=False, generic_names=True, attested=True)
    kwargs.update(extra)
    return declare(person, answers, version=V, **kwargs)


# --- The categories and their wording ------------------------------------------------------


def test_the_five_categories_render_with_their_own_versions_wording_after_a_later_edit(settings):
    person = make_person()
    declaration = speaker_declaration(person, {"patents": ("Acme Devices", "Co-inventor")})
    texts = [text for text, _, _, _ in declaration.rendered()]
    assert texts == [
        "Any direct financial payments including receipt of honoraria",
        "Membership on advisory boards or speakers' bureaus",
        "Funded grants or clinical trials",
        "Patents on a drug, product or device",
        "All other investments or relationships that could be seen by a reasonable, well-informed "
        "participant as having the potential to influence the content of the educational activity",
    ]
    # A later version rewords; the declaration keeps the wording it was made under.
    settings.COI_QUESTIONS = {
        **settings.COI_QUESTIONS,
        "v2027-reworded": [(k, t.upper()) for k, t in settings.COI_QUESTIONS[V]],
    }
    settings.COI_NATIONAL_STANDARD_VERSIONS = {V, "v2027-reworded"}
    assert [text for text, _, _, _ in declaration.rendered()] == texts
    assert declaration.rendered()[3] == ("Patents on a drug, product or device", True, "Acme Devices", "Co-inventor")
    # And the retired provisional set still renders for its own declarations.
    old = declare_no_conflicts(make_person(), version="2026-10")
    assert [text for text, _, _, _ in old.rendered()][0] == "Research funding or grants"


def test_both_columns_are_required_when_a_category_is_yes():
    person = make_person()
    with pytest.raises(ValidationError) as err:
        speaker_declaration(person, {"grants_trials": ("", "A trial")})
    assert "grants_trials_organizations" in err.value.message_dict
    with pytest.raises(ValidationError) as err:
        speaker_declaration(person, {"grants_trials": ("Pharma Co", "   ")})
    assert "grants_trials_description" in err.value.message_dict
    assert COIDeclaration.objects.filter(person=person).count() == 0
    declaration = speaker_declaration(person, {"grants_trials": ("Pharma Co", "Site investigator, 2025")})
    row = declaration.responses.get(question_key="grants_trials")
    assert (row.organizations, row.relationship_description) == ("Pharma Co", "Site investigator, 2025")
    # A no carries nothing in either column.
    assert all(r.organizations == "" and r.relationship_description == "" for r in declaration.responses.exclude(question_key="grants_trials"))


def test_the_no_relationship_radio_writes_an_explicit_no_to_every_category():
    person = make_person()
    declaration = declare(person, {}, version=V, role="author", has_relationships=False, attested=True)
    assert declaration.has_relationships is False
    assert [yes for _, yes, _, _ in declaration.rendered()] == [False] * 5
    assert declaration.is_complete and declaration.summary == "no conflicts"
    # Still distinguishable from a declaration nobody answered.
    blank = COIDeclaration.objects.create(person=make_person(), disclosure_text_version=V)
    assert blank.is_complete is False and blank.responses.count() == 0
    # Saying "yes" with nothing ticked is refused.
    with pytest.raises(ValidationError) as err:
        declare(person, dict(NONE), version=V, role="author", has_relationships=True, attested=True)
    assert "has_relationships" in err.value.message_dict


def test_role_is_required_and_other_needs_a_description():
    person = make_person()
    with pytest.raises(ValidationError) as err:
        declare(person, {}, version=V, has_relationships=False, attested=True)
    assert "activity_role" in err.value.message_dict
    with pytest.raises(ValidationError) as err:
        declare(person, {}, version=V, role="other", has_relationships=False, attested=True)
    assert "activity_role_other" in err.value.message_dict
    spc = declare(person, {}, version=V, role="spc_member", has_relationships=False, attested=True)
    assert spc.role_label == "Scientific planning committee member"
    assert spc.session_presenters.count() == 0  # a declaration need not be tied to a session


def test_speaker_only_questions_apply_to_speakers_only():
    # A moderator is not asked, and whatever is passed is not stored.
    moderator = declare(make_person(), {}, version=V, role="moderator", has_relationships=False, attested=True, off_label=True)
    assert moderator.off_label is None and moderator.generic_names_acknowledged is None
    # A speaker must answer both.
    with pytest.raises(ValidationError) as err:
        declare(make_person(), {}, version=V, role="speaker", has_relationships=False, attested=True)
    assert {"off_label", "generic_names"} <= set(err.value.message_dict)
    speaker = declare(make_person(), {}, version=V, role="speaker", has_relationships=False, attested=True, off_label=True, generic_names=True)
    assert (speaker.off_label, speaker.generic_names_acknowledged, speaker.is_complete) == (True, True, True)


def test_a_no_on_generic_names_is_flagged_for_review():
    speaker = declare(make_person(), {}, version=V, role="speaker", has_relationships=False, attested=True, off_label=False, generic_names=False)
    assert speaker.needs_review
    assert speaker.is_complete  # accepted, not silently: flagged
    entry = AuditLog.objects.get(action="coi.declared", object_id=str(speaker.pk))
    assert entry.metadata["generic_names_declined"] is True
    fine = declare(make_person(), {}, version=V, role="speaker", has_relationships=False, attested=True, off_label=False, generic_names=True)
    assert not fine.needs_review
    assert list(COIDeclaration.objects.filter(generic_names_acknowledged=False)) == [speaker]


def test_the_attestation_is_required_and_snapshots_the_name():
    person = make_person(given="Ada", family="Lovelace")
    with pytest.raises(ValidationError) as err:
        declare(person, {}, version=V, role="author", has_relationships=False, attested=False)
    assert "attested" in err.value.message_dict
    declaration = declare(person, {}, version=V, role="author", has_relationships=False, attested=True)
    assert (declaration.attested, declaration.attested_name) == (True, "Ada Lovelace")
    person.given_name = "Augusta"
    person.save()
    declaration.refresh_from_db()
    assert declaration.attested_name == "Ada Lovelace"  # as it was when they agreed


# --- The slide ----------------------------------------------------------------------------------


def test_slide_text_for_no_relationships():
    person = make_person(given="Ada", family="Lovelace")
    declaration = declare(person, {}, version=V, role="speaker", has_relationships=False, attested=True, off_label=False, generic_names=True)
    assert slide_text(declaration).splitlines() == [
        "Disclosure: Ada Lovelace",
        "I have no relationships with for-profit or not-for-profit organizations to disclose.",
        "This presentation makes no off-label therapeutic recommendations.",
    ]


def test_slide_text_for_several_categories_and_off_label():
    person = make_person(given="Ada", family="Lovelace")
    declaration = speaker_declaration(
        person,
        {
            "direct_payments": ("Acme Devices", "Honoraria for two talks, 2025"),
            "patents": ("Acme Devices", "Co-inventor on a monitoring patent"),
        },
        off_label=True,
    )
    text = slide_text(declaration)
    lines = text.splitlines()
    assert lines[0] == "Disclosure: Ada Lovelace"
    assert lines[1].startswith("Relationships over the previous 2 years")
    assert "- Any direct financial payments including receipt of honoraria: Acme Devices - Honoraria for two talks, 2025" in lines
    assert "- Patents on a drug, product or device: Acme Devices - Co-inventor on a monitoring patent" in lines
    assert "Membership on advisory boards" not in text  # only the yeses
    assert lines[-1].startswith("This presentation includes therapeutic recommendations for off-label use")


# --- Per-activity confirmation and the publishing block ----------------------------------------


def presenter_on(session, person):
    return SessionPresenter.objects.create(session=session, person=person)


def upcoming_event():
    """A declaration made today is only picked up for events on or after today."""
    return make_event(start=timezone.now() + datetime.timedelta(days=10), sessions=0)


def test_a_reused_declaration_is_confirmed_per_session_and_logged():
    person = make_person()
    declaration = speaker_declaration(person)
    event = upcoming_event()
    s1, s2 = make_session(event, minutes=30), make_session(event, minutes=30)
    p1, p2 = presenter_on(s1, person), presenter_on(s2, person)
    assert p1.coi_declaration == p2.coi_declaration == declaration  # reused
    assert p1.coi_confirmed_at is None and p2.coi_confirmed_at is None  # never silently

    confirm_for_session(p1, person=person)
    p1.refresh_from_db(); p2.refresh_from_db()
    assert p1.coi_confirmed_at is not None and p2.coi_confirmed_at is None
    entry = AuditLog.objects.get(action="coi.confirmed_for_session")
    assert entry.actor_person == person and entry.metadata["session"] == str(s1.pk)
    with pytest.raises(ValidationError):
        confirm_for_session(p2, person=make_person())  # only the presenter themselves


def test_publishing_is_blocked_while_a_presenter_has_no_declaration():
    event = upcoming_event()
    session = make_session(event, minutes=60)
    undeclared = make_person(given="Grace", family="Hopper")
    declared = make_person(given="Ada", family="Lovelace")
    speaker_declaration(declared)
    presenter_on(session, declared)
    presenter_on(session, undeclared)

    assert [sp.person for sp in event.undeclared_presenters()] == [undeclared]
    assert "Hopper, Grace" in event.publication_blockers()[0]
    event.status = RoundsEvent.Status.PUBLISHED
    with pytest.raises(ValidationError) as err:
        event.full_clean()
    assert "Cannot publish" in str(err.value) and "Hopper" in str(err.value)
    event.refresh_from_db()
    assert event.status == RoundsEvent.Status.DRAFT

    speaker_declaration(undeclared)
    sp = SessionPresenter.objects.get(session=session, person=undeclared)
    sp.attach_current_declaration()
    sp.save()
    event.status = RoundsEvent.Status.PUBLISHED
    event.full_clean()  # now allowed
    event.save()
    assert event.publication_blockers() == []


def test_the_event_admin_page_says_why_it_cannot_be_published(client):
    from django.contrib.auth import get_user_model

    event = make_event(sessions=0)
    session = make_session(event, minutes=60)
    presenter_on(session, make_person(given="Grace", family="Hopper"))
    boss = get_user_model().objects.create_superuser(username="boss", password="x" * 20)
    client.force_login(boss)
    page = client.get(reverse("admin:rounds_roundsevent_change", args=[event.pk])).content.decode()
    assert "Cannot be published until:" in page and "Hopper, Grace" in page


# --- The presenter's own page ------------------------------------------------------------------


@pytest.fixture
def ada():
    AllowedDomain.objects.create(domain="mcgill.ca")
    return make_person(given="Ada", family="Lovelace", email="ada@mcgill.ca")


def signed_in_client(email):
    from signin.tests.test_signin import ask_for_link, last_link_token

    client = Client()
    ask_for_link(client, email)
    client.get(reverse("signin:redeem", args=[last_link_token()]))
    return client


def test_the_presenter_page_declares_confirms_and_shows_the_slide(ada):
    program = make_program("Disclosing")
    event = make_event(program=program, start=timezone.now() + datetime.timedelta(days=10), sessions=0)
    session = make_session(event, minutes=60)
    presentation = presenter_on(session, ada)
    assert presentation.coi_declaration is None
    client = signed_in_client("ada@mcgill.ca")
    url = reverse("rounds:disclosure")

    page = client.get(url).content.decode()
    assert "You have no declaration in force" in page
    assert "over the previous 2 years" in page
    assert "I do not have a relationship with a for-profit and/or a not-for-profit organization to disclose" in page
    assert "Name of for-profit or not-for-profit organization(s)" in page

    # A speaker with one relationship, the client's hiding bypassed: the server still demands both columns.
    refused = client.post(url, {
        "action": "declare", "activity_role": "speaker", "has_relationships": "yes",
        "q_advisory_boards": "1", "q_advisory_boards_description": "Advisory board",
        "off_label": "yes", "generic_names": "yes", "attested": "1",
    })
    assert refused.status_code == 400 and "Name the organization(s)." in refused.content.decode()

    done = client.post(url, {
        "action": "declare", "activity_role": "speaker", "has_relationships": "yes",
        "q_advisory_boards": "1", "q_advisory_boards_organizations": "Acme Devices",
        "q_advisory_boards_description": "Advisory board member",
        "off_label": "yes", "generic_names": "yes", "attested": "1",
    })
    assert done.status_code == 302
    declaration = COIDeclaration.objects.get(person=ada)
    presentation.refresh_from_db()
    assert presentation.coi_declaration == declaration  # attached to the upcoming session
    page = client.get(url).content.decode()
    assert "Disclosure: Ada Lovelace" in page and "Acme Devices - Advisory board member" in page
    assert "Copy slide text" in page and "off-label" in page
    assert "Still accurate for this session" in page

    confirmed = client.post(url, {"action": "confirm", "presentation": presentation.pk})
    assert confirmed.status_code == 302
    presentation.refresh_from_db()
    assert presentation.coi_confirmed_at is not None
    assert "Confirmed" in client.get(url).content.decode()
