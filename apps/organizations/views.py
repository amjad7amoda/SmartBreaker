from django.shortcuts import get_object_or_404
from rest_framework import generics
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsAdminRole, IsPasswordSet

from . import services
from .models import Organization
from .permissions import IsOwnerOrAdminForDelete
from .serializers import OrganizationCreateSerializer, OrganizationSerializer


class OrganizationListCreateView(generics.ListCreateAPIView):
    queryset = Organization.objects.all().order_by('-created_at')

    def get_permissions(self):
        if self.request.method == 'POST':
            return [IsAuthenticated(), IsPasswordSet()]
        return [IsAuthenticated()]

    def get_serializer_class(self):
        if self.request.method == 'POST':
            return OrganizationCreateSerializer
        return OrganizationSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user

        if user.role != 'admin':
            return queryset.filter(owner=user)

        status_param = self.request.query_params.get('status')
        if status_param:
            queryset = queryset.filter(status=status_param)

        owner_param = self.request.query_params.get('owner')
        if owner_param:
            queryset = queryset.filter(owner_id=owner_param)

        return queryset

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)


class OrganizationDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [IsAuthenticated, IsOwnerOrAdminForDelete]

    def get_serializer_class(self):
        if self.request.method in ('PUT', 'PATCH'):
            return OrganizationCreateSerializer
        return OrganizationSerializer

    def get_queryset(self):
        user = self.request.user
        if user.role == 'admin':
            return Organization.objects.all()
        return Organization.objects.filter(owner=user)


class OrganizationApproveView(APIView):
    permission_classes = [IsAuthenticated, IsAdminRole]

    def post(self, request, pk):
        organization = get_object_or_404(Organization, pk=pk)
        try:
            services.approve_organization(organization, reviewed_by=request.user)
        except ValueError as exc:
            raise ValidationError(str(exc))
        return Response(OrganizationSerializer(organization).data)


class OrganizationDenyView(APIView):
    permission_classes = [IsAuthenticated, IsAdminRole]

    def post(self, request, pk):
        organization = get_object_or_404(Organization, pk=pk)
        name, owner_email = organization.name, organization.owner.email
        try:
            services.deny_organization(organization, reviewed_by=request.user)
        except ValueError as exc:
            raise ValidationError(str(exc))
        return Response({'detail': f'Organization "{name}" for {owner_email} has been denied and removed.'})
