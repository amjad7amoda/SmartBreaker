"""KBS control and closed-loop simulator API."""

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.breakers.models import Breaker, BreakerReading, BreakerStatus
from apps.organizations.models import Organization
from apps.telemetry.models import Reading

from .climate import CLIMATE_CSV_PATH, ClimateDataError, load_climate_rows
from .models import Alert, BreakerAction, KBSDecision, KBSSettings
from .services import run_cycle

SETTINGS_EDITABLE_FIELDS = (
    'cycle_seconds', 'power_saving', 'mode', 'data_source',
    'battery_low_voltage_V', 'battery_low_margin_V', 'battery_shutdown_buffer_percent',
    'joule_deficit_limit_J', 'grid_present_min_V',
)
SETTINGS_SHARED_FIELDS = SETTINGS_EDITABLE_FIELDS + (
    'battery_capacity_Wh', 'night_reserve_percent', 'stability_threshold_percent',
    'event_stability_threshold_percent', 'heatsink_temp_limit_C',
    'deficit_window_minutes', 'max_inverter_power_W', 'sudden_drop_fraction',
    'sudden_draw_W', 'baseline_minutes', 'motor_peak_minutes', 'event_prep_hours',
    'day_start', 'day_end', 'pv_day_min_W',
)


def _org_or_none(request, from_query=True):
    org_id = (request.query_params if from_query else request.data).get('organization')
    return Organization.objects.filter(id=org_id).first()


def _action_dict(action):
    return {
        'id': action.id,
        'device_id': action.breaker.device_id,
        'action': action.action,
        'countdown_s': action.countdown_s,
        'reason': action.reason,
        'branch': action.decision.branch,
        'created_at': action.created_at,
    }


def _reading_dict(reading):
    if reading is None:
        return None
    result = {'organization': reading.organization_id}
    for field in Reading._meta.concrete_fields:
        if field.name in ('pk', 'organization'):
            continue
        result[field.name] = getattr(reading, field.name)
    return result


def _breaker_dict(breaker):
    try:
        current = breaker.status
    except BreakerStatus.DoesNotExist:
        current = None
    return {
        'device_id': breaker.device_id,
        'priority_type': breaker.priority_type,
        'priority_degree': breaker.priority_degree,
        'load_type': breaker.load_type,
        'peak_load_W': breaker.peak_load_W,
        'mean_load_W': breaker.mean_load_W,
        'cycle_start': breaker.cycle_start,
        'cycle_end': breaker.cycle_end,
        'switch': current.switch if current else None,
        'countdown_1_s': current.countdown_1_s if current else 0,
        'online': current.online if current else None,
        'fault': current.fault if current else '',
        'child_lock': breaker.child_lock,
        'locked_out': breaker.locked_out,
        'lockout_reason': breaker.lockout_reason,
        'locked_at': breaker.locked_at,
        'reported_at': current.reported_at if current else None,
    }


class ClimateView(APIView):
    """Return validated source climatology; never substitute invented values."""

    permission_classes = [AllowAny]

    def get(self, request):
        try:
            rows = list(load_climate_rows(str(CLIMATE_CSV_PATH)))
        except ClimateDataError as exc:
            return Response(
                {'detail': str(exc), 'source': str(CLIMATE_CSV_PATH)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        cities = sorted({row['city'] for row in rows})
        city = request.query_params.get('city')
        month = request.query_params.get('month')
        if city:
            rows = [row for row in rows if row['city'] == city]
        if month is not None:
            try:
                month_number = int(month)
            except (TypeError, ValueError):
                return Response({'detail': 'month must be an integer from 1 to 12'}, status=400)
            if month_number not in range(1, 13):
                return Response({'detail': 'month must be an integer from 1 to 12'}, status=400)
            rows = [row for row in rows if row['month'] == month_number]
        return Response({'cities': cities, 'count': len(rows), 'rows': rows})


class RunCycleView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        org = _org_or_none(request, from_query=False)
        if org is None:
            return Response({'detail': 'unknown organization'}, status=status.HTTP_404_NOT_FOUND)
        decision = run_cycle(org)
        if decision is None:
            return Response({
                'engine': 'apps.kbs.services.run_cycle',
                'branch': None,
                'facts': None,
                'actions': [],
                'detail': 'skipped (observing mode or no readings)',
            })
        return Response({
            'engine': 'apps.kbs.services.run_cycle',
            'branch': decision.branch,
            'created_at': decision.created_at,
            'facts': decision.facts,
            'actions': [_action_dict(a) for a in decision.actions.select_related('breaker', 'decision')],
        })


class SimStateView(APIView):
    """Return engine settings, source state, pending work, metadata and lockouts."""

    permission_classes = [AllowAny]

    def get(self, request):
        org = _org_or_none(request)
        if org is None:
            return Response({'detail': 'unknown organization'}, status=status.HTTP_404_NOT_FOUND)
        kbs, _ = KBSSettings.objects.get_or_create(organization=org)
        latest = KBSDecision.objects.filter(organization=org).first()
        newest_per_breaker = {}
        for action in (
            BreakerAction.objects
            .filter(breaker__organization=org, executed=False)
            .select_related('breaker', 'decision')
            .order_by('created_at')
        ):
            newest_per_breaker[action.breaker_id] = action
        pending = sorted(newest_per_breaker.values(), key=lambda action: action.created_at)
        alerts = Alert.objects.filter(organization=org)[:10]
        latest_reading = Reading.objects.filter(organization=org).first()
        breakers = Breaker.objects.filter(organization=org).select_related('status')
        return Response({
            'organization': {
                'id': org.id,
                'name': org.name,
                'latitude': float(org.latitude),
                'longitude': float(org.longitude),
                'status': org.status,
            },
            'settings': {field: getattr(kbs, field) for field in SETTINGS_SHARED_FIELDS},
            'latest_telemetry': _reading_dict(latest_reading),
            'breakers': [_breaker_dict(breaker) for breaker in breakers],
            'metadata': {
                'engine': 'apps.kbs.services.run_cycle',
                'data_source': kbs.data_source,
                'generated_at': timezone.now(),
            },
            'latest_decision': (
                {
                    'engine': 'apps.kbs.services.run_cycle',
                    'branch': latest.branch,
                    'created_at': latest.created_at,
                    'facts': latest.facts,
                } if latest else None
            ),
            'pending_actions': [_action_dict(action) for action in pending],
            'recent_alerts': [
                {'kind': alert.kind, 'severity': alert.severity, 'message': alert.message, 'created_at': alert.created_at}
                for alert in alerts
            ],
        })


class AckActionsView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        ids = request.data.get('action_ids', [])
        updated = BreakerAction.objects.filter(id__in=ids, executed=False).update(executed=True)
        return Response({'acknowledged': updated})


class SettingsView(APIView):
    permission_classes = [AllowAny]

    def patch(self, request):
        org = _org_or_none(request)
        if org is None:
            return Response({'detail': 'unknown organization'}, status=status.HTTP_404_NOT_FOUND)
        kbs, _ = KBSSettings.objects.get_or_create(organization=org)
        changed = {}
        for field in SETTINGS_EDITABLE_FIELDS:
            if field in request.data:
                setattr(kbs, field, request.data[field])
                changed[field] = request.data[field]
        try:
            kbs.full_clean(exclude=['organization'])
        except Exception as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        kbs.save()
        return Response({'updated': changed})


class SimulatorOnlyMixin:
    def simulator_org(self, request):
        org = _org_or_none(request, from_query=False)
        if org is None:
            return None, Response({'detail': 'unknown organization'}, status=404)
        settings_row = KBSSettings.objects.filter(organization=org).first()
        if settings_row is None or settings_row.data_source != 'simulator':
            return None, Response({'detail': 'operation is restricted to simulator organizations'}, status=403)
        return org, None


class SimResetView(SimulatorOnlyMixin, APIView):
    """Clear scoped run history while preserving configuration and definitions."""

    permission_classes = [AllowAny]

    @transaction.atomic
    def post(self, request):
        org, error = self.simulator_org(request)
        if error:
            return error
        if request.data.get('confirm') is not True:
            return Response({'detail': 'confirm must be true'}, status=400)
        deleted = {
            'telemetry': Reading.objects.filter(organization=org).count(),
            'breaker_readings': BreakerReading.objects.filter(breaker__organization=org).count(),
            'decisions': KBSDecision.objects.filter(organization=org).count(),
            'alerts': Alert.objects.filter(organization=org).count(),
        }
        Reading.objects.filter(organization=org).delete()
        BreakerReading.objects.filter(breaker__organization=org).delete()
        KBSDecision.objects.filter(organization=org).delete()
        Alert.objects.filter(organization=org).delete()
        Breaker.objects.filter(organization=org).update(
            child_lock=False, locked_out=False, lockout_reason='', locked_at=None,
        )
        BreakerStatus.objects.filter(breaker__organization=org).update(
            countdown_1_s=0, child_lock=False,
        )
        return Response({'reset': True, 'organization': org.id, 'deleted': deleted})


class BreakerOverrideView(SimulatorOnlyMixin, APIView):
    """Apply an explicit physical simulator switch and record its reading."""

    permission_classes = [AllowAny]

    @transaction.atomic
    def post(self, request):
        org, error = self.simulator_org(request)
        if error:
            return error
        device_id = request.data.get('device_id')
        switch = request.data.get('switch')
        if not device_id or type(switch) is not bool:
            return Response({'detail': 'device_id and boolean switch are required'}, status=400)
        timestamp = parse_datetime(str(request.data.get('timestamp', '')))
        if timestamp is None:
            return Response({'detail': 'timestamp must be an ISO-8601 datetime'}, status=400)
        if timezone.is_naive(timestamp):
            timestamp = timezone.make_aware(timestamp)
        breaker = (
            Breaker.objects.select_for_update()
            .filter(organization=org, device_id=device_id)
            .first()
        )
        if breaker is None:
            return Response({'detail': 'breaker does not belong to the selected simulator organization'}, status=404)
        current, _ = BreakerStatus.objects.select_for_update().get_or_create(breaker=breaker)
        was_on = current.switch
        current.switch = switch
        current.countdown_1_s = 0
        current.online = True
        if switch and not was_on:
            current.last_switched_on_at = timestamp
        if switch:
            current.child_lock = False
        current.save()
        if switch:
            breaker.child_lock = False
            breaker.locked_out = False
            breaker.lockout_reason = ''
            breaker.locked_at = None
            breaker.save(update_fields=['child_lock', 'locked_out', 'lockout_reason', 'locked_at'])
        BreakerReading.objects.update_or_create(
            breaker=breaker,
            timestamp=timestamp,
            defaults={'switch': switch, 'cur_power_mW': current.cur_power_mW},
        )
        return Response({
            'organization': org.id,
            'device_id': breaker.device_id,
            'switch': current.switch,
            'countdown_1_s': current.countdown_1_s,
            'child_lock': breaker.child_lock,
            'locked_out': breaker.locked_out,
            'timestamp': timestamp,
        })
