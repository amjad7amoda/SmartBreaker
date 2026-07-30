from django.contrib import admin

from .models import Alert, BreakerAction, KBSDecision, KBSSettings, ScheduledEvent


@admin.register(KBSSettings)
class KBSSettingsAdmin(admin.ModelAdmin):
    list_display = (
        'organization', 'mode', 'data_source', 'power_saving', 'cycle_seconds',
        'stability_threshold_percent', 'event_stability_threshold_percent',
        'night_reserve_percent',
    )
    list_filter = ('mode', 'data_source', 'power_saving')
    search_fields = ('organization__name',)


@admin.register(ScheduledEvent)
class ScheduledEventAdmin(admin.ModelAdmin):
    list_display = ('name', 'organization', 'start_at', 'end_at')
    list_filter = ('organization',)
    search_fields = ('name', 'organization__name')
    filter_horizontal = ('required_breakers',)


class BreakerActionInline(admin.TabularInline):
    model = BreakerAction
    extra = 0
    readonly_fields = ('breaker', 'action', 'countdown_s', 'reason', 'executed', 'created_at')


@admin.register(KBSDecision)
class KBSDecisionAdmin(admin.ModelAdmin):
    list_display = ('organization', 'branch', 'created_at')
    list_filter = ('branch', 'organization')
    readonly_fields = ('organization', 'branch', 'facts', 'created_at')
    inlines = [BreakerActionInline]


@admin.register(Alert)
class AlertAdmin(admin.ModelAdmin):
    list_display = ('organization', 'kind', 'severity', 'message', 'created_at')
    list_filter = ('kind', 'severity', 'organization')
    search_fields = ('message',)
