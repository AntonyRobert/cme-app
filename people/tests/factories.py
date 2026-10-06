"""Small helpers for building test data. Not a factory library on purpose."""
import itertools

from django.contrib.auth import get_user_model

from people.models import Person, PersonEmail

_counter = itertools.count(1)


def make_person(given="Ada", family=None, role=Person.Role.PHYSICIAN, email=None, **extra):
    n = next(_counter)
    person = Person.objects.create(
        given_name=given, family_name=family or f"Person{n}", role=role, **extra
    )
    if email:
        PersonEmail.objects.create(person=person, email=email, is_primary=True)
    return person


def make_staff(username=None):
    n = next(_counter)
    return get_user_model().objects.create_user(
        username=username or f"staff{n}", is_staff=True
    )
