import uuid

from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from .models import EdgeDevice


class DeviceAuthentication(BaseAuthentication):
    """Authenticate ``Authorization: Device <device-id>.<secret>``."""

    keyword = 'Device'

    def authenticate(self, request):
        header = request.headers.get('Authorization', '')
        if not header:
            return None
        scheme, separator, credential = header.partition(' ')
        if scheme.lower() != self.keyword.lower() or not separator:
            return None
        device_text, separator, secret = credential.partition('.')
        if not separator or not secret:
            raise AuthenticationFailed('Invalid device credential format.')
        try:
            device_id = uuid.UUID(device_text)
        except (ValueError, TypeError):
            raise AuthenticationFailed('Invalid device identifier.')
        device = EdgeDevice.objects.select_related('organization').filter(
            device_id=device_id
        ).first()
        if device is None or not device.secret_is_valid(secret):
            raise AuthenticationFailed('Invalid or revoked device credential.')
        return device, device

    def authenticate_header(self, request):
        return self.keyword
