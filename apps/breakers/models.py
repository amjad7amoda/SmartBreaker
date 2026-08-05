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

    TYPE_CHOICES = [
        ('motor', 'Motor'),
        ('normal', 'Normal'),
    ]

    device_id = models.CharField(max_length=100, unique=True)
    name = models.CharField(max_length=100, blank=True)
    organization= models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='breakers')
    type= models.CharField(max_length=20, choices=TYPE_CHOICES, default='normal')
    priority= models.PositiveIntegerField()
    protected= models.BooleanField(default=False)
    child_lock = models.BooleanField(default=False)
    peak_load= models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True,)
    mean_load = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    cycle_start = models.TimeField(null=True, blank=True)
    cycle_end = models.TimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['organization', 'priority']
       

    @property
    def label(self):
        """What to call this breaker to a human. The name is optional, so anything
        user-facing has to be able to fall back to the device id."""
        return self.name or self.device_id

    def __str__(self):
        return f'{self.label} ({self.organization.name})'


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
