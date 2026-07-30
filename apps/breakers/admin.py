from django.contrib import admin

from .models import Breaker, BreakerReading, BreakerStatus


@admin.register(Breaker)
class BreakerAdmin(admin.ModelAdmin):
    list_display = (
        'device_id', 'organization', 'priority_type', 'priority_degree',
        'load_type', 'peak_load_W', 'mean_load_W',
        'cycle_start', 'cycle_end', 'locked_out',
    )
    list_filter = ('priority_type', 'load_type', 'locked_out', 'organization')
    search_fields = ('device_id', 'organization__name')
    readonly_fields = ('created_at',)


@admin.register(BreakerStatus)
class BreakerStatusAdmin(admin.ModelAdmin):
    list_display = (
        'breaker', 'switch', 'online', 'fault',
        'cur_power_mW', 'cur_current_mA', 'cur_voltage_mV', 'reported_at',
    )
    list_filter = ('switch', 'online',)
    search_fields = ('breaker__device_id',)
    readonly_fields = ('reported_at',)


@admin.register(BreakerReading)
class BreakerReadingAdmin(admin.ModelAdmin):
    list_display = ('breaker', 'timestamp', 'switch', 'cur_power_mW')
    list_filter = ('switch',)
    search_fields = ('breaker__device_id',)
