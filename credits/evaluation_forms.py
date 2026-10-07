"""
Evaluation form templates: which form a session uses, what it asks once
per-objective questions expand, what counts as complete, and how a form
changes without touching what was already answered.

The rules that might change are here, one function each.
"""
from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction

from audit.log import record

from .models import EvaluationForm, EvaluationFormVersion, EvaluationQuestion, EvaluationResponse

Kind = EvaluationQuestion.Kind

LEVEL_SESSION = "session"
LEVEL_EVENT = "event"
LEVEL_PROGRAM = "program"

LIKERT_CHOICES = [(1, "1"), (2, "2"), (3, "3"), (4, "4"), (5, "5")]
YES_NO_CHOICES = [(1, "Yes"), (0, "No")]

# Provisional, pending McGill CPD: accrediting bodies say what an evaluation
# must ask. Seeded as every program's default on migration and in tests.
STANDARD_FORM_NAME = "Standard CME evaluation"
STANDARD_QUESTIONS = [
    {
        "question_key": "objectives_met",
        "kind": Kind.PER_OBJECTIVE,
        "required": True,
        "prompt": "This session met the stated objective: {objective}",
    },
    {
        "question_key": "relevance",
        "kind": Kind.LIKERT_5,
        "required": True,
        "prompt": "The content was relevant to my practice",
    },
    {
        "question_key": "commercial_bias",
        "kind": Kind.YES_NO,
        "required": True,
        "prompt": "Was the content free of commercial bias?",
    },
    {
        "question_key": "bias_detail",
        "kind": Kind.FREE_TEXT,
        "required": False,
        "required_when": {"question_key": "commercial_bias", "value": 0},
        "prompt": "If you answered no, please explain",
    },
    {
        "question_key": "practice_change",
        "kind": Kind.FREE_TEXT,
        "required": False,
        "prompt": "What will you do differently in your practice as a result of this session?",
    },
    {
        "question_key": "comments",
        "kind": Kind.FREE_TEXT,
        "required": False,
        "prompt": "Any other comments for the presenter?",
    },
]


# --- Which form a session uses ---------------------------------------------------------


@dataclass(frozen=True)
class Resolution:
    form: object  # EvaluationForm or None
    level: str | None  # where it came from
    skipped: tuple = ()  # (level, form) pairs passed over because the form is not active

    @property
    def version(self):
        return self.form.current_version if self.form else None


def resolve_form(session):
    """
    The form this session uses: the session's own, else its event's, else
    the program's default. Most specific wins. A form that is not active
    (draft or retired) is skipped, and noted, so a retired form pinned on
    an old session does not silently swallow the program default.
    """
    event = session.event
    candidates = [
        (LEVEL_SESSION, session.evaluation_form),
        (LEVEL_EVENT, event.evaluation_form),
        (LEVEL_PROGRAM, event.program.default_evaluation_form),
    ]
    skipped = []
    for level, form in candidates:
        if form is None:
            continue
        if form.is_active and form.current_version is not None:
            return Resolution(form=form, level=level, skipped=tuple(skipped))
        skipped.append((level, form))
    return Resolution(form=None, level=None, skipped=tuple(skipped))


# --- What a version asks, for a session -----------------------------------------------


@dataclass(frozen=True)
class RenderedQuestion:
    """One question as the attendee sees it; per_objective has expanded."""

    question: EvaluationQuestion
    objective: object = None  # LearningObjective for an expanded per_objective question

    @property
    def key(self):
        return self.question.question_key

    @property
    def field_name(self):
        return f"q_{self.key}" + (f"_{self.objective.pk}" if self.objective else "")

    @property
    def prompt(self):
        if self.objective is not None:
            return self.question.prompt.replace("{objective}", self.objective.text)
        return self.question.prompt

    @property
    def kind(self):
        # A per-objective question is a Likert question once expanded.
        return Kind.LIKERT_5 if self.question.kind == Kind.PER_OBJECTIVE else self.question.kind

    @property
    def required(self):
        """Unconditionally required (Likert and choice questions)."""
        return self.question.required

    @property
    def condition(self):
        return self.question.condition

    def required_given(self, answers):
        """
        Required right now: unconditionally, or because the trigger question
        has the triggering answer. `answers` maps field name to the current
        value (an int rating, a string, or a list for several choices).
        """
        if self.required:
            return True
        if self.condition is None:
            return False
        key, value = self.condition
        return answer_matches(answers.get(f"q_{key}"), value)

    @property
    def options(self):
        if self.kind == Kind.LIKERT_5:
            return LIKERT_CHOICES
        if self.kind == Kind.YES_NO:
            return YES_NO_CHOICES
        if self.kind in (Kind.SINGLE_CHOICE, Kind.MULTI_CHOICE):
            return [(c, c) for c in self.question.choices]
        return []


def rendered_questions(version, session):
    """The version's questions in order, per_objective expanded for this session."""
    objectives = list(session.objectives.order_by("position"))
    out = []
    for question in version.questions.order_by("position"):
        if question.kind == Kind.PER_OBJECTIVE:
            out.extend(RenderedQuestion(question=question, objective=o) for o in objectives)
        else:
            out.append(RenderedQuestion(question=question))
    return out


def answer_matches(answer, value):
    """Does an answer (rating, text, list of choices) equal or contain the trigger value?"""
    if answer is None or answer == "" or answer == []:
        return False
    if isinstance(answer, list):
        return str(value) in [str(a) for a in answer]
    return str(answer) == str(value)


def answers_of(submission):
    """{field name: value} from a submission's responses, the shape the views and rules use."""
    answers = {}
    for r in submission.responses.all():
        key = f"q_{r.question_key}" + (f"_{r.objective_id}" if r.objective_id else "")
        if r.rating is not None:
            answers[key] = r.rating
        elif r.selected:
            answers[key] = r.selected
        elif r.free_text:
            answers[key] = r.free_text
    return answers


def response_for(rendered, responses):
    """The stored response for a rendered question, from {(key, objective id): response}."""
    return responses.get((rendered.key, rendered.objective.pk if rendered.objective else None))


def responses_by_question(submission):
    return {(r.question_key, r.objective_id): r for r in submission.responses.all()}


def remaining(submission):
    """
    What still stands between this submission and complete: the rendered
    questions required right now (unconditionally, or whose condition holds
    given the answers so far) that have no answer, plus "minutes" and
    "attestation" when those are missing. Empty means complete.
    """
    responses = responses_by_question(submission)
    answers = answers_of(submission)
    missing = []
    for rendered in rendered_questions(submission.form_version, submission.session):
        if not rendered.required_given(answers):
            continue
        response = response_for(rendered, responses)
        if response is None or not response.answered:
            missing.append(rendered)
    if submission.self_reported_session_minutes is None:
        missing.append("minutes")
    if not submission.attestation:
        missing.append("attestation")
    return missing


def compute_is_complete(submission):
    """
    Complete when every question required right now has an answer, and the
    minutes and attestation are given. A conditionally required question
    whose condition does not hold is not required, and its absence blocks
    nothing. The responses are the truth; the flag is a cache.
    """
    return not remaining(submission)


def store_answer(submission, rendered, value):
    """
    Write one answer. `value` is an int for rating kinds, a string for free
    text or a single choice, a list for several choices; None or empty
    removes the answer. Returns the response or None.
    """
    lookup = {
        "submission": submission,
        "question_key": rendered.key,
        "objective": rendered.objective,
    }
    if isinstance(value, str):
        value = value.strip()
    empty = value is None or value == "" or value == []
    if empty:
        EvaluationResponse.objects.filter(**lookup).delete()
        submission.recompute()
        return None
    fields = {"rating": None, "free_text": None, "selected": None}
    if rendered.kind in (Kind.LIKERT_5, Kind.YES_NO):
        fields["rating"] = int(value)
    elif rendered.kind == Kind.FREE_TEXT:
        fields["free_text"] = str(value)
    elif rendered.kind == Kind.SINGLE_CHOICE:
        fields["selected"] = [str(value)]
    elif rendered.kind == Kind.MULTI_CHOICE:
        fields["selected"] = [str(v) for v in value]
    response, _ = EvaluationResponse.objects.update_or_create(defaults=fields, **lookup)
    return response


# --- Changing a form without touching what was answered -----------------------------


def _log(action, obj, *, user, request, metadata):
    """Staff act through the admin; a migration or a test has no actor and logs as the system."""
    if user is None and request is None:
        record(action, obj, system="evaluation-forms", metadata=metadata)
    else:
        record(action, obj, user=user, request=request, metadata=metadata)


def _copy_questions(source_version, target_version):
    for q in source_version.questions.order_by("position"):
        EvaluationQuestion.objects.create(
            version=target_version,
            question_key=q.question_key,
            position=q.position,
            prompt=q.prompt,
            help_text=q.help_text,
            kind=q.kind,
            required=q.required,
            choices=list(q.choices),
        )


@transaction.atomic
def new_version(form, *, user=None, note="", request=None):
    """
    The next version of a form, starting as a copy of the current one.
    This is how an active form is edited: the old version, and every
    submission against it, stay exactly as they were.
    """
    current = form.current_version
    version = EvaluationFormVersion.objects.create(
        form=form, number=(current.number + 1) if current else 1, note=note, created_by=user
    )
    if current is not None:
        _copy_questions(current, version)
    _log(
        "evaluation_form.version_created",
        version,
        user=user,
        request=request,
        metadata={"form": str(form.pk), "number": version.number, "copied_from": current.number if current else None},
    )
    return version


@transaction.atomic
def duplicate_form(form, *, name, user=None, request=None):
    """A new draft form in the same program, with a copy of the current questions as v1."""
    if EvaluationForm.objects.filter(program=form.program, name=name).exists():
        raise ValidationError(f"There is already a form called {name!r} in {form.program}.")
    copy = EvaluationForm.objects.create(
        program=form.program, name=name, status=EvaluationForm.Status.DRAFT, created_by=user
    )
    version = EvaluationFormVersion.objects.create(
        form=copy, number=1, note=f"Copied from {form} v{form.current_version.number if form.current_version else '-'}", created_by=user
    )
    if form.current_version is not None:
        _copy_questions(form.current_version, version)
    _log(
        "evaluation_form.duplicated",
        copy,
        user=user,
        request=request,
        metadata={"from": str(form.pk)},
    )
    return copy


@transaction.atomic
def create_standard_form(program, *, user=None, attach=True):
    """
    The provisional "Standard CME evaluation" for a program, active, as
    v1, attached as the program default when `attach`. Idempotent.
    """
    form, created = EvaluationForm.objects.get_or_create(
        program=program,
        name=STANDARD_FORM_NAME,
        defaults={"status": EvaluationForm.Status.ACTIVE, "created_by": user},
    )
    if created:
        version = EvaluationFormVersion.objects.create(
            form=form, number=1, note="Provisional, pending McGill CPD.", created_by=user
        )
        for position, spec in enumerate(STANDARD_QUESTIONS, start=1):
            EvaluationQuestion.objects.create(version=version, position=position, **spec)
    if attach and program.default_evaluation_form_id is None:
        program.default_evaluation_form = form
        program.save(update_fields=["default_evaluation_form"])
    return form
