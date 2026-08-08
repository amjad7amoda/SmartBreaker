from django.contrib import admin

from .models import (
    Alert, BreakerAction, EdgeDevice, KBSControllerState, KBSDecision,
    KBSSettings, ScheduledEvent, Tier1SafetyState,
)


@admin.register(KBSSettings)
class KBSSettingsAdmin(admin.ModelAdmin):
    list_display = (
        'organization', 'mode', 'data_source', 'tier2_policy', 'power_saving', 'cycle_seconds',
        'stability_threshold_percent', 'event_stability_threshold_percent',
        'night_reserve_percent',
    )
    list_filter = ('mode', 'data_source', 'tier2_policy', 'power_saving')
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
    can_delete = False
    readonly_fields = (
        'action_id', 'breaker', 'device_id', 'action', 'countdown_s', 'reason',
        'status', 'resulting_state', 'executed', 'executed_at', 'failure_reason',
        'created_at',
    )


@admin.register(KBSDecision)
class KBSDecisionAdmin(admin.ModelAdmin):
    list_display = (
        'event_id', 'organization', 'tier', 'event_type', 'policy', 'branch',
        'occurred_at', 'received_at', 'edge_device',
    )
    list_filter = (
        'tier', 'event_type', 'policy', 'branch', 'organization', 'occurred_at',
        'edge_device__status',
    )
    search_fields = ('event_id', 'organization__name', 'branch', 'engine')
    readonly_fields = (
        'event_id', 'organization', 'edge_device', 'tier', 'event_type', 'engine',
        'branch', 'policy', 'counterfactual', 'facts', 'trace_version', 'trace', 'occurred_at', 'received_at',
        'created_at',
    )
    inlines = [BreakerActionInline]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(KBSControllerState)
class KBSControllerStateAdmin(admin.ModelAdmin):
    list_display = (
        'organization', 'current_band', 'candidate_band',
        'consecutive_cycles', 'last_risk_score', 'last_evaluated_at',
        'profile_version',
    )
    list_filter = ('current_band', 'candidate_band', 'profile_version')
    search_fields = ('organization__name',)
    readonly_fields = (
        'organization', 'current_band', 'candidate_band',
        'consecutive_cycles', 'last_risk_score', 'last_evaluated_at',
        'profile_version', 'updated_at',
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(EdgeDevice)
class EdgeDeviceAdmin(admin.ModelAdmin):
    list_display = ('device_id', 'name', 'organization', 'status', 'last_seen_at', 'updated_at')
    list_filter = ('status', 'organization')
    search_fields = ('device_id', 'name', 'organization__name')
    readonly_fields = ('device_id', 'secret_hash', 'last_seen_at', 'created_at', 'updated_at')


@admin.register(Tier1SafetyState)
class Tier1SafetyStateAdmin(admin.ModelAdmin):
    list_display = (
        'organization', 'active', 'situation', 'episode_id',
        'source_occurred_at', 'updated_at',
    )
    list_filter = ('active', 'situation', 'organization')
    search_fields = ('organization__name', 'episode_id', 'situation')
    readonly_fields = (
        'organization', 'edge_device', 'source_decision', 'active', 'situation',
        'episode_id', 'commands', 'source_occurred_at', 'activated_at',
        'cleared_at', 'updated_at',
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Alert)
class AlertAdmin(admin.ModelAdmin):
    list_display = (
        'organization', 'decision', 'kind', 'severity', 'suppressed', 'message', 'created_at',
    )
    list_filter = ('kind', 'severity', 'suppressed', 'organization')
    search_fields = ('message', 'decision__event_id')
    readonly_fields = (
        'organization', 'decision', 'kind', 'severity', 'message', 'suppressed',
        'suppression_reason', 'created_at',
    )
