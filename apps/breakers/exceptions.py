from rest_framework import serializers
from rest_framework.exceptions import APIException

from .tuya import TuyaAuthError, TuyaDeviceError, TuyaUnavailableError


class TuyaMisconfigured(APIException):
    status_code = 502
    default_detail = 'Tuya rejected this organization\'s credentials.'


class TuyaUnreachable(APIException):
    status_code = 503
    default_detail = 'Tuya could not be reached. Try again shortly.'


def translate(exc, field=None):
    if isinstance(exc, TuyaDeviceError):
        message = f'Tuya rejected this device ({exc.code}): {exc.message}'
        return serializers.ValidationError({field: message} if field else message)
    if isinstance(exc, TuyaAuthError):
        return TuyaMisconfigured(f'{TuyaMisconfigured.default_detail} ({exc.code}: {exc.message})')
    if isinstance(exc, TuyaUnavailableError):
        return TuyaUnreachable(str(exc))
    return APIException(str(exc))
