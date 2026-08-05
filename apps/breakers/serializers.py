from django.core.cache import cache
from rest_framework import serializers

from . import exceptions
from .models import Breaker, BreakerAction, TuyaCredential
from .services import MAX_COUNTDOWN_MINUTES
from .tuya import TuyaClient, TuyaError


class TuyaCredentialSerializer(serializers.ModelSerializer):
    client_secret = serializers.CharField(write_only=True, trim_whitespace=True, required=False)
    organization_name = serializers.CharField(source='organization.name', read_only=True)

    class Meta:
        model = TuyaCredential
        fields = (
            'id', 'organization', 'organization_name', 'client_id',
            'client_secret', 'region', 'created_at', 'updated_at',
        )
        read_only_fields = ('id', 'created_at', 'updated_at')

    def validate(self, attrs):
        if self.instance:
            secret = attrs.get('client_secret') or self.instance.client_secret
        else:
            secret = attrs.get('client_secret')
            if not secret:
                raise serializers.ValidationError({'client_secret': 'This field is required.'})

        candidate = TuyaCredential(
            client_id=attrs.get('client_id', getattr(self.instance, 'client_id', '')),
            region=attrs.get('region', getattr(self.instance, 'region', 'us')),
        )
        candidate.client_secret = secret

        cache.delete(f'tuya:token:{candidate.client_id}')
        try:
            TuyaClient(candidate).verify()
        except TuyaError as exc:
            raise serializers.ValidationError({
                'client_secret': (
                    f'Tuya rejected these credentials ({exc.code}: {exc.message}). '
                    'Check the Access Secret and that the region matches the project.'
                )
            })
        return attrs

    def create(self, validated_data):
        secret = validated_data.pop('client_secret')
        credential = TuyaCredential(**validated_data)
        credential.client_secret = secret
        credential.save()
        return credential

    def update(self, instance, validated_data):
        secret = validated_data.pop('client_secret', None)
        for field, value in validated_data.items():
            setattr(instance, field, value)
        if secret:
            instance.client_secret = secret
        instance.save()
        return instance


class BreakerSerializer(serializers.ModelSerializer):
    organization_name = serializers.CharField(source='organization.name', read_only=True)
    class Meta:
        model = Breaker
        fields = (
            'id', 'name', 'device_id', 'organization', 'organization_name', 'type', 'priority',
            'protected', 'child_lock', 'peak_load', 'mean_load', 'cycle_start',
            'cycle_end', 'created_at',
        )
        read_only_fields = fields

class BreakerSwitchSerializer(serializers.Serializer):
    state = serializers.ChoiceField(choices=('on', 'off'))
    reason = serializers.CharField(required=False, allow_blank=True, default='')

    @property
    def turn_on(self):
        return self.validated_data['state'] == 'on'

class BreakerChildLockSerializer(serializers.Serializer):
    enabled = serializers.BooleanField()
    reason = serializers.CharField(required=False, allow_blank=True, default='')


class BreakerCountdownSerializer(serializers.Serializer):

    minutes = serializers.IntegerField(min_value=0, max_value=MAX_COUNTDOWN_MINUTES)
    reason = serializers.CharField(required=False, allow_blank=True, default='')


class BreakerActionSerializer(serializers.ModelSerializer):
    device_id = serializers.CharField(source='breaker.device_id', read_only=True)
    breaker_name = serializers.CharField(source='breaker.name', read_only=True)
    organization = serializers.IntegerField(source='breaker.organization_id', read_only=True)
    actor_email = serializers.EmailField(source='actor.email', read_only=True, default=None)

    class Meta:
        model = BreakerAction
        fields = (
            'id', 'breaker', 'breaker_name', 'device_id', 'organization', 'action', 'source',
            'reason', 'actor', 'actor_email', 'confirmed', 'telemetry',
            'breaker_status', 'created_at',
        )
        read_only_fields = fields

class BreakerUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Breaker
        fields = (
            'id', 'name', 'device_id', 'organization', 'type', 'priority', 'protected',
            'child_lock', 'peak_load', 'mean_load', 'cycle_start', 'cycle_end', 'created_at',
        )
        read_only_fields = (
            'id', 'device_id', 'organization', 'created_at',
            'child_lock',
        )

class BreakerCreateSerializer(serializers.ModelSerializer):
    tuya = serializers.SerializerMethodField(read_only=True)
    class Meta:
        model = Breaker
        fields = (
            'id', 'name', 'device_id', 'organization', 'type', 'priority', 'protected',
            'child_lock', 'peak_load', 'mean_load', 'cycle_start', 'cycle_end',
            'created_at', 'tuya',
        )
        read_only_fields = ('id', 'created_at', 'tuya', 'child_lock')

    def get_tuya(self, obj):
        return getattr(self, '_verification', None)

    def validate(self, attrs):
        organization = attrs['organization']
        credential = TuyaCredential.objects.filter(organization=organization).first()
        if credential is None:
            raise serializers.ValidationError({
                'organization': (
                    f'No Tuya credentials configured for "{organization.name}". '
                    'Register the organization on Tuya before adding breakers.'
                )
            })

        try:
            result = TuyaClient(credential).get_device_properties(attrs['device_id'])
        except TuyaError as exc:
            raise exceptions.translate(exc, field='device_id')

        properties = {p['code']: p['value'] for p in result.get('properties', [])}
        online = properties.get('online_state') == 'online'

        self._verification = {
            'verified': True,
            'online': online,
            'switch_on': properties.get('switch_1'),
            'fault': properties.get('fault'),
        }

        if not online:
            self._verification['warning'] = 'Device is registered on Tuya but currently offline.'

        return attrs
