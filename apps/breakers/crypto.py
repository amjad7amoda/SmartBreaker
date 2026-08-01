from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

GENERATE_HINT = (
    'Generate one with: python -c "from cryptography.fernet import Fernet; '
    'print(Fernet.generate_key().decode())"'
)


def _fernet():
    key = getattr(settings, 'TUYA_FERNET_KEY', None)
    if not key:
        raise ImproperlyConfigured(f'TUYA_FERNET_KEY is not set. {GENERATE_HINT}')
    try:
        return Fernet(key)
    except (ValueError, TypeError) as exc:
        raise ImproperlyConfigured(f'TUYA_FERNET_KEY is not a valid Fernet key ({exc}). {GENERATE_HINT}')


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise ValueError(
            'Stored credential could not be decrypted. TUYA_FERNET_KEY has most '
            'likely changed since it was saved; the credential must be re-entered.'
        ) from exc
