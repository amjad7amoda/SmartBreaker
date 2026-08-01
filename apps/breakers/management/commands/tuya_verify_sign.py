from django.core.management.base import BaseCommand, CommandError

from apps.breakers.models import TuyaCredential
from apps.breakers.tuya import TuyaClient


class Command(BaseCommand):
    help = (
        'Recompute the signature for a request that Tuya already accepted and compare it '
        'to the signature that worked. A match proves both the stored secret and our '
        'signing implementation are correct.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--organization', type=int, required=True)
        parser.add_argument('--method', default='GET')
        parser.add_argument('--path', required=True, help='Path including query string.')
        parser.add_argument('--t', required=True, help='The t header from the working request.')
        parser.add_argument('--access-token', default='', help='Omit for a /v1.0/token request.')
        parser.add_argument('--expected', required=True, help='The sign header from the working request.')

    def handle(self, *args, **options):
        try:
            credential = TuyaCredential.objects.get(organization_id=options['organization'])
        except TuyaCredential.DoesNotExist:
            raise CommandError(f'No Tuya credential row for organization {options["organization"]}.')

        client = TuyaClient(credential)
        computed = client._sign(
            options['method'].upper(), options['path'], options['t'], options['access_token']
        )
        expected = options['expected'].strip().upper()

        self.stdout.write(f'computed : {computed}')
        self.stdout.write(f'expected : {expected}')

        if computed == expected:
            self.stdout.write(self.style.SUCCESS(
                'MATCH — the stored secret and the signing implementation are both correct.'
            ))
        else:
            self.stdout.write(self.style.ERROR(
                'MISMATCH — either the stored secret is wrong or the signing inputs differ '
                '(check that --path includes the query string exactly as sent).'
            ))
