from django.contrib.auth.models import AbstractUser


class User(AbstractUser):
    """
    A staff account that logs into the admin.

    Not the same thing as a Person: attendees and presenters never get one.
    It adds nothing to Django's user yet. It exists because Django cannot
    switch to a custom user model after the first migration.
    """
