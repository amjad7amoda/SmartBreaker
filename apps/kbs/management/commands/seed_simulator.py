"""Create the deterministic site used by the browser scenario runner."""

from datetime import datetime, time
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.breakers.models import Breaker, BreakerReading, BreakerStatus
from apps.kbs.models import Alert, KBSDecision, KBSSettings, ScheduledEvent
from apps.organizations.models import Organization
from apps.telemetry.models import Reading


SIMULATOR_EMAIL = 'simulator@smartbreaker.local'
SIMULATOR_ORG_NAME = 'SmartBreaker Simulator Site'

BREAKER_SPECS = {
    'sim-servers': {
        'priority_type': 'mandatory', 'priority_degree': 5,
        'load_type': 'normal', 'peak_load_W': 300, 'mean_load_W': 300,
        'cycle_start': None, 'cycle_end': None, 'initial_switch': True,
    },
    'sim-fridge': {
        'priority_type': 'normal', 'priority_degree': 3,
        'load_type': 'motor', 'peak_load_W': 600, 'mean_load_W': 150,
        'cycle_start': None, 'cycle_end': None, 'initial_switch': True,
    },
    'sim-ac-unit': {
        'priority_type': 'comfort', 'priority_degree': 2,
        'load_type': 'motor', 'peak_load_W': 1800, 'mean_load_W': 900,
        'cycle_start': time(0, 0), 'cycle_end': time(23, 59), 'initial_switch': False,
    },
    'sim-event-load': {
        'priority_type': 'normal', 'priority_degree': 8,
        'load_type': 'normal', 'peak_load_W': 700, 'mean_load_W': 700,
        'cycle_start': None, 'cycle_end': None, 'initial_switch': False,
    },
    'sim-grid': {
        'priority_type': 'ac_grid', 'priority_degree': 1,
        'load_type': 'normal', 'peak_load_W': 0, 'mean_load_W': 0,
        'cycle_start': None, 'cycle_end': None, 'initial_switch': False,
    },
}


class Command(BaseCommand):
    help = 'Create/update the local organization, breakers and event used by browser scenarios.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--reset-history', action='store_true',
            help='Delete prior simulator telemetry, decisions and alerts before reseeding.',
        )

    @transaction.atomic
    def handle(self, *args, **options):
        # Reuse a pre-existing simulator site when its globally unique sim-*
        # device ids already live together. This supports repositories that
        # were manually prepared before this bootstrap command existed while
        # still refusing to move identifiers between organizations.
        existing_breakers = list(
            Breaker.objects
            .filter(device_id__in=BREAKER_SPECS)
            .select_related('organization')
        )
        existing_org_ids = {breaker.organization_id for breaker in existing_breakers}
        if len(existing_org_ids) > 1:
            ownership = ', '.join(
                f'{breaker.device_id}->org {breaker.organization_id}'
                for breaker in existing_breakers
            )
            raise CommandError(
                f'Simulator breaker ids are split across organizations ({ownership}); '
                'refusing to choose or move them automatically.'
            )

        if existing_org_ids:
            organization = existing_breakers[0].organization
            existing_kbs = KBSSettings.objects.filter(organization=organization).first()
            looks_like_simulator = (
                'sim' in organization.name.lower()
                or (existing_kbs is not None and existing_kbs.data_source == 'simulator')
            )
            if not looks_like_simulator:
                raise CommandError(
                    f'Simulator device ids belong to organization {organization.id} '
                    f'("{organization.name}"), which is not marked as a simulator site.'
                )
            self.stdout.write(
                f'Reusing existing simulator organization {organization.id} '
                f'("{organization.name}").'
            )
        else:
            owner, owner_created = User.objects.get_or_create(
                email=SIMULATOR_EMAIL,
                defaults={
                    'role': 'home_user', 'is_active': True,
                    'must_set_password': False,
                },
            )
            if owner_created:
                owner.set_unusable_password()
                owner.save(update_fields=['password'])
            if not owner.is_active or owner.must_set_password:
                owner.is_active = True
                owner.must_set_password = False
                owner.save(update_fields=['is_active', 'must_set_password'])

            organization = Organization.objects.filter(
                name=SIMULATOR_ORG_NAME, owner=owner,
            ).first()
            if organization is None:
                organization = Organization.objects.create(
                    name=SIMULATOR_ORG_NAME,
                    phone='0000000000',
                    latitude='33.510000',
                    longitude='36.290000',
                    owner=owner,
                    status='active',
                )

        if organization.status != 'active':
            organization.status = 'active'
            organization.save(update_fields=['status'])

        kbs, _ = KBSSettings.objects.get_or_create(organization=organization)
        if options['reset_history']:
            # The destructive option is deliberately restricted to the
            # dedicated/generated site or a pre-existing site explicitly
            # identifiable as simulator-only; it cannot wipe an arbitrary site.
            if 'sim' not in organization.name.lower() and kbs.data_source != 'simulator':
                raise CommandError('History reset is restricted to an identifiable simulator site.')
            Reading.objects.filter(organization=organization).delete()
            BreakerReading.objects.filter(breaker__organization=organization).delete()
            KBSDecision.objects.filter(organization=organization).delete()
            Alert.objects.filter(organization=organization).delete()
            self.stdout.write(self.style.WARNING('Deleted prior history for the simulator site.'))

        kbs.mode = 'active'
        kbs.data_source = 'simulator'
        kbs.power_saving = False
        kbs.cycle_seconds = 5
        kbs.battery_capacity_Wh = 5000
        kbs.battery_low_voltage_V = 24
        kbs.battery_low_margin_V = 0.5
        kbs.battery_shutdown_buffer_percent = 2
        kbs.grid_present_min_V = 100
        kbs.max_inverter_power_W = 4000
        kbs.save()

        seeded = {}
        for device_id, spec in BREAKER_SPECS.items():
            existing = Breaker.objects.filter(device_id=device_id).first()
            if existing is not None and existing.organization_id != organization.id:
                raise CommandError(
                    f'{device_id} already belongs to organization {existing.organization_id}; '
                    'refusing to move a globally unique hardware identifier.'
                )
            defaults = {key: value for key, value in spec.items() if key != 'initial_switch'}
            breaker, _ = Breaker.objects.update_or_create(
                device_id=device_id,
                defaults={'organization': organization, **defaults},
            )
            breaker.locked_out = False
            breaker.lockout_reason = ''
            breaker.locked_at = None
            breaker.save(update_fields=['locked_out', 'lockout_reason', 'locked_at'])
            status_row, _ = BreakerStatus.objects.get_or_create(breaker=breaker)
            status_row.switch = spec['initial_switch']
            status_row.online = True
            status_row.fault = ''
            status_row.last_switched_on_at = timezone.now() if spec['initial_switch'] else None
            status_row.save()
            seeded[device_id] = breaker

        local_tz = ZoneInfo(settings.TIME_ZONE)
        event_start = datetime(2026, 8, 15, 11, 55, tzinfo=local_tz)
        event_end = datetime(2026, 8, 15, 12, 30, tzinfo=local_tz)
        event, _ = ScheduledEvent.objects.update_or_create(
            organization=organization,
            name='Browser simulator event scenario',
            defaults={'start_at': event_start, 'end_at': event_end},
        )
        event.required_breakers.set([seeded['sim-event-load']])

        self.stdout.write(self.style.SUCCESS('Browser simulator site is ready.'))
        self.stdout.write(f'Organization id: {organization.id}')
        self.stdout.write('Enter that id in the browser simulator before running Tier-2 scenarios.')
        if not options['reset_history']:
            self.stdout.write(
                'For a clean repeat run: python manage.py seed_simulator --reset-history'
            )
