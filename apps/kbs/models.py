import uuid
from datetime import time

from django.contrib.auth.hashers import check_password, make_password
from django.db import models
from django.utils import timezone

from apps.breakers.models import Breaker
from apps.organizations.models import Organization

from .contracts import TIER2_ENGINE


class KBSSettings(models.Model):
    MODE_CHOICES = [('observing', 'Observing'), ('active', 'Active')]
    DATA_SOURCE_CHOICES = [('real', 'Real Site'), ('simulator', 'Simulator')]
    TIER2_POLICY_CHOICES = [
        ('crisp', 'Crisp'),
        ('fuzzy_shadow', 'Fuzzy shadow'),
        ('fuzzy_active', 'Fuzzy active'),
    ]

    organization = models.OneToOneField(
        Organization, on_delete=models.CASCADE, related_name='kbs_settings'
    )
    mode = models.CharField(max_length=20, choices=MODE_CHOICES, default='observing')
    data_source = models.CharField(max_length=20, choices=DATA_SOURCE_CHOICES, default='real')
    tier2_policy = models.CharField(
        max_length=20, choices=TIER2_POLICY_CHOICES, default='crisp',
    )
    power_saving = models.BooleanField(default=False)
    cycle_seconds = models.PositiveIntegerField(default=300)
    battery_capacity_Wh = models.FloatField(default=5000.0)
    night_reserve_percent = models.FloatField(default=30.0)
    stability_threshold_percent = models.FloatField(default=50.0)
    event_stability_threshold_percent = models.FloatField(default=80.0)
    battery_low_voltage_V = models.FloatField(default=24.0)
    battery_low_margin_V = models.FloatField(default=0.5)
    battery_shutdown_buffer_percent = models.FloatField(default=2.0)
    heatsink_temp_limit_C = models.FloatField(default=70.0)
    joule_deficit_limit_J = models.FloatField(default=10_800_000.0)
    grid_present_min_V = models.FloatField(default=100.0)
    deficit_window_minutes = models.PositiveIntegerField(default=30)
    max_inverter_power_W = models.FloatField(default=5000.0)
    sudden_drop_fraction = models.FloatField(default=0.4)
    sudden_draw_W = models.FloatField(default=1000.0)
    baseline_minutes = models.PositiveIntegerField(default=10)
    motor_peak_minutes = models.PositiveIntegerField(default=20)
    event_prep_hours = models.FloatField(default=24.0)
    day_start = models.TimeField(default=time(6, 0))
    day_end = models.TimeField(default=time(18, 0))
    pv_day_min_W = models.FloatField(default=10.0)
    observing_started_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = 'KBS settings'

    def __str__(self):
        return f'KBS settings for {self.organization.name} ({self.mode})'


class KBSControllerState(models.Model):
    BAND_CHOICES = [('low', 'Low'), ('watch', 'Watch'), ('high', 'High')]

    organization = models.OneToOneField(
        Organization, on_delete=models.CASCADE, related_name='kbs_controller_state'
    )
    current_band = models.CharField(
        max_length=10, choices=BAND_CHOICES, default='watch',
    )
    candidate_band = models.CharField(
        max_length=10, choices=BAND_CHOICES, blank=True, default='',
    )
    consecutive_cycles = models.PositiveSmallIntegerField(default=0)
    last_risk_score = models.FloatField(null=True, blank=True)
    last_evaluated_at = models.DateTimeField(null=True, blank=True)
    profile_version = models.CharField(max_length=64, default='mamdani-v1')
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return (
            f'{self.organization_id} fuzzy controller: {self.current_band} '
            f'({self.candidate_band or "-"} x{self.consecutive_cycles})'
        )


class ScheduledEvent(models.Model):
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name='scheduled_events'
    )
    name = models.CharField(max_length=255)
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    required_breakers = models.ManyToManyField(
        Breaker, blank=True, related_name='required_for_events'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['start_at']

    def __str__(self):
        return f'{self.name} ({self.start_at:%Y-%m-%d %H:%M})'


class EdgeDevice(models.Model):
    """Organization-scoped edge identity; plaintext secrets are never stored."""

    STATUS_CHOICES = [('active', 'Active'), ('revoked', 'Revoked')]

    device_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name='edge_devices'
    )
    name = models.CharField(max_length=100, default='Primary edge')
    secret_hash = models.CharField(max_length=128)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    last_seen_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('organization', 'name')]

    def set_secret(self, plaintext):
        self.secret_hash = make_password(plaintext)

    def secret_is_valid(self, plaintext):
        return self.status == 'active' and check_password(plaintext, self.secret_hash)

    def __str__(self):
        return f'{self.name} ({self.organization_id})'


class KBSDecision(models.Model):
    TIER_CHOICES = [('tier1', 'Tier 1'), ('tier2', 'Tier 2')]
    EVENT_TYPE_CHOICES = [
        ('decision', 'Decision'), ('clear', 'Clear'), ('error', 'Error'),
    ]

    event_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name='kbs_decisions'
    )
    edge_device = models.ForeignKey(
        EdgeDevice, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='decision_events',
    )
    tier = models.CharField(max_length=10, choices=TIER_CHOICES, default='tier2')
    event_type = models.CharField(
        max_length=20, choices=EVENT_TYPE_CHOICES, default='decision'
    )
    engine = models.CharField(max_length=150, default=TIER2_ENGINE)
    branch = models.CharField(max_length=100, blank=True)
    facts = models.JSONField(default=dict)
    trace_version = models.PositiveSmallIntegerField(default=1)
    trace = models.JSONField(default=list)
    policy = models.CharField(
        max_length=20, choices=KBSSettings.TIER2_POLICY_CHOICES, default='crisp',
    )
    counterfactual = models.JSONField(default=dict)
    occurred_at = models.DateTimeField(default=timezone.now)
    received_at = models.DateTimeField(auto_now_add=True)
    # Existing clients and ordering continue to use created_at.
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-occurred_at', '-received_at']
        indexes = [
            models.Index(fields=['organization', '-created_at']),
            models.Index(fields=['organization', 'tier', '-occurred_at']),
            models.Index(fields=['event_type', '-occurred_at']),
        ]

    @property
    def is_legacy(self):
        return self.trace_version == 0

    def __str__(self):
        return f'{self.organization_id} {self.tier} {self.branch} @ {self.occurred_at:%Y-%m-%d %H:%M:%S}'


class BreakerAction(models.Model):
    ACTION_CHOICES = [('on', 'Switch On'), ('off', 'Switch Off')]
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('scheduled', 'Scheduled'),
        ('applied', 'Applied'),
        ('blocked', 'Blocked'),
        ('failed', 'Failed'),
        ('noop', 'No-op'),
        ('suppressed_duplicate', 'Suppressed duplicate'),
        ('superseded', 'Superseded'),
    ]
    RESOLVED_STATUSES = {
        'applied', 'blocked', 'failed', 'noop', 'suppressed_duplicate',
        'superseded',
    }

    action_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    decision = models.ForeignKey(
        KBSDecision, on_delete=models.CASCADE, related_name='actions'
    )
    breaker = models.ForeignKey(
        Breaker, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='kbs_actions',
    )
    device_id = models.CharField(max_length=100)
    action = models.CharField(max_length=10, choices=ACTION_CHOICES)
    countdown_s = models.PositiveIntegerField(default=0)
    reason = models.CharField(max_length=255)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='pending')
    resulting_state = models.BooleanField(null=True, blank=True)
    executed_at = models.DateTimeField(null=True, blank=True)
    failure_reason = models.CharField(max_length=500, blank=True)
    executed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['status', '-created_at'])]

    def save(self, *args, **kwargs):
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).only(
                'device_id', 'action_id'
            ).first()
            if previous:
                self.device_id = previous.device_id
                self.action_id = previous.action_id
        elif not self.device_id and self.breaker_id:
            self.device_id = self.breaker.device_id
        if self.executed and self.status == 'pending':
            self.status = 'applied'
        self.executed = self.status in self.RESOLVED_STATUSES
        if self.executed and self.executed_at is None:
            self.executed_at = timezone.now()
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.device_id} -> {self.action} ({self.status})'


class Tier1SafetyState(models.Model):
    """Latest authoritative Tier-1 safety state for one organization.

    This is coordination state, not another rules engine: Tier-1 evaluates the
    hazard and this row lets Tier-2 observe the resulting safety hold.
    """

    organization = models.OneToOneField(
        Organization, on_delete=models.CASCADE, related_name='tier1_safety_state'
    )
    edge_device = models.ForeignKey(
        EdgeDevice, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='safety_states',
    )
    source_decision = models.ForeignKey(
        KBSDecision, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='safety_state_updates',
    )
    active = models.BooleanField(default=False)
    situation = models.CharField(max_length=100, blank=True)
    episode_id = models.UUIDField(null=True, blank=True, editable=False)
    commands = models.JSONField(default=list)
    source_occurred_at = models.DateTimeField(null=True, blank=True)
    activated_at = models.DateTimeField(null=True, blank=True)
    cleared_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        state = self.situation if self.active else 'clear'
        return f'{self.organization_id} Tier-1 safety: {state}'


class Alert(models.Model):
    KIND_CHOICES = [
        ('weather_drop', 'Weather Drop'),
        ('panel_fault', 'Panel Fault'),
        ('inverter_protection', 'Inverter Protection'),
        ('battery_low', 'Battery Low'),
        ('grid_outage', 'Grid Outage'),
        ('breaker_fault', 'Breaker Fault'),
        ('night_trip', 'Night Trip'),
    ]
    SEVERITY_CHOICES = [
        ('info', 'Info'), ('warning', 'Warning'), ('critical', 'Critical'),
    ]

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name='kbs_alerts'
    )
    decision = models.ForeignKey(
        KBSDecision, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='alerts',
    )
    kind = models.CharField(max_length=30, choices=KIND_CHOICES)
    severity = models.CharField(max_length=10, choices=SEVERITY_CHOICES, default='warning')
    message = models.CharField(max_length=500)
    suppressed = models.BooleanField(default=False)
    suppression_reason = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'[{self.severity}] {self.kind}: {self.message[:60]}'
