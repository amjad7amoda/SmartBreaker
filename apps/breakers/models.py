from django.db import models

from apps.organizations.models import Organization


class Breaker(models.Model):
    """A smart breaker installed at a site, plus the KBS metadata attached to it.

    Live electrical state lives in ``BreakerStatus`` (one row, overwritten on
    every report); historical samples live in ``BreakerReading`` (time series,
    used by the observing phase and sudden-draw detection).
    """

    # Importance category of the load behind this breaker. The KBS sheds in the
    # order comfort -> normal and NEVER touches mandatory. 'ac_grid' marks the
    # single special breaker that connects the site to state-grid electricity:
    # switching it ON means "buy grid electricity".
    PRIORITY_TYPE_CHOICES = [
        ('mandatory', 'Mandatory'),  # must never be switched off by the KBS (servers, ...)
        ('normal', 'Normal'),        # may be shed when the system is stressed
        ('comfort', 'Comfort'),      # luxury loads: first to shed, last to restore
        ('ac_grid', 'AC Grid'),      # ON = site draws state-grid electricity
    ]

    # Rank used to order categories when shedding/restoring: higher = more
    # important. ac_grid is an actuator, not a load, so it is never ranked.
    CATEGORY_RANK = {'comfort': 1, 'normal': 2, 'mandatory': 3}

    # Electrical behaviour of the load, used for inverter head-room calculations.
    LOAD_TYPE_CHOICES = [
        ('motor', 'Motor'),    # inrush profile: draws peak_load_W for ~motor_peak_minutes after switch-on, then settles to mean_load_W (AC units, pumps)
        ('normal', 'Normal'),  # flat profile: draws about mean_load_W the whole time
    ]

    device_id       = models.CharField(max_length=100, unique=True)                                       # hardware identifier reported by the smart breaker (unitless)
    organization    = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='breakers')  # site (one Organization = one physical site)
    priority_type   = models.CharField(max_length=20, choices=PRIORITY_TYPE_CHOICES, default='normal')    # importance category, chosen by the user (see choices)
    priority_degree = models.PositiveIntegerField(default=1)                                              # importance inside the category, chosen by the user; higher = more important (positive integer, unitless)
    load_type       = models.CharField(max_length=20, choices=LOAD_TYPE_CHOICES, default='normal')        # electrical load profile (see choices)
    peak_load_W     = models.FloatField(null=True, blank=True)                                            # highest sustained draw, learned in the observing phase (W)
    mean_load_W     = models.FloatField(null=True, blank=True)                                            # steady-state average draw, learned in the observing phase (W)
    cycle_start     = models.TimeField(null=True, blank=True)                                             # daily schedule window start for comfort loads (local clock time)
    cycle_end       = models.TimeField(null=True, blank=True)                                             # daily schedule window end for comfort loads (local clock time)
    locked_out      = models.BooleanField(default=False)                                                  # True after the KBS trips this breaker (e.g. night sudden-draw); only the user may re-enable (flag)
    lockout_reason  = models.CharField(max_length=255, blank=True, default='')                            # human-readable reason the lockout was applied (text)
    locked_at       = models.DateTimeField(null=True, blank=True)                                         # when the lockout was applied (UTC timestamp)
    created_at      = models.DateTimeField(auto_now_add=True)                                             # row creation time (UTC timestamp)

    class Meta:
        ordering = ['organization', 'priority_type', '-priority_degree']

    def __str__(self):
        return f'{self.device_id} ({self.organization.name})'

    @property
    def category_rank(self):
        """Numeric importance of the category: 3=mandatory, 2=normal, 1=comfort, 0=ac_grid (unitless)."""
        return self.CATEGORY_RANK.get(self.priority_type, 0)


class BreakerStatus(models.Model):
    """Latest live state reported for one breaker.

    Exactly one row per breaker; every report from the edge overwrites it.
    """

    # What the relay does when mains power returns after an outage.
    RELAY_STATUS_CHOICES = [
        ('power_off', 'Power Off'),  # relay comes back OFF after power loss
        ('power_on', 'Power On'),    # relay comes back ON after power loss
        ('last', 'Last'),            # relay restores its last position after power loss
    ]

    breaker             = models.OneToOneField(Breaker, on_delete=models.CASCADE, related_name='status')  # the breaker this state belongs to
    switch              = models.BooleanField(default=False)                                              # relay position: True = ON (load powered), False = OFF (flag)
    countdown_1_s       = models.PositiveIntegerField(default=0)                                          # remaining time of the on-device flip timer; 0 = no timer armed (s)
    cur_current_mA      = models.FloatField(null=True, blank=True)                                        # instantaneous current through the breaker (mA)
    cur_power_mW        = models.FloatField(null=True, blank=True)                                        # instantaneous active power through the breaker (mW)
    cur_voltage_mV      = models.FloatField(null=True, blank=True)                                        # instantaneous voltage at the breaker (mV)
    fault               = models.CharField(max_length=100, blank=True, default='')                        # device fault flags (overheating / overvoltage / overcurrent ...); empty = healthy (text)
    relay_status        = models.CharField(max_length=20, choices=RELAY_STATUS_CHOICES, default='last')   # power-recovery behaviour configured on the device (see choices)
    child_lock          = models.BooleanField(default=False)                                              # True = physical buttons locked; remote control still works (flag)
    cycle_time          = models.CharField(max_length=100, blank=True, default='')                        # raw on-device cycling-schedule string, stored as reported (text)
    online              = models.BooleanField(default=False)                                              # True = breaker currently reachable on the network (flag)
    last_switched_on_at = models.DateTimeField(null=True, blank=True)                                     # when switch last went OFF -> ON; motor loads draw peak until motor_peak_minutes after this (UTC timestamp)
    reported_at         = models.DateTimeField(auto_now=True)                                             # when this state was last received (UTC timestamp)

    class Meta:
        verbose_name_plural = 'breaker statuses'

    def __str__(self):
        return f'{self.breaker.device_id}: {"ON" if self.switch else "OFF"}'


class BreakerReading(models.Model):
    """One per-breaker consumption sample (time series).

    Feeds the observing-phase learning (mean/peak load, average night usage)
    and the night sudden-draw culprit detection.
    """

    breaker      = models.ForeignKey(Breaker, on_delete=models.CASCADE, related_name='readings')  # the breaker this sample belongs to
    timestamp    = models.DateTimeField()                                                         # sample time as measured at the edge (UTC timestamp)
    switch       = models.BooleanField()                                                          # relay position at sample time: True = ON (flag)
    cur_power_mW = models.FloatField(null=True, blank=True)                                       # active power at sample time (mW)

    class Meta:
        ordering = ['-timestamp']
        indexes = [models.Index(fields=['breaker', '-timestamp'])]
        constraints = [
            models.UniqueConstraint(fields=['breaker', 'timestamp'], name='uniq_breaker_reading'),
        ]

    def __str__(self):
        return f'{self.breaker.device_id} @ {self.timestamp:%Y-%m-%d %H:%M:%S}'
