import secrets

from django.core.management.base import BaseCommand, CommandError

from apps.kbs.models import EdgeDevice
from apps.organizations.models import Organization


class Command(BaseCommand):
    help = 'Provision or rotate an organization-scoped edge device credential.'

    def add_arguments(self, parser):
        parser.add_argument('organization_id', type=int)
        parser.add_argument('--name', default='Primary edge')
        parser.add_argument('--rotate', action='store_true')

    def handle(self, *args, **options):
        organization = Organization.objects.filter(pk=options['organization_id']).first()
        if organization is None:
            raise CommandError('Unknown organization.')
        device = EdgeDevice.objects.filter(
            organization=organization, name=options['name'],
        ).first()
        if device is not None and not options['rotate']:
            raise CommandError('Device already exists; pass --rotate to replace its secret.')
        if device is None:
            device = EdgeDevice(organization=organization, name=options['name'])
        plaintext = secrets.token_urlsafe(32)
        device.set_secret(plaintext)
        device.status = 'active'
        device.save()
        self.stdout.write(self.style.SUCCESS('Edge credential provisioned.'))
        self.stdout.write(f'Device ID: {device.device_id}')
        self.stdout.write(f'Token: {device.device_id}.{plaintext}')
        self.stdout.write('Store this token now; the plaintext secret cannot be recovered.')
