from django.utils import timezone
from rest_framework import serializers

from .models import Breaker, BreakerReading, BreakerStatus


class BreakerStatusIngestSerializer(serializers.Serializer):
    device_id = serializers.CharField(max_length=100)                                        # hardware identifier of the reporting breaker (unitless)
    timestamp = serializers.DateTimeField(required=False)                                    # sample time at the edge; defaults to server receive time (UTC timestamp)
    switch = serializers.BooleanField()                                                   # relay position: True = ON (flag)
    countdown_1_s = serializers.IntegerField(required=False, default=0, min_value=0)             # remaining on-device flip timer; 0 = none armed (s)
    cur_current_mA = serializers.FloatField(required=False, allow_null=True, default=None)       # instantaneous current (mA)
    cur_power_mW = serializers.FloatField(required=False, allow_null=True, default=None)       # instantaneous active power (mW)
    cur_voltage_mV = serializers.FloatField(required=False, allow_null=True, default=None)       # instantaneous voltage (mV)
    fault = serializers.CharField(required=False, allow_blank=True, default='')          # device fault flags; empty = healthy (text)
    relay_status = serializers.ChoiceField(
        choices=BreakerStatus.RELAY_STATUS_CHOICES, required=False, default='last'
    )                                                                                            # power-recovery behaviour configured on the device
    child_lock = serializers.BooleanField(required=False, default=False)                      # physical buttons locked (flag)
    cycle_time = serializers.CharField(required=False, allow_blank=True, default='')          # raw on-device cycling-schedule string (text)
    online= serializers.BooleanField(required=False, default=True)                       # breaker reachable on the network (flag)

    def validate_device_id(self, value):
        try:
            self.context[f'breaker_{value}'] = Breaker.objects.get(device_id=value)
        except Breaker.DoesNotExist:
            raise serializers.ValidationError(f'Unknown breaker device_id: {value}')
        return value

    def create(self, validated_data):
        breaker = self.context[f'breaker_{validated_data["device_id"]}']
        sample_time = validated_data.get('timestamp') or timezone.now()  # sample time (UTC timestamp)

        status, _ = BreakerStatus.objects.get_or_create(breaker=breaker)
        switched_on = validated_data['switch'] and not status.switch  # OFF -> ON transition this report (flag)

        status.switch         = validated_data['switch']
        status.countdown_1_s  = validated_data['countdown_1_s']
        status.cur_current_mA = validated_data['cur_current_mA']
        status.cur_power_mW   = validated_data['cur_power_mW']
        status.cur_voltage_mV = validated_data['cur_voltage_mV']
        status.fault          = validated_data['fault']
        status.relay_status   = validated_data['relay_status']
        status.child_lock     = validated_data['child_lock']
        status.cycle_time     = validated_data['cycle_time']
        status.online         = validated_data['online']
        
        if switched_on:
            status.last_switched_on_at = sample_time
        status.save()

        BreakerReading.objects.get_or_create(
            breaker=breaker,
            timestamp=sample_time,
            defaults={
                'switch': validated_data['switch'],
                'cur_power_mW': validated_data['cur_power_mW'],
            },
        )
        return status


class BreakerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Breaker
        fields = (
            'id', 'device_id', 'organization',
            'priority_type', 'priority_degree', 'load_type',
            'peak_load_W', 'mean_load_W',
            'cycle_start', 'cycle_end',
            'locked_out', 'lockout_reason', 'locked_at',
            'created_at',
        )
        read_only_fields = ('lockout_reason', 'locked_at', 'created_at')
