from django.contrib import admin

from . import services
from .models import RegistrationRequest, User


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ('email', 'role', 'is_active', 'must_set_password', 'is_staff', 'created_at')
    list_filter = ('role', 'is_active', 'must_set_password')
    search_fields = ('email',)
    readonly_fields = ('created_at',)

    def has_add_permission(self, request):
        # Accounts are only created via the registration-request approval flow,
        # or as admins via `manage.py createsuperuser`.
        return False


@admin.register(RegistrationRequest)
class RegistrationRequestAdmin(admin.ModelAdmin):
    list_display = ('email', 'phone', 'role', 'status', 'reviewed_by', 'created_at')
    list_filter = ('status', 'role')
    search_fields = ('email', 'phone')
    readonly_fields = ('status', 'reviewed_by', 'reviewed_at', 'created_user', 'created_at')
    actions = ['approve_selected', 'deny_selected']

    def approve_selected(self, request, queryset):
        approved = 0
        for registration_request in queryset:
            if registration_request.status != 'pending':
                continue
            services.approve_request(registration_request, reviewed_by=request.user)
            approved += 1
        self.message_user(request, f'Approved {approved} request(s).')
    approve_selected.short_description = 'Approve selected requests'

    def deny_selected(self, request, queryset):
        denied = 0
        for registration_request in queryset:
            if registration_request.status != 'pending':
                continue
            services.deny_request(registration_request, reviewed_by=request.user)
            denied += 1
        self.message_user(request, f'Denied {denied} request(s).')
    deny_selected.short_description = 'Deny selected requests'
