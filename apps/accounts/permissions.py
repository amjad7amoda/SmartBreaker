from rest_framework.permissions import BasePermission


class IsAdminRole(BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.role == 'admin')


class IsPasswordSet(BasePermission):
    """Blocks access until the user has replaced their OTP-issued account with a real password."""

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and not request.user.must_set_password)
