from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from .models import RegistrationRequest, User


class RegistrationRequestSerializer(serializers.ModelSerializer):

    class Meta:
        model = RegistrationRequest
        fields = ('id', 'email', 'phone', 'role', 'status', 'created_at')
        read_only_fields = ('id', 'status', 'created_at')

    def validate_email(self, value):
        value = value.lower()
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError('An account with this email already exists.')
        if RegistrationRequest.objects.filter(email=value, status='pending').exists():
            raise serializers.ValidationError('A pending request with this email already exists.')
        return value


class OTPLoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    otp = serializers.CharField()

    def validate(self, data):
        try:
            user = User.objects.get(email=data['email'].lower())
        except User.DoesNotExist:
            raise serializers.ValidationError('Invalid email or OTP.')

        if not user.otp_is_valid(data['otp']):
            raise serializers.ValidationError('Invalid or expired OTP.')

        data['user'] = user
        return data


class ResendOTPSerializer(serializers.Serializer):
    email = serializers.EmailField()


class SetPasswordSerializer(serializers.Serializer):
    new_password = serializers.CharField(write_only=True, validators=[validate_password])
    new_password_confirm = serializers.CharField(write_only=True)

    def validate(self, data):
        if data['new_password'] != data['new_password_confirm']:
            raise serializers.ValidationError('Passwords do not match.')
        return data


class ForgotPasswordSerializer(serializers.Serializer):
    email = serializers.EmailField()


class ResetPasswordConfirmSerializer(serializers.Serializer):
    email = serializers.EmailField()
    code = serializers.CharField()
    new_password = serializers.CharField(write_only=True, validators=[validate_password])
    new_password_confirm = serializers.CharField(write_only=True)

    def validate(self, data):
        if data['new_password'] != data['new_password_confirm']:
            raise serializers.ValidationError('Passwords do not match.')

        try:
            user = User.objects.get(email=data['email'].lower())
        except User.DoesNotExist:
            raise serializers.ValidationError('Invalid email or code.')

        if not user.reset_code_is_valid(data['code']):
            raise serializers.ValidationError('Invalid or expired code.')

        data['user'] = user
        return data
