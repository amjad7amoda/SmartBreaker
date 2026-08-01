import getpass

from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError

from apps.breakers.models import TuyaCredential

EXPECTED_SECRET_LENGTH = 32


class Command(BaseCommand):
    help = 'Store an organization\'s Tuya client_secret, read from a hidden prompt.'

    def add_arguments(self, parser):
        parser.add_argument('--organization', type=int, required=True)

    def handle(self, *args, **options):
        try:
            credential = TuyaCredential.objects.get(organization_id=options['organization'])
        except TuyaCredential.DoesNotExist:
            raise CommandError(f'No Tuya credential row for organization {options["organization"]}.')

        secret = getpass.getpass('Tuya Access Secret (input hidden): ').strip()
        if not secret:
            raise CommandError('No secret entered.')

        if len(secret) != EXPECTED_SECRET_LENGTH:
            self.stdout.write(self.style.WARNING(
                f'Warning: got {len(secret)} characters, expected {EXPECTED_SECRET_LENGTH}. '
                'Saving anyway, but verify you copied the Access Secret and not the Access ID or a sign value.'
            ))

        credential.client_secret = secret
        credential.save()
        # A token minted with the old secret would keep being served from cache.
        cache.delete(f'tuya:token:{credential.client_id}')
        self.stdout.write(self.style.SUCCESS(f'Secret stored for {credential.organization.name}.'))
