from django.contrib import admin
from django.urls import path

admin.site.site_header = "Rounds administration"
admin.site.site_title = "Rounds administration"
admin.site.index_title = "Back office"

urlpatterns = [
    path("admin/", admin.site.urls),
]
