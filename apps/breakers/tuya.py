import hashlib
import hmac
import json
import time

import requests
from django.core.cache import cache
from requests.adapters import HTTPAdapter

TOKEN_PATH = '/v1.0/token?grant_type=1'
REQUEST_TIMEOUT = 10
TOKEN_REFRESH_MARGIN = 60

session = requests.Session()
session.mount('https://', HTTPAdapter(pool_connections=4, pool_maxsize=16))

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


def classify(code, message):

    if code in AUTH_ERROR_CODES:
        return TuyaAuthError(code, message)
    
    if code in DEVICE_ERROR_CODES:
        return TuyaDeviceError(code, message)

    if message and 'offline' in message.lower():
        return TuyaDeviceError(code, message)
    
    return TuyaError(code, message)

class TuyaClient:

    def __init__(self, credential):
        self.base_url = credential.api_base_url
        self.client_id = credential.client_id
        self.client_secret = credential.client_secret

    def verify(self):
        self.access_token()

    # Device queries
    def get_device_properties(self, device_id):
        return self.request('GET', f'/v2.0/cloud/thing/{device_id}/shadow/properties')

    def get_device_specification(self, device_id):
        return self.request('GET', f'/v1.0/devices/{device_id}/specifications')

    def get_device_functions(self, device_id):
        return self.request('GET', f'/v1.0/iot-03/devices/{device_id}/functions')

    # Commands 
    def send_commands(self, device_id, commands):
        return self.request(
            'POST', f'/v1.0/iot-03/devices/{device_id}/commands', body={'commands': commands}
        )

    def set_properties(self, device_id, properties):
        return self.request(
            'POST', f'/v2.0/cloud/thing/{device_id}/shadow/properties',
            body={'properties': json.dumps(properties, separators=(',', ':'))},
        )

    # Signing
    def sign(self, method, path, timestamp, access_token='', body_text=''):
        body_hash = hashlib.sha256(body_text.encode()).hexdigest()
        str_tosign = f'{method}\n{body_hash}\n\n{path}'
        payload = f'{self.client_id}{access_token}{timestamp}{str_tosign}'
        return hmac.new(
            self.client_secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest().upper()

    def call(self, method, path, access_token='', body=None):
        body_text = '' if body is None else json.dumps(body, separators=(',', ':'))
        timestamp = str(int(time.time() * 1000))
        headers = {
            'client_id': self.client_id,
            'sign': self.sign(method, path, timestamp, access_token, body_text),
            't': timestamp,
            'sign_method': 'HMAC-SHA256',
        }
        if access_token:
            headers['access_token'] = access_token
        if body_text:
            headers['Content-Type'] = 'application/json'

        try:
            response = session.request(
                method, f'{self.base_url}{path}', headers=headers,
                data=body_text.encode() if body_text else None, timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            raise TuyaUnavailableError(f'Could not reach Tuya: {exc}') from exc
        except ValueError as exc:
            raise TuyaUnavailableError(f'Tuya returned a non-JSON response: {exc}') from exc

    # Token
    @property
    def token_cache_key(self):
        return f'tuya:token:{self.client_id}'

    def access_token(self):
        token = cache.get(self.token_cache_key)
        if token:
            return token

        payload = self.call('GET', TOKEN_PATH)
        if not payload.get('success'):
            raise TuyaAuthError(payload.get('code'), payload.get('msg', 'token request failed'))

        result = payload['result']
        token = result['access_token']
        ttl = max(int(result.get('expire_time', 7200)) - TOKEN_REFRESH_MARGIN, 60)
        cache.set(self.token_cache_key, token, ttl)
        return token

    def request(self, method, path, body=None, allow_token_retry=True):
        payload = self.call(method, path, self.access_token(), body)
        if payload.get('success'):
            return payload.get('result')

        code = payload.get('code')
        message = payload.get('msg', 'unknown error')

        # A cached token can be revoked before it expires; drop it and retry once.
        if code in TOKEN_EXPIRED_CODES and allow_token_retry:
            cache.delete(self.token_cache_key)
            return self.request(method, path, body, allow_token_retry=False)

        raise classify(code, message)
