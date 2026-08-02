from datetime import time

from django.db import models

from apps.breakers.models import Breaker
from apps.organizations.models import Organization


class KBSSettings(models.Model):
    """Per-site configuration and tunable thresholds of the KBS engine.

    One row per organization. Values marked "learned" are filled in from the
    observing phase; the rest are chosen by the user/technician.
    """

    MODE_CHOICES = [
        ('observing', 'Observing'),  # first ~3 days: only collect data to learn loads/night usage; take no actions
        ('active', 'Active'),        # normal operation: run the decision cycle and command breakers
    ]

    DATA_SOURCE_CHOICES = [
        ('real', 'Real Site'),       # data comes from the real client's Pi; cycles run on the server wall clock
        ('simulator', 'Simulator'),  # data comes from the simulator; cycles anchor to the latest reading's (simulated) timestamp so simulated time drives day/night, windows and events
    ]

    organization  = models.OneToOneField(Organization, on_delete=models.CASCADE, related_name='kbs_settings')  # the site these settings belong to
    mode          = models.CharField(max_length=20, choices=MODE_CHOICES, default='observing')                 # engine lifecycle state (see choices)
    data_source   = models.CharField(max_length=20, choices=DATA_SOURCE_CHOICES, default='real')               # where this site's data comes from and which clock anchors the cycles (see choices)
    power_saving  = models.BooleanField(default=False)                                                         # user-selected mode: prefer shedding / best-subset over buying grid electricity (flag)
    cycle_seconds = models.PositiveIntegerField(default=300)                                                   # K: period between two KBS decision cycles (s)

    battery_capacity_Wh               = models.FloatField(default=5000.0)  # usable energy of the battery bank at 100% charge — set per site (Wh)
    night_reserve_percent             = models.FloatField(default=30.0)    # battery share that must stay reserved for mandatory loads overnight; learned in the observing phase (% of capacity)
    stability_threshold_percent       = models.FloatField(default=50.0)    # battery charge above which the battery counts as 'stable' on a normal day (% of capacity)
    event_stability_threshold_percent = models.FloatField(default=80.0)    # raised threshold applied while hoarding energy before a scheduled event (% of capacity)

    battery_low_voltage_V           = models.FloatField(default=24.0)      # battery voltage floor the system must never let the bank reach — set per battery chemistry/site (V)
    battery_low_margin_V            = models.FloatField(default=0.5)       # act this far above the floor: voltage <= floor + margin triggers battery protection (V)
    battery_shutdown_buffer_percent = models.FloatField(default=2.0)       # battery energy the site may still spend after the trigger, before countdowns flip breakers OFF (% of capacity)

    heatsink_temp_limit_C  = models.FloatField(default=70.0)               # inverter heatsink temperature above which protection shedding starts (°C)
    joule_deficit_limit_J  = models.FloatField(default=10_800_000.0)       # cumulative (load - PV) energy over the deficit window above which protection shedding starts; default = 3 kWh — normal night battery usage must NOT trip this (J)
    grid_present_min_V     = models.FloatField(default=100.0)              # grid voltage at/above which state-grid electricity counts as actually delivering (V)
    deficit_window_minutes = models.PositiveIntegerField(default=30)       # look-back window for the cumulative joule deficit (min)
    max_inverter_power_W   = models.FloatField(default=5000.0)             # maximum continuous AC output the inverter tolerates — set per site (W)

    sudden_drop_fraction = models.FloatField(default=0.4)                  # PV drop vs its recent baseline that counts as 'sudden'; 0.4 = 40% (fraction, 0-1)
    sudden_draw_W        = models.FloatField(default=1000.0)               # jump of total load above its baseline that counts as a night 'sudden draw' (W)
    baseline_minutes     = models.PositiveIntegerField(default=10)         # look-back window used to compute the PV/load baselines (min)

    motor_peak_minutes = models.PositiveIntegerField(default=20)           # how long a motor load draws peak_load_W after switching on (min)
    event_prep_hours   = models.FloatField(default=24.0)                   # length of the pre-event ramp: the stability threshold rises linearly from normal to event level over these hours (h)

    day_start    = models.TimeField(default=time(6, 0))                    # fallback start of daytime when the weather API gives no sunrise (local clock time)
    day_end      = models.TimeField(default=time(18, 0))                   # fallback end of daytime when the weather API gives no sunset (local clock time)
    pv_day_min_W = models.FloatField(default=10.0)                         # PV production at/above which the panels prove daylight, regardless of the clock window (W)

    observing_started_at = models.DateTimeField(null=True, blank=True)     # when the observing phase began (UTC timestamp)
    updated_at           = models.DateTimeField(auto_now=True)             # last settings change (UTC timestamp)

    class Meta:
        verbose_name_plural = 'KBS settings'

    def __str__(self):
        return f'KBS settings for {self.organization.name} ({self.mode})'


class ScheduledEvent(models.Model):
    """A user-announced event/gathering at the site (e.g. a meeting).

    Starting ``event_prep_hours`` before the event, the stability threshold
    ramps linearly from its normal level up to the event level so the battery
    hoards energy gradually. While the event is running, its
    ``required_breakers`` are treated like mandatory loads: kept ON and never
    shed.
    """

    organization      = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='scheduled_events')  # the site hosting the event
    name              = models.CharField(max_length=255)                                                            # human label of the event (text)
    start_at          = models.DateTimeField()                                                                      # event start (UTC timestamp)
    end_at            = models.DateTimeField()                                                                      # event end (UTC timestamp)
    required_breakers = models.ManyToManyField(
        Breaker, blank=True, related_name='required_for_events'
    )                                                                                                               # breakers the user needs ON for the whole event window
    created_at        = models.DateTimeField(auto_now_add=True)                                                     # row creation time (UTC timestamp)

    class Meta:
        ordering = ['start_at']

    def __str__(self):
        return f'{self.name} ({self.start_at:%Y-%m-%d %H:%M})'


class KBSDecision(models.Model):
    """One completed KBS cycle: which decision-tree branch fired and on what facts."""

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='kbs_decisions')  # the site this cycle ran for
    branch       = models.CharField(max_length=100)                                                         # decision-tree path code, e.g. 'day.surplus.comfort_on' (text)
    facts        = models.JSONField(default=dict)                                                           # working-memory snapshot the rules fired on: {'system': {...}, 'breakers': [...]} (JSON)
    created_at   = models.DateTimeField(auto_now_add=True)                                                  # cycle time (UTC timestamp)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['organization', '-created_at'])]

    def __str__(self):
        return f'{self.organization_id} {self.branch} @ {self.created_at:%Y-%m-%d %H:%M:%S}'


class BreakerAction(models.Model):
    """One switch command emitted by a KBS decision — the new state a breaker must move to."""

    ACTION_CHOICES = [
        ('on', 'Switch On'),   # close the relay: power the load (or start buying grid power for ac_grid)
        ('off', 'Switch Off'), # open the relay: cut the load (or stop buying grid power for ac_grid)
    ]

    decision    = models.ForeignKey(KBSDecision, on_delete=models.CASCADE, related_name='actions')  # the cycle that produced this command
    breaker     = models.ForeignKey(Breaker, on_delete=models.CASCADE, related_name='kbs_actions')  # the breaker being commanded
    action      = models.CharField(max_length=10, choices=ACTION_CHOICES)                           # target relay state (see choices)
    countdown_s = models.PositiveIntegerField(default=0)                                           # 0 = switch immediately; >0 = set the device countdown so the switch happens after this delay (s)
    reason      = models.CharField(max_length=255)                                                  # why the KBS chose this action (text)
    executed    = models.BooleanField(default=False)                                               # True once the edge confirms the switch was applied (flag)
    created_at  = models.DateTimeField(auto_now_add=True)                                           # command creation time (UTC timestamp)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.breaker.device_id} -> {self.action} ({self.reason})'


class Alert(models.Model):
    """A notification raised by the engine for the user/technician."""

    KIND_CHOICES = [
        ('weather_drop', 'Weather Drop'),               # winter daytime PV drop: most likely cloud or storm
        ('panel_fault', 'Panel Fault'),                 # summer daytime PV drop: likely panel fault or shading on the panel
        ('inverter_protection', 'Inverter Protection'), # protection shedding was executed to save the inverter
        ('battery_low', 'Battery Low'),                 # battery near its voltage floor: countdown shutdown of listed breakers scheduled
        ('grid_outage', 'Grid Outage'),                 # AC-grid breaker is ON but the state grid delivers no power
        ('breaker_fault', 'Breaker Fault'),             # a breaker reported a fault or went offline
        ('night_trip', 'Night Trip'),                   # a breaker was tripped at night and awaits user re-enable
    ]

    SEVERITY_CHOICES = [
        ('info', 'Info'),         # informational, no user action needed
        ('warning', 'Warning'),   # user should have a look
        ('critical', 'Critical'), # user action required / hardware at risk
    ]

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='kbs_alerts')  # the site the alert belongs to
    kind         = models.CharField(max_length=30, choices=KIND_CHOICES)                                 # alert category (see choices)
    severity     = models.CharField(max_length=10, choices=SEVERITY_CHOICES, default='warning')          # how urgent the alert is (see choices)
    message      = models.CharField(max_length=500)                                                      # human-readable description (text)
    created_at   = models.DateTimeField(auto_now_add=True)                                               # alert time (UTC timestamp)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'[{self.severity}] {self.kind}: {self.message[:60]}'
