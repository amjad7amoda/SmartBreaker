from django.db import models

from apps.organizations.models import Organization


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
    peak_load= models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True,)
    mean_load = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    cycle_start = models.TimeField(null=True, blank=True)
    cycle_end = models.TimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['organization', 'priority']
       

    def __str__(self):
        return f'{self.device_id} ({self.organization.name})'
