from django.urls import path

from . import views

app_name = "signin"

urlpatterns = [
    path("signin/", views.start, name="start"),
    path("signin/sent/", views.sent, name="sent"),
    path("signin/complete/", views.complete, name="complete"),
    path("signin/<str:token>/", views.redeem, name="redeem"),
    path("signout/", views.sign_out, name="signout"),
    path("signout/everywhere/", views.sign_out_everywhere, name="signout_everywhere"),
    path("me/", views.me, name="me"),
    path("me/reopen/", views.reopen, name="reopen"),
]
