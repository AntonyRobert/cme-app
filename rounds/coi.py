"""
Writing a conflict-of-interest declaration, confirming it for an activity,
and turning it into the disclosure slide.

A declaration is a set of answers to the questionnaire of one version. It
is complete only when every question has an answer, and "no relationships"
is written as an explicit no to each category, never left blank. An
unanswered declaration and an attested-no declaration must be
distinguishable, and they are: the first has no responses.

Versions laid out as the National Standard form (settings.COI_STANDARD) add
a role in the activity, a top-level yes/no, two text columns per category,
two speaker-only questions and the attestation. The wording of all of that
is the form's, not ours.
"""
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from audit.log import record

from .models import COIDeclaration, COIResponse, coi_is_national_standard, coi_questions, coi_standard

Role = COIDeclaration.Role


def _answer(value):
    """Normalise one category's answer to (has_conflict, organizations, description)."""
    if isinstance(value, (tuple, list)):
        if len(value) == 2:  # legacy shape: (has_conflict, details)
            yes, details = value
            return bool(yes), "", (details or "").strip()
        yes, organizations, description = value
        return bool(yes), (organizations or "").strip(), (description or "").strip()
    return bool(value), "", ""


def validate_declaration(
    answers,
    *,
    version,
    role=None,
    role_other="",
    has_relationships=None,
    off_label=None,
    generic_names=None,
    attested=False,
):
    """
    The rules, as {field: message}; empty means the declaration can be
    written. Keys: activity_role, activity_role_other, has_relationships,
    off_label, generic_names, attested, <category>, <category>_organizations,
    <category>_description. Returns (errors, normalised answers, has_relationships).
    """
    questions = coi_questions(version)
    expected = [key for key, _ in questions]
    standard = coi_is_national_standard(version)
    errors = {}

    if standard:
        if role not in Role.values:
            errors["activity_role"] = "Say what your role in the activity is."
        elif role == Role.OTHER and not (role_other or "").strip():
            errors["activity_role_other"] = "Say what the other role is."
        if has_relationships is None:
            errors["has_relationships"] = "Choose one: a relationship to disclose, or none."
        if role == Role.SPEAKER:
            if off_label is None:
                errors["off_label"] = "Answer yes or no."
            if generic_names is None:
                errors["generic_names"] = "Answer yes or no."
        if not attested:
            errors["attested"] = "Tick 'I agree' to make the declaration."
        if has_relationships is False:
            answers = {key: (False, "", "") for key in expected}
    else:
        has_relationships = None  # not a question on the legacy form

    given = {key: _answer(value) for key, value in answers.items()}
    for key in expected:
        if key not in given:
            errors[key] = "Unanswered. Every question needs a yes or a no."
    for key in sorted(set(given) - set(expected)):
        errors[key] = f"Not a question of version {version}."
    for key in expected:
        if key not in given:
            continue
        yes, organizations, description = given[key]
        if yes and not description:
            errors[f"{key}_description"] = "Describe the relationship(s)."
        if yes and standard and not organizations:
            errors[f"{key}_organizations"] = "Name the organization(s)."
    if standard and has_relationships is True and not any(yes for yes, _, _ in given.values()):
        errors["has_relationships"] = (
            "You said you have a relationship to disclose, but no category is ticked."
        )
    return errors, given, has_relationships


@transaction.atomic
def declare(
    person,
    answers,
    *,
    version,
    role=None,
    role_other="",
    has_relationships=None,
    off_label=None,
    generic_names=None,
    attested=False,
    attested_name=None,
    declared_at=None,
    user=None,
    request=None,
):
    """
    Record a complete declaration under questionnaire `version` (normally the
    program's `coi_question_version`).

    `answers` maps question keys to (has_conflict, organizations, description)
    (or, on the legacy version, (has_conflict, details)). On a National
    Standard version, `has_relationships=False` writes an explicit no to every
    category and `answers` may be empty; `has_relationships=True` needs every
    category answered, and each yes needs both columns. A role is required;
    a speaker also answers `off_label` and `generic_names`; `attested` must be
    true and the declarant's name is snapshotted. Anything missing is refused
    as a ValidationError and nothing is written.
    """
    errors, given, has_relationships = validate_declaration(
        answers,
        version=version,
        role=role,
        role_other=role_other,
        has_relationships=has_relationships,
        off_label=off_label,
        generic_names=generic_names,
        attested=attested,
    )
    if errors:
        raise ValidationError(errors)
    expected = [key for key, _ in coi_questions(version)]
    standard = coi_is_national_standard(version)

    declaration = COIDeclaration.objects.create(
        person=person,
        declared_at=declared_at or timezone.now(),
        disclosure_text_version=version,
        activity_role=role or "",
        activity_role_other=(role_other or "").strip() if role == Role.OTHER else "",
        has_relationships=has_relationships,
        off_label=off_label if (standard and role == Role.SPEAKER) else None,
        generic_names_acknowledged=generic_names if (standard and role == Role.SPEAKER) else None,
        attested=bool(attested) if standard else False,
        attested_name=(attested_name or person.full_name) if standard else "",
    )
    for key in expected:
        yes, organizations, description = given[key]
        COIResponse.objects.create(
            declaration=declaration,
            question_key=key,
            has_conflict=yes,
            organizations=organizations if yes else "",
            relationship_description=description if yes else "",
        )
    actor = {"user": user} if user is not None else {"person": person}
    record(
        "coi.declared",
        declaration,
        request=request,
        metadata={
            "person": str(person.pk),
            "version": version,
            "role": role or "",
            "has_relationships": has_relationships,
            "conflicts": sorted(key for key, (yes, _, _) in given.items() if yes),
            "off_label": off_label if role == Role.SPEAKER else None,
            "generic_names_declined": declaration.needs_review,
            "entered_by_staff": user is not None,
        },
        **actor,
    )
    return declaration


def declare_no_conflicts(person, *, version, **kwargs):
    """An explicit no to every question of the version (the form's first radio)."""
    if coi_is_national_standard(version):
        kwargs.setdefault("has_relationships", False)
        kwargs.setdefault("attested", True)
        return declare(person, {}, version=version, **kwargs)
    answers = {key: (False, "") for key, _ in coi_questions(version)}
    return declare(person, answers, version=version, **kwargs)


# --- Per-activity confirmation --------------------------------------------------------


@transaction.atomic
def confirm_for_session(session_presenter, *, person, request=None, at=None):
    """
    The presenter confirms that the declaration attached to this session is
    still accurate for this activity. The approved form is per activity; we
    keep a standing declaration and record each reuse here, never silently.
    """
    if session_presenter.person_id != person.pk:
        raise ValidationError("Only the presenter can confirm their own declaration.")
    if session_presenter.coi_declaration_id is None:
        raise ValidationError("There is no declaration attached to confirm; make one first.")
    session_presenter.coi_confirmed_at = at or timezone.now()
    session_presenter.save(update_fields=["coi_confirmed_at"])
    record(
        "coi.confirmed_for_session",
        session_presenter.coi_declaration,
        person=person,
        request=request,
        metadata={
            "session": str(session_presenter.session_id),
            "event": str(session_presenter.session.event_id),
            "confirmed_at": session_presenter.coi_confirmed_at.isoformat(),
        },
    )
    return session_presenter


# --- The disclosure slide ------------------------------------------------------------------


def slide_text(declaration):
    """
    What the speaker puts on the disclosure slide and says at the start:
    either no relationships, or each category with its organizations and
    description, plus an off-label line when they intend to recommend it.
    Plain text, ready to paste.
    """
    lines = [f"Disclosure: {declaration.attested_name or declaration.person.full_name}"]
    declared = [row for row in declaration.rendered() if row[1]]
    if declaration.has_relationships is False or not declared:
        lines.append("I have no relationships with for-profit or not-for-profit organizations to disclose.")
    else:
        lines.append(
            f"Relationships over the previous {coi_standard()['lookback_years']} years "
            "with for-profit and/or not-for-profit organizations:"
        )
        for text, _, organizations, description in declared:
            detail = " - ".join(part for part in (organizations, description) if part)
            lines.append(f"- {text}: {detail}")
    if declaration.off_label is True:
        lines.append(
            "This presentation includes therapeutic recommendations for off-label use of medication, "
            "which I will identify as such."
        )
    elif declaration.off_label is False:
        lines.append("This presentation makes no off-label therapeutic recommendations.")
    return "\n".join(lines)
