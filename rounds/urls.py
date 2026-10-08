from django.urls import path

from . import views

app_name = "rounds"

urlpatterns = [
    path("me/disclosure/", views.disclosure, name="disclosure"),
]
