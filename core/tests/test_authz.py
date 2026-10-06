import uuid

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.http import Http404
from django.test import RequestFactory

from core.authz import get_owned_or_404, public_object, unmarked_routes
from people.models import Person, PersonEmail
from people.tests.factories import make_person

from . import urls_fixture


def test_every_real_route_that_takes_an_id_declares_its_authorization():
    """
    The guard for non-negotiable 1. If this fails, a route was added that
    takes a URL parameter without @owned_object or @public_object.
    """
    assert unmarked_routes() == []


def test_walker_accepts_marked_routes():
    assert unmarked_routes(urls_fixture.Good) == []


def test_walker_reports_unmarked_routes_including_nested_ones():
    assert unmarked_routes(urls_fixture.Bad) == [
        "emails/<uuid:id>/",
        "people/<uuid:person_id>/detail/",
        "people/<uuid:person_id>/<uuid:id>/edit/",
    ]


def test_public_object_requires_a_reason():
    with pytest.raises(ImproperlyConfigured):
        public_object("")


@pytest.mark.django_db
class TestOwnedObject:
    def request_as(self, person):
        request = RequestFactory().get("/")
        if person is not None:
            request.person = person
        return request

    def test_owner_gets_the_object(self):
        owner = make_person(email="owner@mcgill.ca")
        row = owner.emails.get()
        response = urls_fixture.email_detail(self.request_as(owner), id=row.pk)
        assert response.content == b"owner@mcgill.ca"

    def test_someone_else_gets_404(self):
        owner = make_person(email="owner@mcgill.ca")
        other = make_person(email="other@mcgill.ca")
        with pytest.raises(Http404):
            urls_fixture.email_detail(self.request_as(other), id=owner.emails.get().pk)

    def test_unknown_id_gets_the_same_404(self):
        with pytest.raises(Http404):
            urls_fixture.email_detail(self.request_as(make_person()), id=uuid.uuid4())

    def test_nobody_signed_in_gets_404(self):
        owner = make_person(email="owner@mcgill.ca")
        with pytest.raises(Http404):
            urls_fixture.email_detail(self.request_as(None), id=owner.emails.get().pk)

    def test_a_model_without_for_person_cannot_be_served_by_id(self):
        person = make_person()
        with pytest.raises(ImproperlyConfigured):
            get_owned_or_404(Person, person.pk, person)

    def test_owner_reaches_rows_through_a_merged_record(self):
        survivor = make_person(email="survivor@mcgill.ca")
        duplicate = make_person(email="dup@mcgill.ca")
        Person.objects.filter(pk=duplicate.pk).update(merged_into=survivor)
        row = PersonEmail.objects.get(email="dup@mcgill.ca")
        assert get_owned_or_404(PersonEmail, row.pk, survivor) == row
