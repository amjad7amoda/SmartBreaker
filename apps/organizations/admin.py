from django.contrib import admin

from . import services
from .models import Organization


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ('name', 'owner', 'status', 'phone', 'reviewed_by', 'created_at')
    list_filter = ('status',)
    search_fields = ('name', 'owner__email')
    readonly_fields = ('status', 'reviewed_by', 'reviewed_at', 'created_at')
    actions = ['approve_selected', 'deny_selected']

    def approve_selected(self, request, queryset):
        approved = 0
        for organization in queryset:
            if organization.status != 'pending':
                continue
            services.approve_organization(organization, reviewed_by=request.user)
            approved += 1
        self.message_user(request, f'Approved {approved} organization(s).')
    approve_selected.short_description = 'Approve selected organizations'

    def deny_selected(self, request, queryset):
        denied = 0
        for organization in queryset:
            if organization.status != 'pending':
                continue
            services.deny_organization(organization, reviewed_by=request.user)
            denied += 1
        self.message_user(request, f'Denied {denied} organization(s).')
    deny_selected.short_description = 'Deny selected organizations'
