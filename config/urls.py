from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView

admin.site.site_header = "Rounds administration"
admin.site.site_title = "Rounds administration"
admin.site.index_title = "Back office"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("signin.urls")),
    path("", include("attendance.urls")),
    path("", include("credits.urls")),
    path("", RedirectView.as_view(pattern_name="signin:me", permanent=False), name="home"),
]
