from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from . import views

urlpatterns = [
    path('requests/', views.RegistrationRequestListCreateView.as_view(), name='registration-request-list-create'),
    path('requests/<int:pk>/', views.RegistrationRequestDetailView.as_view(), name='registration-request-detail'),
    path('requests/<int:pk>/approve/', views.RegistrationRequestApproveView.as_view(), name='registration-request-approve'),
    path('requests/<int:pk>/deny/', views.RegistrationRequestDenyView.as_view(), name='registration-request-deny'),
    path('otp-login/', views.OTPLoginView.as_view(), name='otp-login'),
    path('resend-otp/', views.ResendOTPView.as_view(), name='resend-otp'),
    path('set-password/', views.SetPasswordView.as_view(), name='set-password'),
    path('forgot-password/', views.ForgotPasswordView.as_view(), name='forgot-password'),
    path('reset-password/', views.ResetPasswordConfirmView.as_view(), name='reset-password'),
    path('login/', views.LoginView.as_view(), name='login'),
    path('login/refresh/', TokenRefreshView.as_view(), name='login-refresh'),
]
