from django.contrib import admin

from .models import Breaker


@admin.register(Breaker)
class BreakerAdmin(admin.ModelAdmin):
    list_display = (
        'device_id', 'organization', 'type', 'priority', 'protected',
        'peak_load', 'mean_load', 'cycle_start', 'cycle_end',
    )
    list_filter = ('type', 'protected', 'organization')
    search_fields = ('device_id', 'organization__name')
    readonly_fields = ('created_at',)
