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
