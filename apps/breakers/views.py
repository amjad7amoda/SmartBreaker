from django.db import transaction
from rest_framework import generics, status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import (
    IsTechnicianOrAdmin,
    IsTechnicianOrAdminOrReadOnly,
)

from . import exceptions, scheduling, services
from .models import (
    Breaker,
    BreakerAction,
    BreakerReading,
    BreakerStatus,
    TuyaCredential,
)
from .serializers import (
    BreakerActionSerializer,
    BreakerChildLockSerializer,
    BreakerCountdownSerializer,
    BreakerCreateSerializer,
    BreakerSerializer,
    BreakerStatusIngestSerializer,
    BreakerSwitchSerializer,
    BreakerUpdateSerializer,
    TuyaCredentialSerializer,
)
from .tuya import TuyaError


def scoped_breakers(user):
    queryset = Breaker.objects.select_related('organization')
    if user.role in ('technician', 'admin'):
        return queryset
    return queryset.filter(organization__owner=user)


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
                device_id__in=[item['device_id'] for item in items],
            )
        }
        readings_created = 0
        for item in items:
            breaker = breakers[item['device_id']]
            status_row, _ = (
                BreakerStatus.objects.select_for_update().get_or_create(
                    breaker=breaker,
                )
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


class TuyaCredentialListCreateView(generics.ListCreateAPIView):
    permission_classes = [IsAuthenticated, IsTechnicianOrAdmin]
    serializer_class = TuyaCredentialSerializer
    queryset = TuyaCredential.objects.select_related('organization')


class TuyaCredentialDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [IsAuthenticated, IsTechnicianOrAdmin]
    serializer_class = TuyaCredentialSerializer
    queryset = TuyaCredential.objects.select_related('organization')


class BreakerListCreateView(generics.ListCreateAPIView):
    permission_classes = [IsTechnicianOrAdminOrReadOnly]

    def get_serializer_class(self):
        if self.request.method == 'POST':
            return BreakerCreateSerializer
        return BreakerSerializer

    def get_queryset(self):
        queryset = scoped_breakers(self.request.user)
        organization = self.request.query_params.get('organization')
        if organization:
            queryset = queryset.filter(organization_id=organization)
        return queryset


class BreakerDetailView(generics.RetrieveUpdateAPIView):
    permission_classes = [IsTechnicianOrAdminOrReadOnly]
    lookup_field = 'device_id'

    def get_serializer_class(self):
        if self.request.method == 'GET':
            return BreakerSerializer
        return BreakerUpdateSerializer

    def get_queryset(self):
        return scoped_breakers(self.request.user)


class BreakerDeleteView(generics.DestroyAPIView):
    permission_classes = [IsAuthenticated, IsTechnicianOrAdmin]
    lookup_field = 'device_id'

    def get_queryset(self):
        return scoped_breakers(self.request.user)


class BreakerStatusView(generics.RetrieveAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = BreakerSerializer
    lookup_field = 'device_id'

    def get_queryset(self):
        return scoped_breakers(self.request.user)

    def retrieve(self, request, *args, **kwargs):
        breaker = self.get_object()
        include_raw = request.query_params.get('raw') in ('1', 'true')
        if not include_raw:
            cached = scheduling.cached_status(breaker.device_id)
            if cached is not None:
                return Response(cached)

        try:
            breaker_status = services.read_status(
                breaker, include_raw=include_raw,
            )
        except LookupError as exc:
            raise ValidationError({'organization': str(exc)})
        except TuyaError as exc:
            raise exceptions.translate(exc, field='device_id')
        return Response(breaker_status)


class BreakerSwitchView(generics.GenericAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = BreakerSwitchSerializer
    lookup_field = 'device_id'

    def get_queryset(self):
        return scoped_breakers(self.request.user)

    def post(self, request, *args, **kwargs):
        breaker = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            result = services.set_switch(
                breaker,
                serializer.turn_on,
                actor=request.user,
                reason=serializer.validated_data['reason'],
            )
        except LookupError as exc:
            raise ValidationError({'organization': str(exc)})
        except TuyaError as exc:
            raise exceptions.translate(exc, field='device_id')
        return Response(result)


class BreakerChildLockView(generics.GenericAPIView):
    """Engage the physical device lockout, which also opens the relay."""

    permission_classes = [IsAuthenticated]
    serializer_class = BreakerChildLockSerializer
    lookup_field = 'device_id'

    def get_queryset(self):
        return scoped_breakers(self.request.user)

    def post(self, request, *args, **kwargs):
        breaker = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            result = services.set_child_lock(
                breaker,
                serializer.validated_data['enabled'],
                actor=request.user,
                reason=serializer.validated_data['reason'],
            )
        except LookupError as exc:
            raise ValidationError({'organization': str(exc)})
        except TuyaError as exc:
            raise exceptions.translate(exc, field='device_id')
        return Response(result)


class BreakerCountdownView(generics.GenericAPIView):
    """Tell the device to flip its relay after the requested delay."""

    permission_classes = [IsAuthenticated]
    serializer_class = BreakerCountdownSerializer
    lookup_field = 'device_id'

    def get_queryset(self):
        return scoped_breakers(self.request.user)

    def post(self, request, *args, **kwargs):
        breaker = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            result = services.set_countdown(
                breaker,
                serializer.validated_data['minutes'],
                actor=request.user,
                reason=serializer.validated_data['reason'],
            )
        except LookupError as exc:
            raise ValidationError({'organization': str(exc)})
        except TuyaError as exc:
            raise exceptions.translate(exc, field='device_id')
        return Response(result)


class BreakerActionListView(generics.ListAPIView):
    """Read-only actual-device command audit."""

    permission_classes = [IsAuthenticated]
    serializer_class = BreakerActionSerializer

    def get_queryset(self):
        queryset = BreakerAction.objects.filter(
            breaker__in=scoped_breakers(self.request.user),
        ).select_related('breaker', 'actor')

        params = self.request.query_params
        for field, lookup in (
            ('device_id', 'breaker__device_id'),
            ('organization', 'breaker__organization_id'),
            ('action', 'action'),
            ('source', 'source'),
        ):
            value = params.get(field)
            if value:
                queryset = queryset.filter(**{lookup: value})
        return queryset


class BreakerActionDetailView(generics.RetrieveAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = BreakerActionSerializer

    def get_queryset(self):
        return BreakerAction.objects.filter(
            breaker__in=scoped_breakers(self.request.user),
        ).select_related('breaker', 'actor')
