from .services import person_from_session


class PersonMiddleware:
    """
    request.person is the signed-in attendee or presenter, or None. This is
    the seam core.authz.current_person() reads; nothing else touches the
    session key directly.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.person = person_from_session(request.session)
        return self.get_response(request)
