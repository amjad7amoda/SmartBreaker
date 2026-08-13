from rest_framework import serializers

from .models import Reading


class ReadingListSerializer(serializers.ListSerializer):

    def create(self, validated_data):
        readings = [Reading(**item) for item in validated_data]
        return Reading.objects.bulk_create(readings, ignore_conflicts=True)


class ReadingSerializer(serializers.ModelSerializer):

    class Meta:
        model = Reading
        list_serializer_class = ReadingListSerializer
        fields = (
            'organization', 'timestamp', 'received_at',
            'grid_voltage_V', 'grid_freq_Hz',
            'ac_output_voltage_V', 'ac_output_freq_Hz',
            'ac_output_apparent_power_VA', 'ac_output_active_power_W',
            'output_load_percent', 'bus_voltage_V', 'battery_voltage_V',
            'battery_charge_current_A', 'battery_capacity_percent',
            'heatsink_temp_C', 'pv_input_current_A', 'pv_input_voltage_V',
            'battery_voltage_scc_V', 'battery_discharge_current_A',
            'device_status_flags', 'battery_voltage_offset_fans_on',
            'eeprom_version', 'pv_charging_power_W', 'device_status_flags2',
        )
        read_only_fields = ('received_at',)


class ReadingOutputSerializer(ReadingSerializer):
    """Read-side view of a reading: same fields plus the site name."""

    organization_name = serializers.CharField(
        source='organization.name', read_only=True,
    )

    class Meta(ReadingSerializer.Meta):
        fields = ReadingSerializer.Meta.fields + ('organization_name',)
