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
        'device_id', 'organization', 'type', 'priority', 'protected', 'child_lock',
        'peak_load', 'mean_load', 'cycle_start', 'cycle_end',
    )
    list_filter = ('type', 'protected', 'child_lock', 'organization')
    search_fields = ('device_id', 'organization__name')
    readonly_fields = ('created_at',)
