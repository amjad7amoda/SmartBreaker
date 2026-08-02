from django.core.cache import cache
from rest_framework import serializers

from . import exceptions
from .models import Breaker, TuyaCredential
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
            'id', 'device_id', 'organization', 'organization_name', 'type', 'priority',
            'protected', 'child_lock', 'peak_load', 'mean_load', 'cycle_start',
            'cycle_end', 'created_at',
        )
        read_only_fields = fields


class BreakerSwitchSerializer(serializers.Serializer):
    state = serializers.ChoiceField(choices=('on', 'off'))

    @property
    def turn_on(self):
        return self.validated_data['state'] == 'on'


class BreakerChildLockSerializer(serializers.Serializer):
    enabled = serializers.BooleanField()


class BreakerUpdateSerializer(serializers.ModelSerializer):
    """device_id and organization are fixed at creation: changing either would
    describe a different physical device, so it is a create, not an edit.
    Keeping them out also means an edit never has to call Tuya."""

    class Meta:
        model = Breaker
        fields = (
            'id', 'device_id', 'organization', 'type', 'priority', 'protected',
            'child_lock', 'peak_load', 'mean_load', 'cycle_start', 'cycle_end', 'created_at',
        )
        read_only_fields = (
            'id', 'device_id', 'organization', 'created_at',
            # Owned by the device; use the child-lock endpoint to change it.
            'child_lock',
        )


class BreakerCreateSerializer(serializers.ModelSerializer):
    tuya = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = Breaker
        fields = (
            'id', 'device_id', 'organization', 'type', 'priority', 'protected',
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
        # An unpowered breaker is still a legitimate registration, so being
        # offline is reported rather than rejected.
        if not online:
            self._verification['warning'] = 'Device is registered on Tuya but currently offline.'
        return attrs
