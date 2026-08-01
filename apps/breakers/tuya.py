import hashlib
import hmac
import json
import time

import requests
from django.core.cache import cache

TOKEN_PATH = '/v1.0/token?grant_type=1'
REQUEST_TIMEOUT = 10
TOKEN_REFRESH_MARGIN = 60

TOKEN_EXPIRED_CODES = {1010, 1011, 1012}
AUTH_ERROR_CODES = {1000, 1003, 1004, 1005, 1013, 1114}
DEVICE_ERROR_CODES = {1106, 2009, 2401}


class TuyaError(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(f'Tuya error {code}: {message}')


class TuyaAuthError(TuyaError):
    """Our credentials, clock or region are wrong — not something a user can fix."""


class TuyaDeviceError(TuyaError):
    """The requested device does not exist or is outside this Tuya project."""


class TuyaUnavailableError(TuyaError):
    """Tuya could not be reached at all."""

    def __init__(self, message):
        super().__init__(None, message)


def _classify(code, message):
    if code in AUTH_ERROR_CODES:
        return TuyaAuthError(code, message)
    if code in DEVICE_ERROR_CODES:
        return TuyaDeviceError(code, message)
    # Commanding an unplugged breaker is a normal, user-visible situation, but
    # the code Tuya uses for it varies by endpoint, so fall back to the text.
    if message and 'offline' in message.lower():
        return TuyaDeviceError(code, message)
    return TuyaError(code, message)


class TuyaClient:
    def __init__(self, credential):
        self.base_url = credential.api_base_url
        self.client_id = credential.client_id
        # Decrypted once per client rather than on every signature.
        self._client_secret = credential.client_secret

    # --- public API -----------------------------------------------------

    def verify(self):
        # Minting a token is the cheapest call that exercises client_id, secret
        # and region together, so it is what proves a credential actually works.
        self._access_token()

    def get_device_properties(self, device_id):
        return self._request('GET', f'/v2.0/cloud/thing/{device_id}/shadow/properties')

    def get_device_specification(self, device_id):
        return self._request('GET', f'/v1.0/devices/{device_id}/specifications')

    def get_device_functions(self, device_id):
        return self._request('GET', f'/v1.0/iot-03/devices/{device_id}/functions')

    def send_commands(self, device_id, commands):
        # `code` must come from get_device_functions, not from the property
        # names a read returns: this device reports `switch_1` but only accepts
        # `switch`. Sending a read-only code yields 2008.
        return self._request(
            'POST', f'/v1.0/iot-03/devices/{device_id}/commands', body={'commands': commands}
        )

    def set_properties(self, device_id, properties):
        # Alternative write path for devices on the newer "thing" model. Note
        # that `properties` is itself a JSON-encoded string, not an object.
        return self._request(
            'POST', f'/v2.0/cloud/thing/{device_id}/shadow/properties',
            body={'properties': json.dumps(properties, separators=(',', ':'))},
        )

    # --- signing --------------------------------------------------------

    def _sign(self, method, path, timestamp, access_token='', body_text=''):
        body_hash = hashlib.sha256(body_text.encode()).hexdigest()
        str_to_sign = f'{method}\n{body_hash}\n\n{path}'
        payload = f'{self.client_id}{access_token}{timestamp}{str_to_sign}'
        return hmac.new(
            self._client_secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest().upper()

    def _call(self, method, path, access_token='', body=None):
        # The signed bytes and the sent bytes must be identical, so the body is
        # serialised once here and shipped verbatim. Handing the dict to
        # requests' json= would re-serialise it and can change the spacing,
        # which silently invalidates the signature.
        body_text = '' if body is None else json.dumps(body, separators=(',', ':'))

        timestamp = str(int(time.time() * 1000))
        headers = {
            'client_id': self.client_id,
            'sign': self._sign(method, path, timestamp, access_token, body_text),
            't': timestamp,
            'sign_method': 'HMAC-SHA256',
        }
        if access_token:
            headers['access_token'] = access_token
        if body_text:
            headers['Content-Type'] = 'application/json'

        try:
            response = requests.request(
                method, f'{self.base_url}{path}', headers=headers,
                data=body_text.encode() if body_text else None, timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            raise TuyaUnavailableError(f'Could not reach Tuya: {exc}') from exc
        except ValueError as exc:
            raise TuyaUnavailableError(f'Tuya returned a non-JSON response: {exc}') from exc

    # --- token ----------------------------------------------------------

    @property
    def _token_cache_key(self):
        return f'tuya:token:{self.client_id}'

    def _access_token(self):
        token = cache.get(self._token_cache_key)
        if token:
            return token

        payload = self._call('GET', TOKEN_PATH)
        if not payload.get('success'):
            # A token request can only fail on client_id/secret/region/clock.
            raise TuyaAuthError(payload.get('code'), payload.get('msg', 'token request failed'))

        result = payload['result']
        token = result['access_token']
        ttl = max(int(result.get('expire_time', 7200)) - TOKEN_REFRESH_MARGIN, 60)
        cache.set(self._token_cache_key, token, ttl)
        return token

    # --- request --------------------------------------------------------

    def _request(self, method, path, body=None, allow_token_retry=True):
        payload = self._call(method, path, self._access_token(), body)
        if payload.get('success'):
            return payload.get('result')

        code = payload.get('code')
        message = payload.get('msg', 'unknown error')

        # A cached token can be revoked before it expires; drop it and retry once.
        if code in TOKEN_EXPIRED_CODES and allow_token_retry:
            cache.delete(self._token_cache_key)
            return self._request(method, path, body, allow_token_retry=False)

        raise _classify(code, message)
