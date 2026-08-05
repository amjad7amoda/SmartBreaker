from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/accounts/', include('apps.accounts.urls')),
    path('api/organizations/', include('apps.organizations.urls')),
    path('api/breakers/', include('apps.breakers.urls')),
    path('api/telemetry/', include('apps.telemetry.urls')),
    path('api/notifications/', include('apps.notifications.urls')),
]
