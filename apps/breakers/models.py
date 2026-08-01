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
    organization= models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='breakers')
    type= models.CharField(max_length=20, choices=TYPE_CHOICES, default='normal')
    priority= models.PositiveIntegerField()
    protected= models.BooleanField(default=False)
    # Mirrors the device's physical button lock. Kept in sync on every status
    # read, so it is a cache of device state rather than a source of truth.
    child_lock = models.BooleanField(default=False)
    peak_load= models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True,)
    mean_load = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    cycle_start = models.TimeField(null=True, blank=True)
    cycle_end = models.TimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['organization', 'priority']
       

    def __str__(self):
        return f'{self.device_id} ({self.organization.name})'
