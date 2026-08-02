from rest_framework import generics, mixins
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import IsTechnicianOrAdmin

from . import exceptions, services
from .models import Breaker, TuyaCredential
from .tuya import TuyaError
from .serializers import (
    BreakerChildLockSerializer,
    BreakerCreateSerializer,
    BreakerSerializer,
    BreakerSwitchSerializer,
    BreakerUpdateSerializer,
    TuyaCredentialSerializer,
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
