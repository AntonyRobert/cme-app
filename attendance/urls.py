from django.urls import path

from . import views

app_name = "attendance"

urlpatterns = [
    path("scan/<uuid:session_id>/<int:window>/<str:token>/", views.scan, name="scan"),
    path("scan/done/", views.scan_complete, name="scan_complete"),
]
