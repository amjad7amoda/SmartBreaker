from django import forms
from django.contrib import admin

from .models import Breaker, TuyaCredential


class TuyaCredentialForm(forms.ModelForm):
    client_secret = forms.CharField(
        widget=forms.PasswordInput(render_value=False),
        required=False,
        help_text='Leave blank to keep the currently stored secret.',
    )

    class Meta:
        model = TuyaCredential
        fields = ('organization', 'client_id', 'region')

    def clean_client_secret(self):
        secret = self.cleaned_data.get('client_secret', '').strip()
        if not secret and not self.instance.pk:
            raise forms.ValidationError('A client_secret is required when adding a credential.')
        return secret

    def save(self, commit=True):
        credential = super().save(commit=False)
        if self.cleaned_data['client_secret']:
            credential.client_secret = self.cleaned_data['client_secret']
        if commit:
            credential.save()
        return credential


@admin.register(TuyaCredential)
class TuyaCredentialAdmin(admin.ModelAdmin):
    form = TuyaCredentialForm
    list_display = ('organization', 'client_id', 'region', 'updated_at')
    list_filter = ('region',)
    search_fields = ('organization__name', 'client_id')


@admin.register(Breaker)
class BreakerAdmin(admin.ModelAdmin):
    list_display = (
        'device_id', 'organization', 'priority_type', 'priority_degree',
        'load_type', 'peak_load_W', 'mean_load_W',
        'cycle_start', 'cycle_end', 'locked_out',
    )
    list_filter = ('priority_type', 'load_type', 'locked_out', 'organization')
    search_fields = ('device_id', 'organization__name')
    readonly_fields = ('created_at',)


@admin.register(BreakerStatus)
class BreakerStatusAdmin(admin.ModelAdmin):
    list_display = (
        'breaker', 'switch', 'online', 'fault',
        'cur_power_mW', 'cur_current_mA', 'cur_voltage_mV', 'reported_at',
    )
    list_filter = ('switch', 'online',)
    search_fields = ('breaker__device_id',)
    readonly_fields = ('reported_at',)


@admin.register(BreakerReading)
class BreakerReadingAdmin(admin.ModelAdmin):
    list_display = ('breaker', 'timestamp', 'switch', 'cur_power_mW')
    list_filter = ('switch',)
    search_fields = ('breaker__device_id',)
