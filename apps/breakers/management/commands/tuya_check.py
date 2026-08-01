import hashlib
import json
import time

from django.core.management.base import BaseCommand, CommandError

from apps.breakers.models import TuyaCredential
from apps.breakers.tuya import (
    TOKEN_PATH,
    TuyaAuthError,
    TuyaClient,
    TuyaDeviceError,
    TuyaError,
    TuyaUnavailableError,
)


class Command(BaseCommand):
    help = 'Verify that an organization\'s Tuya credentials sign correctly and that a device is reachable.'

    def add_arguments(self, parser):
        parser.add_argument('--organization', type=int, required=True)
        parser.add_argument('--device', type=str, required=True)
        parser.add_argument(
            '--debug', action='store_true',
            help='Print signing inputs and clock skew. Never prints the secret itself.',
        )

    def handle(self, *args, **options):
        try:
            credential = TuyaCredential.objects.get(organization_id=options['organization'])
        except TuyaCredential.DoesNotExist:
            raise CommandError(f'No Tuya credential stored for organization {options["organization"]}.')

        client = TuyaClient(credential)
        self.stdout.write(f'Project : {credential.client_id} ({credential.region}) -> {credential.api_base_url}')

        if options['debug']:
            self.debug_token(client, credential)

        try:
            client._access_token()
        except TuyaAuthError as exc:
            raise CommandError(
                f'Token request rejected ({exc.code}: {exc.message}). '
                'Check client_id, client_secret, region, and that this machine\'s clock is accurate.'
            )
        except TuyaUnavailableError as exc:
            raise CommandError(str(exc))
        self.stdout.write(self.style.SUCCESS('Token   : OK'))

        try:
            result = client.get_device_properties(options['device'])
        except TuyaDeviceError as exc:
            raise CommandError(
                f'Device rejected ({exc.code}: {exc.message}). '
                'The device_id is wrong or belongs to a different Tuya project.'
            )
        except TuyaError as exc:
            raise CommandError(str(exc))

        properties = result.get('properties', [])
        self.stdout.write(self.style.SUCCESS(f'Device  : OK ({len(properties)} properties)'))

        # Which codes the device actually accepts for writing is the thing that
        # cannot be inferred from a read, so it is reported explicitly.
        try:
            functions = client.get_device_functions(options['device'])
            codes = [f['code'] for f in functions.get('functions', [])]
            self.stdout.write(f'Writable: {", ".join(codes) if codes else "(none reported)"}')
        except TuyaError as exc:
            self.stdout.write(self.style.WARNING(f'Writable: could not be listed ({exc})'))

        self.stdout.write(json.dumps(properties, indent=2))

    def debug_token(self, client, credential):
        secret = credential.client_secret
        self.stdout.write('--- debug ---')
        self.stdout.write(f'client_id     : {credential.client_id!r} (len {len(credential.client_id)})')
        self.stdout.write(f'secret length : {len(secret)}')
        self.stdout.write(f'secret is hex : {all(c in "0123456789abcdefABCDEF" for c in secret)}')
        self.stdout.write(f'secret padded : {secret != secret.strip()}')

        local_ms = int(time.time() * 1000)
        payload = client._call('GET', TOKEN_PATH)
        server_ms = payload.get('t')

        self.stdout.write(f'string signed : {("GET|" + hashlib.sha256(b"").hexdigest() + "||" + TOKEN_PATH)}')
        if server_ms:
            skew = (int(server_ms) - local_ms) / 1000
            marker = 'OK' if abs(skew) < 60 else 'TOO LARGE — Tuya will reject every request'
            self.stdout.write(f'clock skew    : {skew:.1f}s ({marker})')
        self.stdout.write(f'raw response  : {json.dumps(payload)}')
        self.stdout.write('--- end debug ---')
