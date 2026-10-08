"""
The presenter's own disclosure page: declare on the National Standard form,
confirm the standing declaration for each session they present, and copy
the disclosure slide text. Signed in as the attendee; nothing here takes an
object id in the URL.
"""
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from programs.models import Program
from signin.views import signed_in

from .coi import Role, confirm_for_session, declare, slide_text
from .models import COIDeclaration, SessionPresenter, coi_is_national_standard, coi_questions, coi_standard


def _version_for(person):
    """The questionnaire of the program they most recently present in, else the first program's."""
    presentation = (
        SessionPresenter.objects.filter(person=person)
        .select_related("session__event__program")
        .order_by("-session__start_at")
        .first()
    )
    if presentation is not None:
        return presentation.session.event.program.coi_question_version
    program = Program.objects.filter(is_active=True).order_by("name").first()
    return program.coi_question_version if program else None


def _tri(value):
    return {"yes": True, "no": False}.get(value)


@require_http_methods(["GET", "POST"])
@signed_in
def disclosure(request):
    person = request.person
    today = timezone.localdate()
    version = _version_for(person)
    current = COIDeclaration.objects.current_for(person, today)
    presentations = list(
        SessionPresenter.objects.filter(person=person, session__event__date__gte=today)
        .select_related("session__event", "coi_declaration")
        .order_by("session__start_at")
    )
    std = coi_standard()
    context = {
        "person": person,
        "current": current,
        "slide": slide_text(current) if current else "",
        "presentations": presentations,
        "version": version,
        "standard": bool(version) and coi_is_national_standard(version),
        "questions": coi_questions(version) if version else [],
        "std": std,
        "roles": std["roles"],
        "errors": {},
        "posted": {},
    }

    if request.method == "POST" and request.POST.get("action") == "confirm":
        presentation = next((p for p in presentations if str(p.pk) == request.POST.get("presentation")), None)
        if presentation is None:
            raise Http404
        try:
            confirm_for_session(presentation, person=person, request=request)
        except ValidationError as error:
            context["errors"] = {"confirm": " ".join(error.messages)}
            return render(request, "rounds/disclosure.html", context, status=400)
        return redirect("rounds:disclosure")

    if request.method == "POST":
        if not context["standard"]:
            raise Http404
        posted = request.POST
        answers = {}
        if _tri(posted.get("has_relationships")) is True:
            for key, _ in context["questions"]:
                answers[key] = (
                    bool(posted.get(f"q_{key}")),
                    posted.get(f"q_{key}_organizations", ""),
                    posted.get(f"q_{key}_description", ""),
                )
        role = posted.get("activity_role") or None
        try:
            declaration = declare(
                person,
                answers,
                version=version,
                role=role,
                role_other=posted.get("activity_role_other", ""),
                has_relationships=_tri(posted.get("has_relationships")),
                off_label=_tri(posted.get("off_label")) if role == Role.SPEAKER else None,
                generic_names=_tri(posted.get("generic_names")) if role == Role.SPEAKER else None,
                attested=bool(posted.get("attested")),
                attested_name=person.full_name,
                request=request,
            )
        except ValidationError as error:
            context["errors"] = {k: " ".join(v) for k, v in error.message_dict.items()}
            context["posted"] = posted
            return render(request, "rounds/disclosure.html", context, status=400)
        # Freshly declared: attach to every upcoming session they present that
        # has nothing attached yet, so the new declaration is the one in force.
        for presentation in presentations:
            if presentation.coi_declaration_id is None:
                presentation.coi_declaration = declaration
                presentation.save(update_fields=["coi_declaration"])
        return redirect("rounds:disclosure")

    return render(request, "rounds/disclosure.html", context)
