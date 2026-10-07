import itertools

from programs.models import Institution, Program, ProgramRole

_counter = itertools.count(1)


def make_institution(name="Test University", short_name="test"):
    institution, _ = Institution.objects.get_or_create(short_name=short_name, defaults={"name": name})
    return institution


def make_program(name=None, institution=None, **overrides):
    """
    A program with the default rates and settings. Without a name, tests
    share one program per institution, so events made by other factories
    land together.
    """
    institution = institution or make_institution()
    if name is None:
        # The shared program has the evaluation gate ON: most of the suite was
        # written against the gate and keeps testing it. The production
        # default is off; tests of that path make a named program.
        program, _ = Program.objects.get_or_create(
            institution=institution,
            slug="default",
            defaults={"name": "Default Program", "require_evaluation_for_credit": True},
        )
        if overrides:
            for key, value in overrides.items():
                setattr(program, key, value)
            program.save()
        return program
    n = next(_counter)
    return Program.objects.create(
        institution=institution, name=name, slug=overrides.pop("slug", f"p{n}"), **overrides
    )


def give_role(user, program, role=ProgramRole.Role.COORDINATOR):
    link, _ = ProgramRole.objects.update_or_create(
        user=user, program=program, defaults={"role": role}
    )
    return link


def make_signer(program=None, username=None):
    """A program admin of `program` (default: the shared program): someone who may sign off."""
    from people.tests.factories import make_staff

    user = make_staff(username)
    give_role(user, program or make_program(), ProgramRole.Role.PROGRAM_ADMIN)
    return user
