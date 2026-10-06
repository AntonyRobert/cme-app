import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from people.models import AllowedDomain, Person, PersonEmail

from .factories import make_person

pytestmark = pytest.mark.django_db


# --- Licence numbers ---------------------------------------------------------


def test_licence_is_stored_as_entered_and_normalized_separately():
    person = make_person(licence_number="01234", licence_jurisdiction="CMQ")
    person.refresh_from_db()
    assert person.licence_number == "01234"  # what prints
    assert person.licence_number_normalized == "1234"  # what matches


def test_normalized_licence_follows_an_edit():
    person = make_person(licence_number="01234", licence_jurisdiction="CMQ")
    person.licence_number = "98 765"
    person.save()
    person.refresh_from_db()
    assert person.licence_number_normalized == "98765"
    person.licence_number = ""
    person.licence_jurisdiction = ""
    person.save()
    person.refresh_from_db()
    assert person.licence_number is None
    assert person.licence_number_normalized is None
    assert person.licence_jurisdiction is None


def test_same_licence_same_jurisdiction_collides_after_normalization():
    make_person(licence_number="01234", licence_jurisdiction="CMQ")
    with pytest.raises(IntegrityError), transaction.atomic():
        make_person(licence_number=" 1234", licence_jurisdiction="CMQ")


def test_same_number_in_another_jurisdiction_is_fine():
    make_person(licence_number="1234", licence_jurisdiction="CMQ")
    make_person(licence_number="1234", licence_jurisdiction="CPSO")


def test_people_without_a_licence_do_not_collide():
    make_person(role=Person.Role.STUDENT)
    make_person(role=Person.Role.STUDENT)


def test_clean_reports_the_collision_before_the_database_does():
    make_person(licence_number="01234", licence_jurisdiction="CMQ")
    twin = Person(
        given_name="A",
        family_name="B",
        role="physician",
        licence_number="1234",
        licence_jurisdiction="CMQ",
    )
    with pytest.raises(ValidationError) as err:
        twin.full_clean()
    assert "licence_number" in err.value.message_dict


def test_licence_number_needs_a_jurisdiction():
    orphan = Person(given_name="A", family_name="B", role="physician", licence_number="1234")
    with pytest.raises(ValidationError) as err:
        orphan.full_clean()
    assert "licence_jurisdiction" in err.value.message_dict
    with pytest.raises(IntegrityError), transaction.atomic():
        orphan.save()


def test_a_tombstone_does_not_block_the_survivor_from_holding_the_number():
    survivor = make_person()
    duplicate = make_person(licence_number="1234", licence_jurisdiction="CMQ")
    duplicate.merged_into = survivor
    duplicate.save()
    survivor.licence_number = "1234"
    survivor.licence_jurisdiction = "CMQ"
    survivor.full_clean()
    survivor.save()


def test_a_person_cannot_be_merged_into_themselves():
    person = make_person()
    person.merged_into = person
    with pytest.raises(IntegrityError), transaction.atomic():
        person.save()


def test_names_are_not_recased():
    person = make_person(given="jean-françois", family="de la FONTAINE")
    person.refresh_from_db()
    assert (person.given_name, person.family_name) == ("jean-françois", "de la FONTAINE")


# --- Emails ------------------------------------------------------------------


def test_email_is_lowercased_on_save():
    person = make_person()
    row = PersonEmail.objects.create(person=person, email="  Ada.L@McGill.CA ")
    row.refresh_from_db()
    assert row.email == "ada.l@mcgill.ca"


def test_email_is_unique_regardless_of_case():
    PersonEmail.objects.create(person=make_person(), email="ada@mcgill.ca")
    with pytest.raises(IntegrityError), transaction.atomic():
        PersonEmail.objects.create(person=make_person(), email="ADA@mcgill.ca")


def test_database_rejects_a_mixed_case_email_that_bypasses_save():
    row = PersonEmail.objects.create(person=make_person(), email="ada@mcgill.ca")
    with pytest.raises(IntegrityError), transaction.atomic():
        PersonEmail.objects.filter(pk=row.pk).update(email="Ada@McGill.ca")


def test_only_one_primary_email_per_person():
    person = make_person(email="one@mcgill.ca")
    with pytest.raises(IntegrityError), transaction.atomic():
        PersonEmail.objects.create(person=person, email="two@mcgill.ca", is_primary=True)
    PersonEmail.objects.create(person=person, email="two@mcgill.ca", is_primary=False)


def test_allowed_domain_is_lowercased():
    domain = AllowedDomain.objects.create(domain="@MUHC.McGill.ca")
    domain.refresh_from_db()
    assert domain.domain == "muhc.mcgill.ca"
