from django.db import models

from apps.organizations.models import Organization


class Breaker(models.Model):

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

    RELAY_STATUS_CHOICES = [
        ('power_off', 'Power Off'),
        ('power_on', 'Power On'),
        ('last', 'Last'),
    ]

    breaker             = models.OneToOneField(Breaker, on_delete=models.CASCADE, related_name='status') 
    switch              = models.BooleanField(default=False)                                             
    countdown_1_s       = models.PositiveIntegerField(default=0)                                         
    cur_current_mA      = models.FloatField(null=True, blank=True)                                       
    cur_power_mW        = models.FloatField(null=True, blank=True)                                       
    cur_voltage_mV      = models.FloatField(null=True, blank=True)                                       
    fault               = models.CharField(max_length=100, blank=True, default='')                       
    relay_status        = models.CharField(max_length=20, choices=RELAY_STATUS_CHOICES, default='last')  
    child_lock          = models.BooleanField(default=False)                                             
    cycle_time          = models.CharField(max_length=100, blank=True, default='')                       
    online              = models.BooleanField(default=False)                                             
    last_switched_on_at = models.DateTimeField(null=True, blank=True)                                    
    reported_at         = models.DateTimeField(auto_now=True)                                            

    class Meta:
        verbose_name_plural = 'breaker statuses'

    def __str__(self):
        return f'{self.breaker.device_id}: {"ON" if self.switch else "OFF"}'


class BreakerReading(models.Model):

    breaker      = models.ForeignKey(Breaker, on_delete=models.CASCADE, related_name='readings')  
    timestamp    = models.DateTimeField()                                                         
    switch       = models.BooleanField()                                                          
    cur_power_mW = models.FloatField(null=True, blank=True)                                       

    class Meta:
        ordering = ['-timestamp']
        indexes = [models.Index(fields=['breaker', '-timestamp'])]
        constraints = [
            models.UniqueConstraint(fields=['breaker', 'timestamp'], name='uniq_breaker_reading'),
        ]

    def __str__(self):
        return f'{self.breaker.device_id} @ {self.timestamp:%Y-%m-%d %H:%M:%S}'
