from rest_framework.permissions import SAFE_METHODS, BasePermission


class IsOwnerOrAdminForDelete(BasePermission):
    """Owner can retrieve/update/delete their own organization; admin can additionally delete any."""

    def has_object_permission(self, request, view, obj):
        is_owner = obj.owner_id == request.user.id
        is_admin = request.user.role == 'admin'

        if request.method in SAFE_METHODS:
            return is_owner or is_admin
        if request.method == 'DELETE':
            return is_owner or is_admin
        return is_owner
