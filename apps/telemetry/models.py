"""Telemetry readings ingested from the Raspberry Pi (inverter QPIGS stream).

Each row is one inverter snapshot pushed by the edge agent. The table is
designed to become a TimescaleDB hypertable partitioned on ``timestamp``:
TimescaleDB requires the partitioning column to be part of every unique/primary
key, so we use a composite primary key ``(organization, timestamp)`` instead of
the default ``id`` column. When the extension is enabled this table can be
converted directly with ``SELECT create_hypertable('telemetry_reading',
'timestamp');`` without any schema surgery.
"""

from django.db import models

from apps.organizations.models import Organization


class Reading(models.Model):
    pk = models.CompositePrimaryKey('organization', 'timestamp')
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name='readings'
    )
    timestamp    = models.DateTimeField()
    received_at  = models.DateTimeField(auto_now_add=True)

    grid_voltage_V                 = models.FloatField(null=True, blank=True)
    grid_freq_Hz                   = models.FloatField(null=True, blank=True)
    ac_output_voltage_V            = models.FloatField(null=True, blank=True)
    ac_output_freq_Hz              = models.FloatField(null=True, blank=True)
    ac_output_apparent_power_VA    = models.FloatField(null=True, blank=True)
    ac_output_active_power_W       = models.FloatField(null=True, blank=True)
    output_load_percent            = models.FloatField(null=True, blank=True)
    bus_voltage_V                  = models.FloatField(null=True, blank=True)
    battery_voltage_V              = models.FloatField(null=True, blank=True)
    battery_charge_current_A       = models.FloatField(null=True, blank=True)
    battery_capacity_percent       = models.FloatField(null=True, blank=True)
    heatsink_temp_C                = models.FloatField(null=True, blank=True)
    pv_input_current_A             = models.FloatField(null=True, blank=True)
    pv_input_voltage_V             = models.FloatField(null=True, blank=True)
    battery_voltage_scc_V          = models.FloatField(null=True, blank=True)
    battery_discharge_current_A    = models.FloatField(null=True, blank=True)
    device_status_flags            = models.CharField(max_length=16, null=True, blank=True)
    battery_voltage_offset_fans_on = models.FloatField(null=True, blank=True)
    eeprom_version                 = models.CharField(max_length=16, null=True, blank=True)
    pv_charging_power_W            = models.FloatField(null=True, blank=True)
    device_status_flags2           = models.CharField(max_length=16, null=True, blank=True)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f'{self.organization_id} @ {self.timestamp:%Y-%m-%d %H:%M:%S}'
