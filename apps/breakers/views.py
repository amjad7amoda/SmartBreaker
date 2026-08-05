from rest_framework import generics
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import IsTechnicianOrAdmin, IsTechnicianOrAdminOrReadOnly

from . import exceptions, scheduling, services
from .models import Breaker, BreakerAction, TuyaCredential
from .tuya import TuyaError
from .serializers import (
    BreakerActionSerializer,
    BreakerChildLockSerializer,
    BreakerCountdownSerializer,
    BreakerCreateSerializer,
    BreakerSerializer,
    BreakerSwitchSerializer,
    BreakerUpdateSerializer,
    TuyaCredentialSerializer,
)


def scoped_breakers(user):
    queryset = Breaker.objects.select_related('organization')
    if user.role in ('technician', 'admin'):
        return queryset
    return queryset.filter(organization__owner=user)


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
    permission_classes = []
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

        # While the organization's poller is running this is a cache hit, which is the
        # point of the poller: no Tuya round trip on the path the dashboard hammers.
        if not include_raw:
            cached = scheduling.cached_status(breaker.device_id)
            if cached is not None:
                return Response(cached)

        try:
            status = services.read_status(breaker, include_raw=include_raw)
        except LookupError as exc:
            raise ValidationError({'organization': str(exc)})
        except TuyaError as exc:
            raise exceptions.translate(exc, field='device_id')
        return Response(status)


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
                breaker, serializer.turn_on,
                actor=request.user, reason=serializer.validated_data['reason'],
            )
        except LookupError as exc:
            raise ValidationError({'organization': str(exc)})
        except TuyaError as exc:
            raise exceptions.translate(exc, field='device_id')
        return Response(result)


class BreakerChildLockView(generics.GenericAPIView):
    """Engages the device lockout, which also opens the relay until released."""

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
                breaker, serializer.validated_data['enabled'],
                actor=request.user, reason=serializer.validated_data['reason'],
            )
        except LookupError as exc:
            raise ValidationError({'organization': str(exc)})
        except TuyaError as exc:
            raise exceptions.translate(exc, field='device_id')
        return Response(result)


class BreakerCountdownView(generics.GenericAPIView):
    """Tells the device to flip the relay on its own after N minutes, so the
    switch still happens if this server or the network drops in between."""

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
                breaker, serializer.validated_data['minutes'],
                actor=request.user, reason=serializer.validated_data['reason'],
            )
        except LookupError as exc:
            raise ValidationError({'organization': str(exc)})
        except TuyaError as exc:
            raise exceptions.translate(exc, field='device_id')
        return Response(result)


class BreakerActionListView(generics.ListAPIView):
    """Read-only: rows are written by the services that send the commands."""

    permission_classes = [IsAuthenticated]
    serializer_class = BreakerActionSerializer

    def get_queryset(self):
        queryset = BreakerAction.objects.filter(
            breaker__in=scoped_breakers(self.request.user)
        ).select_related('breaker', 'actor')

        params = self.request.query_params
        for field, lookup in (('device_id', 'breaker__device_id'),
                              ('organization', 'breaker__organization_id'),
                              ('action', 'action'),
                              ('source', 'source')):
            value = params.get(field)
            if value:
                queryset = queryset.filter(**{lookup: value})
        return queryset


class BreakerActionDetailView(generics.RetrieveAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = BreakerActionSerializer

    def get_queryset(self):
        return BreakerAction.objects.filter(
            breaker__in=scoped_breakers(self.request.user)
        ).select_related('breaker', 'actor')
