from django.shortcuts import get_object_or_404
from rest_framework import generics, status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView

from . import services
from .models import RegistrationRequest, User
from .permissions import IsAdminRole
from .serializers import (
    ForgotPasswordSerializer,
    OTPLoginSerializer,
    RegistrationRequestReviewSerializer,
    RegistrationRequestSerializer,
    ResendOTPSerializer,
    ResetPasswordConfirmSerializer,
    SetPasswordSerializer,
)


class RegistrationRequestListCreateView(generics.ListCreateAPIView):
    queryset = RegistrationRequest.objects.all().order_by('-created_at')

    def get_permissions(self):
        if self.request.method == 'POST':
            return [AllowAny()]
        return [IsAuthenticated(), IsAdminRole()]

    def get_serializer_class(self):
        if self.request.method == 'POST':
            return RegistrationRequestSerializer
        return RegistrationRequestReviewSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        status_param = self.request.query_params.get('status')
        if status_param:
            queryset = queryset.filter(status=status_param)
        return queryset


class RegistrationRequestDetailView(generics.RetrieveAPIView):
    queryset = RegistrationRequest.objects.all()
    serializer_class = RegistrationRequestReviewSerializer
    permission_classes = [IsAuthenticated, IsAdminRole]


class RegistrationRequestApproveView(APIView):
    permission_classes = [IsAuthenticated, IsAdminRole]

    def post(self, request, pk):
        registration_request = get_object_or_404(RegistrationRequest, pk=pk)
        try:
            services.approve_request(registration_request, reviewed_by=request.user)
        except ValueError as exc:
            raise ValidationError(str(exc))
        return Response(RegistrationRequestSerializer(registration_request).data)


class RegistrationRequestDenyView(APIView):
    permission_classes = [IsAuthenticated, IsAdminRole]

    def post(self, request, pk):
        registration_request = get_object_or_404(RegistrationRequest, pk=pk)
        try:
            services.deny_request(registration_request, reviewed_by=request.user)
        except ValueError as exc:
            raise ValidationError(str(exc))
        return Response(RegistrationRequestSerializer(registration_request).data)


class OTPLoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = OTPLoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data['user']

        user.is_active = True
        user.clear_otp()
        user.save()

        refresh = RefreshToken.for_user(user)
        return Response({
            'access': str(refresh.access_token),
            'refresh': str(refresh),
            'must_set_password': user.must_set_password,
        })


class ResendOTPView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = ResendOTPSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            user = User.objects.get(email=serializer.validated_data['email'].lower())
            services.resend_otp(user)
        except (User.DoesNotExist, ValueError):
            pass

        return Response({'detail': 'If this account is eligible, a new OTP has been sent.'})


class SetPasswordView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = SetPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = request.user
        user.set_password(serializer.validated_data['new_password'])
        user.must_set_password = False
        user.save()

        return Response(status=status.HTTP_204_NO_CONTENT)


class ForgotPasswordView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = ForgotPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            user = User.objects.get(email=serializer.validated_data['email'].lower())
            services.request_password_reset(user)
        except (User.DoesNotExist, ValueError):
            pass

        return Response({'detail': 'If this account is eligible, a password reset code has been sent.'})


class ResetPasswordConfirmView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = ResetPasswordConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = serializer.validated_data['user']
        services.confirm_password_reset(user, serializer.validated_data['new_password'])

        return Response(status=status.HTTP_204_NO_CONTENT)


class LoginSerializer(TokenObtainPairSerializer):
    def validate(self, attrs):
        data = super().validate(attrs)
        if not self.user.is_active or self.user.must_set_password:
            raise ValidationError(
                'Account setup is not complete. Please log in with your OTP first.'
            )
        return data


class LoginView(TokenObtainPairView):
    serializer_class = LoginSerializer
