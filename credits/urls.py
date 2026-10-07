from django.urls import path

from . import views

app_name = "credits"

urlpatterns = [
    # Stable per session: this is the URL the reminder email will carry.
    path("evaluate/<uuid:session_id>/", views.evaluate, name="evaluate"),
    path("evaluate/event/<uuid:event_id>/", views.evaluate_event, name="evaluate_event"),
]
