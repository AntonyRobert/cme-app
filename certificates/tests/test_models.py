import datetime
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from certificates.models import Certificate, CertificateLine
from certificates.rules import (
    VERIFICATION_ALPHABET,
    certificate_type_for,
    generate_verification_code,
)
from core.models import ImmutableRowError
from people.models import Person
from people.tests.factories import make_person, make_staff
from rounds.tests.factories import make_event

pytestmark = pytest.mark.django_db


def issue(person=None, **extra):
    person = person or make_person(given="Ada", family="Lovelace")
    return Certificate.objects.create(
        person=person,
        certificate_type=certificate_type_for(person),
        period_start=datetime.date(2026, 1, 1),
        period_end=datetime.date(2026, 12, 31),
        total_credits=extra.pop("total_credits", Decimal("1.00")),
        recipient_name=person.full_name,
        template_version="2026-1",
        issued_by=extra.pop("issued_by", None) or make_staff(),
        **extra,
    )


def line(certificate, event=None, **extra):
    event = event or make_event()
    values = dict(
        event_title=event.title,
        event_date=event.date,
        session_titles=["One", "Two", "Three"],
        attended_minutes=60,
        minutes_source="teams",
        computed_credits=Decimal("1.00"),
        credits=Decimal("1.00"),
    )
    values.update(extra)
    return CertificateLine.objects.create(certificate=certificate, event=event, **values)


def test_verification_codes_use_the_unambiguous_alphabet():
    codes = {generate_verification_code() for _ in range(500)}
    assert len(codes) == 500
    for code in codes:
        groups = code.split("-")
        assert [len(group) for group in groups] == [4, 4, 4]
        assert set("".join(groups)) <= set(VERIFICATION_ALPHABET)
    assert not set("01ILOU") & set(VERIFICATION_ALPHABET)


@pytest.mark.parametrize(
    "role, expected",
    [
        (Person.Role.PHYSICIAN, "cme"),
        (Person.Role.NURSE, "attendance"),
        (Person.Role.PHARMACIST, "attendance"),
        (Person.Role.TRAINEE, "attendance"),
        (Person.Role.STUDENT, "attendance"),
        (Person.Role.OTHER, "attendance"),
    ],
)
def test_only_physicians_get_a_cme_certificate(role, expected):
    assert certificate_type_for(Person(role=role)) == expected


def test_each_certificate_gets_its_own_code():
    assert issue().verification_code != issue().verification_code


@pytest.mark.parametrize(
    "field, value",
    [
        ("total_credits", Decimal("99.00")),
        ("recipient_name", "Someone Else"),
        ("licence_number", "99999"),
        ("verification_code", "AAAA-AAAA-AAAA"),
        ("certificate_type", "attendance"),
        ("pdf_sha256", "f" * 64),
    ],
)
def test_what_a_certificate_prints_cannot_be_changed(field, value):
    certificate = issue()
    setattr(certificate, field, value)
    with pytest.raises(ImmutableRowError):
        certificate.save()


def test_a_certificate_does_not_follow_later_changes_to_the_person():
    person = make_person(given="Ada", family="Lovelace", licence_number="01234",
                         licence_jurisdiction="CMQ")
    certificate = issue(person, licence_number=person.licence_number,
                        licence_jurisdiction=person.licence_jurisdiction)
    person.family_name = "King"
    person.licence_number = "55555"
    person.save()
    certificate.refresh_from_db()
    assert certificate.recipient_name == "Ada Lovelace"
    assert certificate.licence_number == "01234"  # as entered, leading zero kept


def test_a_certificate_can_be_revoked_but_only_with_a_reason():
    certificate = issue()
    certificate.revoked_at = timezone.now()
    with pytest.raises(IntegrityError), transaction.atomic():
        certificate.save()
    certificate.revoked_reason = "Issued to the wrong person"
    certificate.save()
    certificate.refresh_from_db()
    assert certificate.is_revoked


def test_certificates_and_lines_cannot_be_deleted():
    certificate = issue()
    row = line(certificate)
    with pytest.raises(ImmutableRowError):
        certificate.delete()
    with pytest.raises(ImmutableRowError):
        row.delete()


def test_a_reissue_points_back_and_the_old_certificate_is_untouched():
    old = issue()
    before = Certificate.objects.filter(pk=old.pk).values().get()
    new = issue(old.person, supersedes=old, total_credits=Decimal("1.25"))
    assert Certificate.objects.filter(pk=old.pk).values().get() == before
    old.refresh_from_db()
    assert old.superseded_by == new
    assert old.is_superseded and not new.is_superseded


def test_a_certificate_can_only_be_superseded_once():
    old = issue()
    issue(old.person, supersedes=old)
    with pytest.raises(IntegrityError), transaction.atomic():
        issue(old.person, supersedes=old)


def test_lines_are_one_per_event_and_frozen():
    certificate = issue()
    event = make_event()
    row = line(certificate, event)
    with pytest.raises(IntegrityError), transaction.atomic():
        line(certificate, event)
    row.credits = Decimal("5.00")
    with pytest.raises(ImmutableRowError):
        row.save()


def test_a_line_keeps_its_snapshot_when_the_event_is_renamed():
    certificate = issue()
    event = make_event()
    row = line(certificate, event)
    event.title = "Renamed Rounds"
    event.save()
    row.refresh_from_db()
    assert row.event_title != "Renamed Rounds"
    assert row.session_titles == ["One", "Two", "Three"]


def test_certificates_and_lines_are_reached_through_their_owner():
    mine = issue()
    line(mine)
    line(issue())
    assert list(Certificate.objects.for_person(mine.person)) == [mine]
    assert CertificateLine.objects.for_person(mine.person).count() == 1
