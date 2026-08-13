from django.conf import settings
from django.db import models

from apps.organizations.models import Organization

from . import crypto


class TuyaCredential(models.Model):
    REGION_CHOICES = [
        ('us', 'Western America'),
        ('eu', 'Central Europe'),
        ('cn', 'China'),
        ('in', 'India'),
    ]

    API_BASE_URLS = {
        'us': 'https://openapi.tuyaus.com',
        'eu': 'https://openapi.tuyaeu.com',
        'cn': 'https://openapi.tuyacn.com',
        'in': 'https://openapi.tuyain.com',
    }

    organization = models.OneToOneField(
        Organization, on_delete=models.CASCADE, related_name='tuya_credential'
    )
    client_id               = models.CharField(max_length=64)
    encrypted_client_secret = models.TextField()
    region                  = models.CharField(max_length=2, choices=REGION_CHOICES, default='us')
    created_at              = models.DateTimeField(auto_now_add=True)
    updated_at              = models.DateTimeField(auto_now=True)

    @property
    def client_secret(self):
        return crypto.decrypt(self.encrypted_client_secret)

    @client_secret.setter
    def client_secret(self, raw_secret):
        self.encrypted_client_secret = crypto.encrypt(raw_secret)

    @property
    def api_base_url(self):
        return self.API_BASE_URLS[self.region]

    def __str__(self):
        return f'Tuya credential for {self.organization.name} ({self.region})'


class Breaker(models.Model):

    PRIORITY_TYPE_CHOICES = [
        ('mandatory', 'Mandatory'),  # must never be switched off by the KBS (servers, ...)
        ('normal', 'Normal'),        # may be shed when the system is stressed
        ('comfort', 'Comfort'),      # luxury loads: first to shed, last to restore
        ('ac_grid', 'AC Grid'),      # ON = site draws state-grid electricity
    ]

    CATEGORY_RANK = {'comfort': 1, 'normal': 2, 'mandatory': 3}

    LOAD_TYPE_CHOICES = [
        ('motor', 'Motor'),
        ('normal', 'Normal'),
    ]

    device_id = models.CharField(max_length=100, unique=True)
    name = models.CharField(max_length=100, blank=True)
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name='breakers'
    )
    priority_type = models.CharField(
        max_length=20, choices=PRIORITY_TYPE_CHOICES, default='normal'
    )
    priority_degree = models.PositiveIntegerField(default=1)
    load_type = models.CharField(
        max_length=20, choices=LOAD_TYPE_CHOICES, default='normal'
    )
    # Mirrors the device's physical button lock. Kept in sync on every status
    # read, so it is a cache of device state rather than a source of truth.
    child_lock = models.BooleanField(default=False)
    peak_load_W = models.FloatField(null=True, blank=True)
    mean_load_W = models.FloatField(null=True, blank=True)
    cycle_start = models.TimeField(null=True, blank=True)
    cycle_end = models.TimeField(null=True, blank=True)
    locked_out = models.BooleanField(default=False)
    lockout_reason = models.CharField(max_length=255, blank=True, default='')
    locked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['organization', 'priority_type', '-priority_degree']

    @property
    def label(self):
        return self.name or self.device_id

    def __str__(self):
        return f'{self.label} ({self.organization.name})'

    @property
    def category_rank(self):
        """Numeric importance: 3=mandatory, 2=normal, 1=comfort, 0=AC grid."""
        return self.CATEGORY_RANK.get(self.priority_type, 0)


class BreakerAction(models.Model):

    ACTION_CHOICES = [
        ('switch_on', 'Switch on'),
        ('switch_off', 'Switch off'),
        ('child_lock_on', 'Child lock engaged'),
        ('child_lock_off', 'Child lock released'),
        ('countdown_set', 'Countdown scheduled'),
        ('countdown_cancel', 'Countdown cancelled'),
    ]
    SOURCE_CHOICES = [
        ('manual', 'Manual'),
        ('kbs', 'Knowledge-based system'),
    ]

    breaker = models.ForeignKey(Breaker, on_delete=models.CASCADE, related_name='actions')
    action  = models.CharField(max_length=20, choices=ACTION_CHOICES)
    source  = models.CharField(max_length=10, choices=SOURCE_CHOICES)
    reason  = models.TextField(blank=True)
    actor   = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='breaker_actions',
    )
    # Whether the device echoed the change back, not whether Tuya accepted the
    # request. False means the command was lost and the log disagrees with reality.
    confirmed = models.BooleanField(null=True)

    # Copied, not referenced: telemetry rows age out under the TimescaleDB retention
    # policy, and Tuya status is never persisted anywhere else.
    telemetry      = models.JSONField(null=True, blank=True)
    breaker_status = models.JSONField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['breaker', '-created_at'])]

    def __str__(self):
        return f'{self.action} on {self.breaker.device_id} ({self.source})'


class BreakerStatus(models.Model):

    RELAY_STATUS_CHOICES = [
        ('power_off', 'Power Off'),
        ('power_on', 'Power On'),
        ('last', 'Last'),
    ]

    breaker = models.OneToOneField(
        Breaker, on_delete=models.CASCADE, related_name='status'
    )
    switch = models.BooleanField(default=False)
    countdown_1_s = models.PositiveIntegerField(default=0)
    cur_current_mA = models.FloatField(null=True, blank=True)
    cur_power_mW = models.FloatField(null=True, blank=True)
    cur_voltage_mV = models.FloatField(null=True, blank=True)
    fault = models.CharField(max_length=100, blank=True, default='')
    relay_status = models.CharField(
        max_length=20, choices=RELAY_STATUS_CHOICES, default='last'
    )
    child_lock = models.BooleanField(default=False)
    cycle_time = models.CharField(max_length=100, blank=True, default='')
    online = models.BooleanField(default=False)
    # False when Tuya would not tell us the device's scale/unit spec, so the
    # measurements below are the device's own numbers rather than real units.
    # Stored per row because it is only knowable at the moment of the read.
    units_resolved = models.BooleanField(default=True)
    last_switched_on_at = models.DateTimeField(null=True, blank=True)
    reported_at = models.DateTimeField(auto_now=True)

    # The half of a snapshot that is an observation rather than derived state.
    # BreakerReading stores exactly these, so a historical sample and the
    # current status describe a breaker in the same terms. last_switched_on_at
    # is deliberately absent: it is a running summary of past samples, not
    # something observed at one instant.
    SAMPLE_FIELDS = (
        'switch', 'online', 'child_lock', 'countdown_1_s',
        'fault', 'relay_status', 'cycle_time', 'units_resolved',
        'cur_current_mA', 'cur_power_mW', 'cur_voltage_mV',
    )

    class Meta:
        verbose_name_plural = 'breaker statuses'

    def __str__(self):
        return f'{self.breaker.device_id}: {"ON" if self.switch else "OFF"}'

    def as_sample(self):
        """This snapshot as BreakerReading field values."""
        return {field: getattr(self, field) for field in self.SAMPLE_FIELDS}


class BreakerReading(models.Model):
    """One timestamped sample, carrying the same measurements as BreakerStatus.

    Rows are written on every poll and aged out by ``purge_breaker_readings``,
    so this is the history behind the single current-status row.
    """

    breaker = models.ForeignKey(
        Breaker, on_delete=models.CASCADE, related_name='readings'
    )
    timestamp = models.DateTimeField()
    switch = models.BooleanField()
    online = models.BooleanField(default=False)
    child_lock = models.BooleanField(default=False)
    countdown_1_s = models.PositiveIntegerField(default=0)
    fault = models.CharField(max_length=100, blank=True, default='')
    relay_status = models.CharField(
        max_length=20, choices=BreakerStatus.RELAY_STATUS_CHOICES, default='last'
    )
    cycle_time = models.CharField(max_length=100, blank=True, default='')
    units_resolved = models.BooleanField(default=True)
    cur_current_mA = models.FloatField(null=True, blank=True)
    cur_power_mW = models.FloatField(null=True, blank=True)
    cur_voltage_mV = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ['-timestamp']
        indexes = [models.Index(fields=['breaker', '-timestamp'])]
        constraints = [
            models.UniqueConstraint(fields=['breaker', 'timestamp'], name='uniq_breaker_reading'),
        ]

    def __str__(self):
        return f'{self.breaker.device_id} @ {self.timestamp:%Y-%m-%d %H:%M:%S}'
