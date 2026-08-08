from rest_framework.permissions import SAFE_METHODS, BasePermission


class IsAdminRole(BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.role == 'admin')


class IsTechnicianOrAdmin(BasePermission):
    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.role in ('technician', 'admin')
        )


class IsTechnicianOrAdminOrReadOnly(BasePermission):
    """Reading is open to any authenticated user; writing is technician/admin only."""

    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        return request.method in SAFE_METHODS or request.user.role in ('technician', 'admin')


class IsPasswordSet(BasePermission):
    """Blocks access until the user has replaced their OTP-issued account with a real password."""

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and not request.user.must_set_password)
