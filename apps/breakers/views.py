from django.db import transaction
from rest_framework import generics, mixins, status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsTechnicianOrAdmin

from . import exceptions, services
from .models import Breaker, BreakerReading, BreakerStatus, TuyaCredential
from .tuya import TuyaError
from .serializers import (
    BreakerChildLockSerializer,
    BreakerCreateSerializer,
    BreakerSerializer,
    BreakerStatusIngestSerializer,
    BreakerSwitchSerializer,
    BreakerUpdateSerializer,
    TuyaCredentialSerializer,
)


class BreakerStatusIngestView(APIView):
    """Ingest one simulator/Pi snapshot for every breaker in the payload."""

    permission_classes = [AllowAny]

    @transaction.atomic
    def post(self, request):
        serializer = BreakerStatusIngestSerializer(
            data=request.data,
            allow_empty=False,
        )
        serializer.is_valid(raise_exception=True)

        items = serializer.validated_data
        breakers = {
            breaker.device_id: breaker
            for breaker in Breaker.objects.select_for_update().filter(
                device_id__in=[item['device_id'] for item in items]
            )
        }
        readings_created = 0
        for item in items:
            breaker = breakers[item['device_id']]
            status_row, _ = BreakerStatus.objects.select_for_update().get_or_create(
                breaker=breaker
            )
            previous_switch = status_row.switch
            status_row.switch = item['switch']
            status_row.countdown_1_s = item['countdown_1_s']
            status_row.cur_current_mA = item.get('cur_current_mA')
            status_row.cur_power_mW = item.get('cur_power_mW')
            status_row.cur_voltage_mV = item.get('cur_voltage_mV')
            status_row.fault = item['fault']
            status_row.relay_status = item['relay_status']
            status_row.child_lock = item['child_lock']
            status_row.cycle_time = item['cycle_time']
            status_row.online = item['online']
            if item['switch'] and not previous_switch:
                status_row.last_switched_on_at = item['timestamp']
            status_row.save()

            if breaker.child_lock != item['child_lock']:
                breaker.child_lock = item['child_lock']
                breaker.save(update_fields=['child_lock'])

            _, created = BreakerReading.objects.get_or_create(
                breaker=breaker,
                timestamp=item['timestamp'],
                defaults={
                    'switch': item['switch'],
                    'cur_power_mW': item.get('cur_power_mW'),
                },
            )
            readings_created += int(created)

        return Response(
            {
                'received': len(items),
                'readings_created': readings_created,
            },
            status=status.HTTP_201_CREATED,
        )


class OrganizationScopedMixin:
    organization_lookup = 'organization'

    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        if user.role in ('technician', 'admin'):
            return queryset
        return queryset.filter(**{f'{self.organization_lookup}__owner': user})


class TechnicianWritesMixin:
    """Reading is open to any authenticated user; writing is technician/admin only."""

    def get_permissions(self):
        if self.request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            return [IsAuthenticated(), IsTechnicianOrAdmin()]
        return [IsAuthenticated()]


class TuyaCredentialListCreateView(
    mixins.ListModelMixin, mixins.CreateModelMixin, generics.GenericAPIView
):
    # Credentials are pure configuration, so even reading them is staff-only.
    permission_classes = [IsAuthenticated, IsTechnicianOrAdmin]
    serializer_class = TuyaCredentialSerializer
    queryset = TuyaCredential.objects.select_related('organization')

    def get(self, request, *args, **kwargs):
        return self.list(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        return self.create(request, *args, **kwargs)


class TuyaCredentialDetailView(
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    generics.GenericAPIView,
):
    permission_classes = [IsAuthenticated, IsTechnicianOrAdmin]
    serializer_class = TuyaCredentialSerializer
    queryset = TuyaCredential.objects.select_related('organization')

    def get(self, request, *args, **kwargs):
        return self.retrieve(request, *args, **kwargs)

    def patch(self, request, *args, **kwargs):
        return self.partial_update(request, *args, **kwargs)

    def put(self, request, *args, **kwargs):
        return self.update(request, *args, **kwargs)

    def delete(self, request, *args, **kwargs):
        return self.destroy(request, *args, **kwargs)


class BreakerListCreateView(
    TechnicianWritesMixin,
    OrganizationScopedMixin,
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    generics.GenericAPIView,
):
    queryset = Breaker.objects.select_related('organization')

    def get_serializer_class(self):
        return BreakerCreateSerializer if self.request.method == 'POST' else BreakerSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        organization = self.request.query_params.get('organization')
        return queryset.filter(organization_id=organization) if organization else queryset

    def get(self, request, *args, **kwargs):
        return self.list(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        return self.create(request, *args, **kwargs)


class BreakerStatusView(OrganizationScopedMixin, generics.GenericAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = BreakerSerializer
    queryset = Breaker.objects.select_related('organization')
    lookup_field = 'device_id'

    def get(self, request, *args, **kwargs):
        breaker = self.get_object()
        include_raw = request.query_params.get('raw') in ('1', 'true')
        try:
            status = services.read_status(breaker, include_raw=include_raw)
        except LookupError as exc:
            raise ValidationError({'organization': str(exc)})
        except TuyaError as exc:
            raise exceptions.translate(exc, field='device_id')
        return Response(status)


class DeviceActionMixin(OrganizationScopedMixin):
    """Resolves a scoped breaker and runs one Tuya write against it.

    Permission is carried entirely by the queryset scoping: technicians and
    admins are unscoped, and a home user can only resolve breakers belonging to
    an organization they own, so an unrelated device is a 404 rather than a
    controllable target.
    """

    permission_classes = [IsAuthenticated]
    queryset = Breaker.objects.select_related('organization')
    lookup_field = 'device_id'

    def perform_action(self, breaker, serializer):
        raise NotImplementedError

    def post(self, request, *args, **kwargs):
        breaker = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            result = self.perform_action(breaker, serializer)
        except LookupError as exc:
            raise ValidationError({'organization': str(exc)})
        except TuyaError as exc:
            raise exceptions.translate(exc, field='device_id')
        return Response(result)


class BreakerSwitchView(DeviceActionMixin, generics.GenericAPIView):
    serializer_class = BreakerSwitchSerializer

    def perform_action(self, breaker, serializer):
        return services.set_switch(breaker, serializer.turn_on)


class BreakerChildLockView(DeviceActionMixin, generics.GenericAPIView):
    """Engages the device lockout, which also opens the relay until released."""

    serializer_class = BreakerChildLockSerializer

    def perform_action(self, breaker, serializer):
        return services.set_child_lock(breaker, serializer.validated_data['enabled'])


class BreakerDetailView(
    TechnicianWritesMixin,
    OrganizationScopedMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    generics.GenericAPIView,
):
    queryset = Breaker.objects.select_related('organization')
    lookup_field = 'device_id'

    def get_serializer_class(self):
        return BreakerSerializer if self.request.method == 'GET' else BreakerUpdateSerializer

    def get(self, request, *args, **kwargs):
        return self.retrieve(request, *args, **kwargs)

    def patch(self, request, *args, **kwargs):
        return self.partial_update(request, *args, **kwargs)

    def put(self, request, *args, **kwargs):
        return self.update(request, *args, **kwargs)

    def delete(self, request, *args, **kwargs):
        return self.destroy(request, *args, **kwargs)
