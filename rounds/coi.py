"""
Writing a conflict-of-interest declaration.

A declaration is a set of answers to the questionnaire of one version. It
is complete only when every question has an answer, and "no conflicts in
any category" is written as an explicit no to each question, never left
blank. An unanswered declaration and an attested-no declaration must be
distinguishable, and they are: the first has no responses.
"""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from audit.log import record

from .models import COIDeclaration, COIResponse, coi_questions


@transaction.atomic
def declare(person, answers, *, version=None, declared_at=None, user=None, request=None):
    """
    Record a complete declaration.

    `answers` maps every question_key of `version` to (has_conflict, details).
    Missing or unknown keys, and a yes without details, are refused as a
    ValidationError; nothing is written. The audit entry names the staff
    user if one entered it on the person's behalf, else the person.
    """
    version = version or settings.COI_CURRENT_VERSION
    questions = coi_questions(version)
    expected = {key for key, _ in questions}
    given = set(answers)
    errors = {}
    for key in sorted(expected - given):
        errors[key] = "Unanswered. Every question needs a yes or a no."
    for key in sorted(given - expected):
        errors[key] = f"Not a question of version {version}."
    for key in expected & given:
        has_conflict, details = answers[key]
        if has_conflict and not (details or "").strip():
            errors[key] = "A yes needs an explanation."
    if errors:
        raise ValidationError(errors)

    declaration = COIDeclaration.objects.create(
        person=person,
        declared_at=declared_at or timezone.now(),
        disclosure_text_version=version,
    )
    for key, _ in questions:
        has_conflict, details = answers[key]
        COIResponse.objects.create(
            declaration=declaration,
            question_key=key,
            has_conflict=bool(has_conflict),
            details=(details or "").strip() if has_conflict else "",
        )
    actor = {"user": user} if user is not None else {"person": person}
    record(
        "coi.declared",
        declaration,
        request=request,
        metadata={
            "person": str(person.pk),
            "version": version,
            "conflicts": sorted(key for key, (yes, _) in answers.items() if yes),
            "entered_by_staff": user is not None,
        },
        **actor,
    )
    return declaration


def declare_no_conflicts(person, *, version=None, **kwargs):
    """An explicit no to every question of the version."""
    version = version or settings.COI_CURRENT_VERSION
    answers = {key: (False, "") for key, _ in coi_questions(version)}
    return declare(person, answers, version=version, **kwargs)
