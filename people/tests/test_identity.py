import pytest

from people.identity import MAX_MERGE_DEPTH, MergeChainError, identity_ids, resolve_root
from people.models import Person, PersonEmail

from .factories import make_person

pytestmark = pytest.mark.django_db


def point(duplicate, survivor):
    Person.objects.filter(pk=duplicate.pk).update(merged_into=survivor)
    duplicate.refresh_from_db()


def test_unmerged_person_is_their_own_root():
    person = make_person()
    assert resolve_root(person) == person


def test_root_is_found_through_a_chain():
    a, b, c = make_person(), make_person(), make_person()
    point(a, b)
    point(b, c)
    assert resolve_root(a) == c
    assert resolve_root(b) == c
    assert resolve_root(c) == c


def test_a_loop_raises_instead_of_spinning():
    a, b = make_person(), make_person()
    point(a, b)
    point(b, a)
    with pytest.raises(MergeChainError):
        resolve_root(a)


def test_chain_at_the_depth_cap_resolves_and_one_deeper_raises():
    people = [make_person() for _ in range(MAX_MERGE_DEPTH + 2)]
    for duplicate, survivor in zip(people, people[1:]):
        point(duplicate, survivor)
    root = people[-1]
    assert resolve_root(people[1]) == root  # exactly MAX_MERGE_DEPTH hops
    with pytest.raises(MergeChainError):
        resolve_root(people[0])  # one more


def test_identity_ids_covers_the_root_and_everything_merged_into_it():
    root, dup1, dup2, dup_of_dup, stranger = (make_person() for _ in range(5))
    point(dup1, root)
    point(dup2, root)
    point(dup_of_dup, dup1)
    expected = {root.pk, dup1.pk, dup2.pk, dup_of_dup.pk}
    assert identity_ids(root) == expected
    assert identity_ids(dup_of_dup) == expected
    assert stranger.pk not in identity_ids(root)


def test_for_person_sees_rows_left_on_a_tombstone():
    root = make_person(email="root@mcgill.ca")
    duplicate = make_person(email="dup@muhc.mcgill.ca")
    make_person(email="stranger@mcgill.ca")
    point(duplicate, root)
    emails = set(PersonEmail.objects.for_person(root).values_list("email", flat=True))
    assert emails == {"root@mcgill.ca", "dup@muhc.mcgill.ca"}
    assert set(PersonEmail.objects.for_person(duplicate).values_list("email", flat=True)) == emails
