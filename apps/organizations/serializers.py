from rest_framework import serializers

from .models import Organization


class OrganizationCreateSerializer(serializers.ModelSerializer):

    class Meta:
        model = Organization
        fields = ('id', 'name', 'phone', 'latitude', 'longitude', 'status', 'created_at')
        read_only_fields = ('id', 'status', 'created_at')


class OrganizationSerializer(serializers.ModelSerializer):
    owner = serializers.EmailField(source='owner.email', read_only=True)
    reviewed_by = serializers.EmailField(source='reviewed_by.email', read_only=True, default=None)

    class Meta:
        model = Organization
        fields = (
            'id', 'name', 'phone', 'latitude', 'longitude', 'owner',
            'status', 'reviewed_by', 'reviewed_at', 'created_at',
        )
        read_only_fields = fields
